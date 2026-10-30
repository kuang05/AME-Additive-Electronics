"""STEP（AP203/214 子集）读取与写出 —— 几何唯一入口（STR-001/002）。

只做 B-Rep 解析，不做 STL、不做网格布尔（§1.6）。
写出器用于生成样例与测试样本；读取器面向常见 CAD 导出的多环面模型。

曲面支持范围（面积一律解析式优先，不用三角网格累加）：
  平面 PLANE                 精确（环多边形面积，扣孔洞）
  柱面 CYLINDRICAL_SURFACE   精确（2πr·轴向跨度，与真实高度相关）
  圆锥 CONICAL_SURFACE       精确（圆台侧面积 π(r1+r2)·l，l 为母线长）
  球面 SPHERICAL_SURFACE     精确到纬带（A=r²(cosθ1−cosθ2)·Δφ；整球即 4πr²），
                              单纬环无法判侧别 → 按较小球冠估算并标 APPROX
  环面 TOROIDAL_SURFACE      参数范围积分（A=Δu·r[R·Δv + r(sin v2−sin v1)]），标 APPROX
  样条 B_SPLINE_SURFACE*     不支持，面积按包围盒/三角形扇保守估算（标 UNSUPPORTED）
  SURFACE_OF_REVOLUTION 等   同"样条"一行

诊断码（STR-002，绝不静默修补）：
  G-UNIT-MISSING     未声明长度单位（按 mm 处理并提示）
  G-OPEN-SHELL       壳有单边（缺面/缝合失败）
  G-TINY-FACE        极小面（< 0.01 mm²）
  G-DEGEN-LOOP       环顶点数 < 3
  G-CURVE-UNSUPPORTED 环含非直线曲线（面积按包围盒估算）
  G-NONPLANAR-FACE   平面面但环点偏离平面超差
  G-SELF-INTERSECT   环投影自交
  G-SURFACE-UNSUPPORTED 曲面类型不支持/该面无法构成有效面片（样条等），面积按包围盒估算
  G-SURFACE-APPROX   曲面已解析但只是近似（环面参数近似、球冠侧别按较小球冠假设），
                     面积/法向可用但不保证是精确曲面几何
  G-WINDING-INCONSISTENT 相邻面绕向不一致（法向可能翻转）
  G-NORMAL-INWARD    多数面法向朝内（实体可能是"内外翻转"的）
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------- 基础向量
# 向量与多边形原语已拆到 geometry/vec3.py；这里保留 re-export，
# 因为旧调用方（analysis / tests / samples）按 `from .stepio import v_dot` 使用。
from .vec3 import (centroid_3d, polygon_area_3d, v_add, v_cross, v_dot,  # noqa: F401
                   v_len, v_mul, v_sub, v_unit, v_unit_safe, _v3)


# ---------------------------------------------------------------- 数据/语法/装配（已拆分）
# 这些定义已移到 types.py / syntax.py / assembly.py；此处 re-export 保持
# 旧调用方（analysis、samples、tests）按 `from .stepio import Face` 可用。
from .assembly import (_apply_matrix, _apply_matrix_dir, _assembly_edges,  # noqa: F401
                       _axis_placement, _det, _flatten_assembly,
                       _flatten_mapped_items, _matrix_mul, _placement_matrix,
                       _rep_faces, _transform_face_inplace)
from .syntax import (A, _ARG_QUOTED_RE, _COMPLEX_PART_RE, _ENTITY_RE,  # noqa: F401
                     _NEED_ARGS, _REF_RE, _STMT_RE, _num, _parse_entities,
                     _point, _ref, _refs, _split_args, _split_complex,
                     _tess_float_list, _tess_int_triplets)
from .types import Face, StepModel  # noqa: F401

# 曲面离散与解析面积已拆到 geometry/surfaces.py；re-export 保持旧调用方可用。
from .surfaces import (_angle_sweep, _cone_face, _fallback_face,  # noqa: F401,E402
                       _ref_dir, _ring_area, _sphere_face,
                       _surface_type_name, _torus_face)


# ---------------------------------------------------------------- 读取器
_TOKEN = re.compile(r"#\d+|'[^']*'|\(|\)|,|;|[A-Za-z_0-9_.\-+]+")
_STRUCT_RE = re.compile(r"[(),']")














# 解析阶段函数族已拆到 step_build（本模块保留同名 re-export，
# 兼容旧导入路径；F401 在 ruff.toml 全局豁免）。
from .step_build import (  # noqa: F401
    _arc_points,
    _assemble_faces,
    _build_curves,
    _build_edge_geom,
    _build_point_dirs,
    _build_surfaces,
    _check_shell,
    _convert_inch_to_mm,
    _detect_length_unit,
    _edge_ring,
    _finalize_geometry,
    _loop_self_intersects,
    _make_placement,
    _signed_volume,
    _surface_area,
    _unit_from_conversion,
    _unit_from_si,
    _unit_from_text_fallback,
)

def read_step(text: str, tess_tol: float = 0.02) -> StepModel:
    """解析 STEP 文本。

    tess_tol: 圆弧离散的弦高容差（mm，STR-008 可调）。默认 0.02 mm，
    远低于机器定位精度；调大到 0.05 可减少面数、加快大模型渲染。
    """
    model = StepModel()
    complex_parts: dict[int, list] = {}
    complex_types: dict[int, list] = {}
    ent = _parse_entities(text, complex_parts, complex_types)
    # ---- 长度单位识别（PRJ-008：全软件统一 mm，inch 需换算）----
    unit_scale, unit_name, unit_found = _detect_length_unit(ent, text)
    if not unit_found:
        model.diagnostics.append({"code": "G-UNIT-MISSING",
                                  "msg": "文件未声明长度单位，按 mm 处理",
                                  "advice": "导出 STEP 时请选择毫米单位"})
    elif abs(unit_scale - 1.0) > 1e-9:
        model.unit = unit_name

    pts, dirs = _build_point_dirs(ent)

    # 轴放置/曲面/曲线/边几何的构建拆到模块级函数（见各函数 docstring）。
    placement = _make_placement(ent, pts, dirs)
    surfaces = _build_surfaces(ent, placement)
    circles, lines = _build_curves(ent, pts, dirs, placement)
    edge_geom = _build_edge_geom(ent, pts)

    # 面
    (faces, edge_use, edge_dir_use, unsupported_face_types,
     face_entity_id, face_no) = _assemble_faces(ent, surfaces, circles, edge_geom,
                                                tess_tol, unit_scale,
                                                complex_parts, complex_types)
    model.faces.extend(faces)

    # ---- AP242 三角化几何（STR-001）----
    # 网格型 AP242 没有 ADVANCED_FACE，几何全在 TRIANGULATED_FACE_SET 里。
    # 原实现只认 ADVANCED_FACE，这类文件报 G-NO-FACE（提示语还误导成"格式不对"）。
    if not model.faces:
        _read_tessellated(model, ent, face_no)
    elif unsupported_face_types:
        # 混合文件（B-rep + AP242 网格）：精确曲面已解析，剩下一批无法求值的
        # 曲面若能找到同文件的三角化数据，就用它替换包围盒估算 —— 这比估算准得多。
        # 被替换的面改标 G-SURFACE-APPROX，并从未解决清单里移除。
        for upgraded_face in _apply_tessellated_to_faces(
                model, _collect_triangles(ent)[0]):
            unsupported_face_types.pop(upgraded_face.fid, None)

    # ---- 装配体位姿展平（STR-001）----
    # 同一个零件被 NEXT_ASSEMBLY_USAGE_OCCURRENCE 多次引用时，每次引用有自己的
    # ITEM_DEFINED_TRANSFORMATION。原实现只解析一份几何，多次引用会重叠在原坐标。
    # 这里为每个额外实例复制一份面并施加变换。
    _flatten_assembly(model, ent, face_entity_id, complex_parts)

    # ---- 壳有效性检查 + 收尾（诊断汇总 / 单位换算 / bbox / 体积）----
    open_edges = _check_shell(model, edge_use, edge_dir_use)
    _finalize_geometry(model, ent, unit_scale, unit_name,
                       open_edges, unsupported_face_types)

    return model






def _collect_coord_lists(ent: dict) -> dict:
    """① COORDINATES_LIST：坐标点列表（引用 CARTESIAN_POINT 或内联三元组）。"""
    points: dict[int, tuple] = {}
    for i, e in ent.items():
        if not e or e[0] != "COORDINATES_LIST":
            continue
        refs = _refs(A(e, 1))
        pts = []
        if refs:
            for r in refs:
                q = ent.get(r)
                if q and q[0] == "CARTESIAN_POINT":
                    pts.append(_v3(_point(q)))
        else:
            # 内联：((x,y,z),(x,y,z),...)
            for grp in re.findall(r"\(([^()]*)\)", A(e, 1)):
                c = _tess_float_list(grp)
                if len(c) >= 3:
                    pts.append((c[0], c[1], c[2]))
        if pts:
            points[i] = pts
    return points


def _face_set_triangles(ent: dict, points: dict, tris: list, missing: list) -> None:
    """② TRIANGULATED_FACE_SET / COMPLEX_TRIANGULATED_FACE_SET。

    tris 追加解析出的三角形；missing[0] 累加索引缺失计数。
    """
    for _eid, e in ent.items():
        if not e or e[0] not in ("TRIANGULATED_FACE_SET", "COMPLEX_TRIANGULATED_FACE_SET"):
            continue
        coord_refs = _refs(A(e, 1))
        coords = points.get(coord_refs[0]) if coord_refs else None
        # 参数位置随导出器而变：
        #   标准：('', #coords, pnmax, normals, ((1,2,3),...), $)  → 第 4 参
        #   省略可选：('', #coords, ((1,2,3),...))               → 第 2 参
        #   内联坐标：('', $, ((x,y,z),...), ((1,2,3),...))
        # 因此不写死下标，而是挑"第一个含嵌套三元组的参数"作为三角形列表。
        tri_tok = ""
        for tok in e[2:]:
            st = str(tok)
            if "(" in st and ")" in st and "," in st and                     re.search(r"\(\s*-?\d+\s*,", st):
                tri_tok = st
                break
        if not tri_tok:
            tri_tok = A(e, 4)
        idx = _tess_int_triplets(tri_tok)
        if not coords and not coord_refs:
            # 坐标内联在 face_set 自身：('', $, ((x,y,z),(x,y,z),...), ((1,2,3),...))
            # 关键：只在"整包坐标参数"里取三元组，且要求带小数点（STEP 浮点写法），
            # 否则会把三角形索引 (1,2,3) 误当成坐标（实测踩过）。
            for tok in e[2:]:
                st = str(tok)
                if st.count("(") < 2 or ".," not in st.replace(" ", ""):
                    continue
                cand = []
                for grp in re.findall(r"\(([^()]*)\)", st):
                    if "." not in grp and "e" not in grp.lower():
                        continue
                    c = _tess_float_list(grp)
                    if len(c) >= 3:
                        cand.append((c[0], c[1], c[2]))
                if len(cand) >= 3:
                    coords = cand
                    break
            idx = [tri for tri in idx
                   if all(1 <= n <= len(coords or []) for n in tri)]
        if not coords:
            # 坐标引用缺失（指向不存在的实体）或坐标列表为空：计入缺失，
            # 让调用方知道"文件里有三角面定义但坐标不可用"，而不是空洞的 G-NO-FACE。
            missing[0] += max(1, len(idx))
            continue
        n = len(coords)
        for a, b, c in idx:
            if not (1 <= a <= n and 1 <= b <= n and 1 <= c <= n):
                missing[0] += 1
                continue
            tris.append((coords[a - 1], coords[b - 1], coords[c - 1]))


def _single_triangles(ent: dict, tris: list) -> None:
    """③ TRIANGULATED_FACE（单三角形实体）：三个顶点引用直接成三角形。"""
    for _eid, e in ent.items():
        if not e or e[0] != "TRIANGULATED_FACE":
            continue
        refs = _refs(" ".join(str(x) for x in e[2:]))
        got = []
        for r in refs[:3]:
            q = ent.get(r)
            if q and q[0] == "CARTESIAN_POINT":
                got.append(_v3(_point(q)))
        if len(got) >= 3:
            tris.append((got[0], got[1], got[2]))


def _collect_triangles(ent: dict) -> tuple[list, int]:
    """从 AP242 网格实体里收集三角形 (a,b,c) 坐标与索引缺失计数。

    与 _read_tessellated 分开：B-rep 文件也可能同时带三角化表示（AP242 允许
    同时导出精确曲面与其网格），此时三角形不是独立的面，而是**同一批曲面**的
    离散数据，只能用来补那些无法求值的曲面（见 _apply_tessellated_to_faces）。
    """
    tris: list[tuple[tuple, tuple, tuple]] = []
    missing = [0]                      # list 装计数，供子阶段累加
    _face_set_triangles(ent, _collect_coord_lists(ent), tris, missing)
    _single_triangles(ent, tris)
    return tris, missing[0]


class _BoxGrid:
    """目标面包围盒的 XY 均匀网格索引（三角形归属查询用）。

    为什么需要：`_apply_tessellated_to_faces` 原实现对每个三角形线性扫描
    全部目标面的包围盒——O(T×F)。实测 T×F 配对斜率 1.03（纯二次）：
    30M 配对 2.1 s，160M 配对约 15 s。**混合 AP242 文件（B-rep 样条面 +
    三角化网格）正是这种形态**，20 MB 实测 23–35 s，超 STR-001 的 15 s。

    用 sqrt(F) 见方的网格按包围盒登记面，三角形只查自己重心所在的那一格。
    格的边长取"平均包围盒尺寸"，并对跨格的面登记到所有覆盖的格子
    （一个面最多登记到 4 格，实测平均 1.3 格）。

    正确性：与线性扫描**完全等价**——每个三角形仍然只从候选里取第一个
    "三维包围盒命中"的面（保留 `break` 语义），只是候选集缩小到同一格。
    极端情形（所有面都覆盖整个板面 → 全部登记在同一批格子）会退化为线性，
    但那意味着 F 个面互相重叠，本身就不合理。
    """

    __slots__ = ("_cells", "_nx", "_ny", "_x0", "_y0", "_cw", "_ch")

    def __init__(self, boxes: list, n_side: int | None = None):
        xs0 = [lo[0] for lo, _ in boxes]
        xs1 = [hi[0] for _, hi in boxes]
        ys0 = [lo[1] for lo, _ in boxes]
        ys1 = [hi[1] for _, hi in boxes]
        self._x0, self._y0 = min(xs0), min(ys0)
        w = max(max(xs1) - self._x0, 1e-9)
        h = max(max(ys1) - self._y0, 1e-9)
        n = n_side or max(1, int(len(boxes) ** 0.5))
        self._nx = self._ny = n
        self._cw = w / n
        self._ch = h / n
        self._cells: dict[tuple[int, int], list[int]] = {}
        for k, (lo, hi) in enumerate(boxes):
            c0 = self._cell(lo[0], lo[1])
            c1 = self._cell(hi[0], hi[1])
            for cx in range(c0[0], c1[0] + 1):
                for cy in range(c0[1], c1[1] + 1):
                    self._cells.setdefault((cx, cy), []).append(k)

    def _cell(self, x: float, y: float) -> tuple[int, int]:
        cx = int((x - self._x0) / self._cw) if self._cw else 0
        cy = int((y - self._y0) / self._ch) if self._ch else 0
        return (min(max(cx, 0), self._nx - 1),
                min(max(cy, 0), self._ny - 1))

    def candidates(self, x: float, y: float) -> list[int]:
        return self._cells.get(self._cell(x, y), ())


def _apply_tessellated_to_faces(model, tris: list) -> list:
    """用同一文件里的三角化表示替换"不支持曲面"的估算面积（STR-001/AP242）。

    只在**面已经解析出来**（B-rep 存在）但其中有无法求值的曲面时调用：
    这些三角形是那批曲面的离散数据，不是独立的面。按"三角形重心落在面的
    包围盒内"做归属（包围盒各轴外扩 1% + 1e-6），一个三角形只归一个面。
    返回被替换的 Face 对象列表（调用方据此更新未解决曲面统计）。

    为什么用包围盒而不是精确的曲面求交：本解析器不做曲面求值，精确归属需要
    求解曲面上点与三角形的关系；包围盒归属可能把邻近面漏掉或错分，因此
    结果只标 G-SURFACE-APPROX（面积可信、但不保证逐面精确对应）。

    归属查询走 `_BoxGrid` 空间索引：原实现是 O(T×F) 全配对，混合 AP242
    文件（样条面 + 网格）20 MB 要 23–35 s，超 STR-001 的 15 s 上限。
    """
    targets = [f for f in model.faces if f.surf == "other" and len(f.loop) >= 3]
    if not targets or not tris:
        return []
    boxes = []
    for f in targets:
        xs = [p[0] for p in f.loop]
        ys = [p[1] for p in f.loop]
        zs = [p[2] for p in f.loop]
        pad = [1e-6 + 0.01 * (max(v) - min(v)) for v in (xs, ys, zs)]
        boxes.append(((min(xs) - pad[0], min(ys) - pad[1], min(zs) - pad[2]),
                      (max(xs) + pad[0], max(ys) + pad[1], max(zs) + pad[2])))
    grid = _BoxGrid(boxes) if len(boxes) > 16 else None
    areas = [0.0] * len(targets)
    for a, b, c in tris:
        nx = v_cross(v_sub(b, a), v_sub(c, a))
        tri_area = v_len(nx) / 2.0
        if tri_area < 1e-12:
            continue
        cx = (a[0] + b[0] + c[0]) / 3.0
        cy = (a[1] + b[1] + c[1]) / 3.0
        cz = (a[2] + b[2] + c[2]) / 3.0
        cand = grid.candidates(cx, cy) if grid is not None else range(len(boxes))
        for k in cand:
            lo, hi = boxes[k]
            if (lo[0] <= cx <= hi[0] and lo[1] <= cy <= hi[1]
                    and lo[2] <= cz <= hi[2]):
                areas[k] += tri_area
                break
    replaced: list = []
    for f, got in zip(targets, areas, strict=True):
        if got <= 0.0:
            continue
        f.area = got
        # 由"包围盒估算"升级为"三角化数据"，但归属仍是启发式 → 诚实标 APPROX
        diags = [d for d in (f.diagnostics or []) if d != "G-SURFACE-UNSUPPORTED"]
        diags.append("G-SURFACE-APPROX")
        f.diagnostics = sorted(set(diags))
        replaced.append(f)
    return replaced


def _read_tessellated(model, ent: dict, start_face_no: int = 0) -> int:
    """读取 AP242 三角化几何（STR-001）。

    STEP AP242 的网格几何有三种常见结构：

    ```
    #6=COORDINATES_LIST('',(#2,#3,#4,#5));
    #7=TRIANGULATED_FACE_SET('',#6,$,$,((1,2,3),(1,3,4)),$);
    #8=TESSELLATED_SOLID('',(#7),$);
    #9=TESSELLATED_SHAPE_REPRESENTATION('',(#8),#10);
    ```

    也支持：
    - `TESSELLATED_SHELL` 直接挂 face_set；
    - `TRIANGULATED_FACE(...)` 单三角形实体（带自己的坐标或引用）；
    - `COORDINATES_LIST` 内联 `((x,y,z),...)` 坐标。

    每个三角形产出一个 `Face`：surf="tessellated"，面积与法向按三角形实际几何算。
    顶点索引是 **1-based**（STEP 惯例），越界时跳过并记诊断。
    返回新增的面数。
    """
    tris, missing = _collect_triangles(ent)
    face_no = start_face_no
    for a, b, c in tris:
        # 边长退化（共线/重合）跳过：面积为零的三角形对坡度与板框都无意义
        ab, ac = v_sub(b, a), v_sub(c, a)
        nx = v_cross(ab, ac)
        area = v_len(nx) / 2.0
        if area < 1e-12:
            continue
        face_no += 1
        model.faces.append(Face(
            fid=f"F{face_no}", surf="tessellated",
            normal=v_unit(nx), area=area,
            centroid=((a[0] + b[0] + c[0]) / 3.0,
                      (a[1] + b[1] + c[1]) / 3.0,
                      (a[2] + b[2] + c[2]) / 3.0),
            loop=[a, b, c],
            radius=None, diagnostics=[],
        ))
    if model.faces:
        model.diagnostics.append({
            "code": "G-TESSELLATED-GEOMETRY",
            "msg": f"该文件为三角化网格（AP242），已按 {len(model.faces)} 个三角面片读取",
            "advice": "网格几何只有三角面片，没有精确曲面；面积/体积为三角近似，"
                      "曲面精度取决于导出时的细分级別"})
    if missing:
        model.diagnostics.append({
            "code": "G-TESS-INDEX-INVALID",
            "msg": f"{missing} 处三角形索引越界或坐标缺失，已跳过",
            "advice": "文件可能被截断或导出器有缺陷；请在 CAD 中重新导出网格"})
    return face_no - start_face_no


























def _group_faces_by_connectivity(faces: list[list[tuple]]) -> list[int]:
    """按共享顶点把面分组，返回每组的面数（用于写出多个实体）。

    两个面只要有一个坐标相同的顶点即视为连通（对平面多面体足够）；
    完全不含共点的面属于不同实体。装配体展平后靠这个区分零件。
    """
    if not faces:
        return []
    keys = []
    for f in faces:
        keys.append({tuple(round(c, 6) for c in p) for p in f})
    n = len(faces)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    owner: dict[tuple, int] = {}
    for i, ks in enumerate(keys):
        for k in ks:
            if k in owner:
                union(owner[k], i)
            else:
                owner[k] = i

    counts: dict[int, int] = {}
    for i in range(n):
        r = find(i)
        counts[r] = counts.get(r, 0) + 1
    # 按首次出现顺序返回，保证输出稳定
    order = []
    seen = set()
    for i in range(n):
        r = find(i)
        if r not in seen:
            seen.add(r)
            order.append(counts[r])
    return order


class StepBuilder:
    """生成平面多面体的最小 AP203 文件（样例与测试用）。

    点/线/面实体按依赖顺序编号；文件可被本包 read_step 正确读回。
    """

    def __init__(self, unit: str = "mm"):
        self.unit = unit

    def build(self, faces: list[list[tuple]], name: str = "PART") -> str:
        lines: list[str] = []
        nid = 9  # 头部占用 #1..#9

        def addf(body: str) -> int:
            nonlocal nid
            nid += 1
            lines.append(f"#{nid}={body};")
            return nid

        pid: dict[tuple, int] = {}

        def point_id(p: tuple) -> int:
            q = tuple(round(c, 6) for c in p)
            if q not in pid:
                pid[q] = addf(f"CARTESIAN_POINT('',({q[0]:.6f},{q[1]:.6f},{q[2]:.6f}))")
            return pid[q]

        def direction_id(d: tuple) -> int:
            u = v_unit(d)
            return addf(f"DIRECTION('',({u[0]:.6f},{u[1]:.6f},{u[2]:.6f}))")

        edge_ids: dict[tuple, int] = {}

        def edge_id(a: tuple, b: tuple) -> int:
            key = (a, b) if a <= b else (b, a)
            if key in edge_ids:
                return edge_ids[key]
            pa = addf(f"VERTEX_POINT('',#{point_id(key[0])})")
            pb = addf(f"VERTEX_POINT('',#{point_id(key[1])})")
            vec = addf(f"VECTOR('',#{direction_id(v_sub(key[1], key[0]))},1.)")
            ln = addf(f"LINE('',#{point_id(key[0])},#{vec})")
            ec = addf(f"EDGE_CURVE('',#{pa},#{pb},#{ln},.T.)")
            edge_ids[key] = ec
            return ec

        # 按顶点连通性把面分组成多个实体（STR-001：装配体展平后应保留实体数）
        groups = _group_faces_by_connectivity(faces)

        face_entity_ids = []
        for f in faces:
            loop = [tuple(round(c, 6) for c in p) for p in f]
            if len(loop) >= 2 and loop[0] == loop[-1]:
                loop.pop()
            loop = [k for i, k in enumerate(loop) if i == 0 or k != loop[i - 1]]
            if len(loop) < 3:
                continue
            oe_ids = []
            for i in range(len(loop)):
                ce = edge_id(loop[i], loop[(i + 1) % len(loop)])
                fwd = loop[i] <= loop[(i + 1) % len(loop)]
                oe_ids.append(addf(f"ORIENTED_EDGE('',*,*,#{ce},.T.)" if fwd
                                   else f"ORIENTED_EDGE('',*,*,#{ce},.F.)"))
            lp = addf(f"EDGE_LOOP('',({','.join('#%d' % i for i in oe_ids)}))")
            bound = addf(f"FACE_OUTER_BOUND('',#{lp},.T.)")
            n = v_unit(_face_normal(loop))
            az = direction_id(n)
            ax = direction_id((1.0, 0.0, 0.0))
            place = addf(f"AXIS2_PLACEMENT_3D('',#{point_id(loop[0])},#{az},#{ax})")
            plane = addf(f"PLANE('',#{place})")
            face_entity_ids.append(addf(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)"))

        # 每个连通分量写成独立的 CLOSED_SHELL + MANIFOLD_SOLID_BREP，
        # 这样装配体（多个不相连实体）读回后 solid_count 才正确。
        if not groups:
            shell = addf(f"CLOSED_SHELL('',({','.join('#%d' % i for i in face_entity_ids)}))")
            addf(f"MANIFOLD_SOLID_BREP('',#{shell})")
        else:
            idx = 0
            for size in groups:
                ids = face_entity_ids[idx:idx + size]
                idx += size
                if not ids:
                    continue
                shell = addf(f"CLOSED_SHELL('',({','.join('#%d' % i for i in ids)}))")
                addf(f"MANIFOLD_SOLID_BREP('',#{shell})")

        header = [
            "ISO-10303-21;", "HEADER;",
            "FILE_DESCRIPTION(('AME sample'),'2;1');",
            f"FILE_NAME('{name}.step','2026-10-25',('AME'),('AME'),'AME v1','','');",
            "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));",
            "ENDSEC;", "DATA;",
            "#1=APPLICATION_CONTEXT('core data for automotive mechanical design processes');",
            "#2=APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#1);",
            "#3=PRODUCT_DEFINITION_CONTEXT('part definition',#1,'design');",
            "#4=PRODUCT_DEFINITION_SHAPE('','',#5);",
            "#5=PRODUCT_DEFINITION('design','',#6,#3);",
            "#6=PRODUCT_DEFINITION_FORMATION('','',#7);",
            "#7=PRODUCT('part','',$(#8),#1);",
            "#8=PRODUCT_CONTEXT('',#1,'mechanical');",
            "#9=SI_UNIT(.MILLI.,.METRE.);" if self.unit == "mm" else "#9=SI_UNIT(.INCH.,.METRE.);",
        ]
        return "\n".join(header + lines + ["ENDSEC;", "END-ISO-10303-21;", ""])


def _face_normal(pts: list[tuple]) -> tuple:
    n = (0.0, 0.0, 0.0)
    for i in range(1, len(pts) - 1):
        n = v_add(n, v_cross(v_sub(pts[i], pts[0]), v_sub(pts[i + 1], pts[0])))
    return n


# ---------------------------------------------------------------- 常用几何体
def box_faces(x0, y0, z0, dx, dy, dz) -> list[list[tuple]]:
    """轴对齐长方体的六个面（外法向）。"""
    b = [(x0, y0, z0), (x0 + dx, y0, z0), (x0 + dx, y0 + dy, z0), (x0, y0 + dy, z0)]
    t = [(x, y, z + dz) for x, y, z in b]
    # 点序须满足右手定则、法向朝外；否则有向体积为负、渲染时被背面剔除。
    # 原实现 y=y0 与 y=y1 两侧的点序反了（法向朝内），导致体积算错、诊断误报。
    return [
        list(reversed(b)),            # 底面 z=z0  法向 -Z
        list(t),                      # 顶面 z=z1  法向 +Z
        [b[0], b[1], t[1], t[0]],     # y=y0       法向 -Y
        [b[1], b[2], t[2], t[1]],     # x=x1       法向 +X
        [b[2], b[3], t[3], t[2]],     # y=y1       法向 +Y
        [b[3], b[0], t[0], t[3]],     # x=x0       法向 -X
    ]


def prism_faces(profile: list[tuple], y0: float, y1: float) -> list[list[tuple]]:
    """沿 Y 轴拉伸 XZ 轮廓，生成**外法向朝外**的封闭多面体（楔形/斜面样本）。

    参数
    ----
    profile : [(x, z), ...] 封闭轮廓顶点（顺序任意，内部统一为逆时针）

    说明
    ----
    点序必须满足右手定则、法向朝外，否则有向体积为负 —— 表现为渲染时整片被
    背面剔除、内外判定反、G-NORMAL-INWARD 误报。

    这里的点序由「有向体积必须为正」反推验证（不是靠几何直觉）：
      端盖 y0 按轮廓正序、y1 反序；侧面统一取 (a@y1, b@y1, b@y0, a@y0)。
    验证：10×5 轮廓拉伸 10mm → 体积 +500 mm³。
    """
    if len(profile) < 3:
        return []
    # 轮廓绕向（XZ 平面有向面积）：>0 为逆时针；统一成逆时针再排列
    area2 = sum(profile[i][0] * profile[(i + 1) % len(profile)][1]
                - profile[(i + 1) % len(profile)][0] * profile[i][1]
                for i in range(len(profile)))
    prof = list(profile) if area2 > 0 else list(reversed(profile))

    faces = [[(x, y0, z) for x, z in prof],
             [(x, y1, z) for x, z in reversed(prof)]]
    n = len(prof)
    for i in range(n):
        a, b = prof[i], prof[(i + 1) % n]
        faces.append([(a[0], y1, a[1]), (b[0], y1, b[1]),
                      (b[0], y0, b[1]), (a[0], y0, a[1])])
    return faces
