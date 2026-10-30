"""STEP 解析的阶段函数族（从 stepio.py 拆出，2026-10-29）。

read_step 的编排器在 stepio.py；本模块承载它的各解析阶段，依赖方向严格
单向：stepio → step_build → {syntax, vec3, types, surfaces}（叶子模块）。

- 单位识别：CONVERSION_BASED_UNIT / SI_UNIT / 原文兜底三策略
- 几何构建：点/方向、轴放置、曲面、曲线、边几何
- 面装配：环遍历（含圆弧离散）、面积求值（含诚实降级）、Face 列表
- 壳检查与收尾：水密/绕向、诊断汇总、单位换算、bbox、体积、量级提示
"""
from __future__ import annotations

import math
import re

from .syntax import A, _num, _point, _ref, _refs
from .surfaces import (_cone_face, _fallback_face, _ring_area, _sphere_face,
                       _surface_type_name, _torus_face)
from .types import Face, StepModel
from .vec3 import (_v3, centroid_3d, polygon_area_3d, v_cross, v_dot,
                   v_sub, v_unit)

def _unit_from_conversion(ent: dict, text: str):
    """策略①：CONVERSION_BASED_UNIT（显式换算单位）——真实 CAD 的主要写法。

    LENGTH_MEASURE_WITH_UNIT 里的数值是"多少个**基准 SI 单位**"，基准可能是
    毫米或米：proe.stp 给 2.54E1 且基准 .MILLI. → 25.4；feature.stp 给 0.0254
    且基准 .METRE. → 0.0254（米）。找不到数值时按名称给公称值。
    命中返回 (scale, name)，否则返回 None。
    """
    # （性能审计发现的死代码：原 here 有两份 `length_measures` 收集循环——
    # 字典从未被读取，20 MB 文件上白扫两遍全表。已删。）
    # 优先级：CONVERSION_BASED_UNIT（显式换算单位） > SI_UNIT（基准单位）。
    # 真实英制文件同时存在 SI_UNIT($,.METRE.) 与 CONVERSION_BASED_UNIT('INCH')，
    # 前者只是基准声明、后者才是实际单位；先判前者会把英寸件当米（差 25.4 倍）。
    for e in ent.values():
        if e[0] != "CONVERSION_BASED_UNIT" or len(e) < 3:
            continue
        name = str(e[1]).strip().strip("'").upper()
        ref = _ref(e[2])
        # LENGTH_MEASURE_WITH_UNIT 里的数值是"多少个**基准 SI 单位**"，
        # 基准可能是毫米或米，必须先看它引用的 SI_UNIT 才能定换算因子。
        #   proe.stp: LENGTH_MEASURE(2.54E1) 且基准 SI_UNIT(.MILLI.,.METRE.) → 25.4 mm/英寸
        #   feature.stp: LENGTH_MEASURE(0.0254) 且基准 SI_UNIT($,.METRE.) → 0.0254 m/英寸
        raw_val = None
        base_scale = 1.0        # 基准 SI 单位相对毫米
        if ref is not None:
            mstmt = re.search(rf"#{ref}\s*=\s*[^;]*?LENGTH_MEASURE\s*\(\s*"
                              rf"([-\d.eE+]+)\s*\)\s*,\s*#(\d+)", text, re.S)
            if mstmt:
                raw_val = _num(mstmt.group(1), 0.0)
                base_ref = int(mstmt.group(2))
                bstmt = re.search(rf"#{base_ref}\s*=\s*[^;]*?(SI_UNIT\s*\([^;]*\))",
                                  text, re.S)
                if bstmt:
                    b = bstmt.group(1)
                    if ".MILLI." in b:
                        base_scale = 1.0        # 基准 mm
                    elif ".CENTI." in b:
                        base_scale = 10.0       # 基准 cm
                    else:
                        base_scale = 1000.0     # 基准 m
        if raw_val is None:
            # 无法读到数值：按名称给公称值（英寸基准默认 mm）
            if name in ("INCH", "IN"):
                raw_val, base_scale = 25.4, 1.0
        if raw_val and raw_val > 0:
            return raw_val * base_scale, name.lower() or "converted"

    return None



def _unit_from_si(ent: dict):
    """策略②：SI_UNIT 前缀（公制声明）。命中返回 (scale, name)。"""
    # ① SI_UNIT：ent[1]=前缀, ent[2]=单位（无名称参数）
    for e in ent.values():
        if e[0] == "SI_UNIT" and len(e) > 2                     and str(e[2]).strip().upper() == ".METRE.":
            prefix = str(e[1]).strip().upper()
            if prefix == ".MILLI.":
                return 1.0, "mm"
            if prefix == ".CENTI.":
                return 10.0, "cm"
            if prefix in (".INCH.", ".INCH"):
                return 25.4, "inch"
            # '$'（无前缀）与其他未知前缀都按基准单位 m 处理
            return 1000.0, "m"

    return None



def _unit_from_text_fallback(ent: dict, text: str):
    """策略③兜底：CONVERSION_BASED_UNIT 引用的 #m 不在 entities 里（复杂实例
    子实体）时，直接从原文按 id 定位 LENGTH_MEASURE 数值。命中返回 (scale, name)。
    """
    # ②b 兜底：CONVERSION_BASED_UNIT 引用的 #m 若不在 entities 里（复杂实例子实体），
    #     直接从原文按 id 定位并解析 LENGTH_MEASURE 数值。
    for e in ent.values():
        if e[0] != "CONVERSION_BASED_UNIT" or len(e) < 3:
            continue
        name = str(e[1]).strip().strip("'").upper()
        ref = _ref(e[2])
        if ref is None:
            continue
        mstmt = re.search(rf"#{ref}\s*=\s*[^;]*?LENGTH_MEASURE\s*\(\s*"
                          rf"([-\d.eE+]+)\s*\)", text, re.S)
        factor_m = _num(mstmt.group(1), 0.0) if mstmt else None
        if factor_m is None and name in ("INCH", "IN"):
            factor_m = 0.0254
        if factor_m is None or factor_m <= 0:
            continue
        return factor_m * 1000.0, name.lower() or "converted"

    return None



def _detect_length_unit(ent: dict, text: str) -> tuple[float, str, bool]:
    """识别长度单位，返回（相对 mm 的换算因子, 单位名, 是否找到）。

    两种声明方式都要支持，真实 CAD 以前者为主：
      ① SI_UNIT(.MILLI.,.METRE.)                          —— 公制
      ② #n=(CONVERSION_BASED_UNIT('INCH',#m) LENGTH_UNIT() NAMED_UNIT(...))
         配合 LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(0.0254),...) —— 英制
    只认 ① 会把所有英寸件当毫米（尺寸差 25.4 倍且不报错）。
    """
    for probe in (_unit_from_conversion(ent, text),
                  _unit_from_si(ent),
                  _unit_from_text_fallback(ent, text)):
        if probe is not None:
            return probe[0], probe[1], True
    return 1.0, "mm", False



def _build_point_dirs(ent: dict) -> tuple[dict, dict]:
    """收集 CARTESIAN_POINT 与 DIRECTION（少于 3 分量的方向补 0，零向量给 +Z）。"""
    pts: dict[int, tuple] = {i: _point(e) for i, e in ent.items()
                             if e[0] == "CARTESIAN_POINT"}
    dirs: dict[int, tuple] = {}
    for i, e in ent.items():
        if e[0] == "DIRECTION":
            raw = A(e, 1).strip("()").split(",")
            # 参数可能少于 3 个（如 DIRECTION('',(0.,1.)) → z 缺省 0）
            d = _v3(tuple(_num(x) for x in raw))
            dirs[i] = d if any(d) else (0.0, 0.0, 1.0)
    return pts, dirs



def _make_placement(ent: dict, pts: dict, dirs: dict):
    """轴放置查表：AXIS2_PLACEMENT_3D('',loc,axis,ref) -> (loc, z轴, x轴)。"""

    def placement(i):
        e = ent.get(i)
        if not e or e[0] != "AXIS2_PLACEMENT_3D":
            return ((0, 0, 0), (0, 0, 1), (1, 0, 0))
        loc = pts.get(_ref(A(e, 1)), (0, 0, 0))
        z = dirs.get(_ref(A(e, 2)), (0, 0, 1))
        x = dirs.get(_ref(A(e, 3)), (1, 0, 0))
        return (loc, z, x)

    return placement



def _build_surfaces(ent: dict, placement) -> dict:
    """face 曲面表：face_id -> (kind, axis/normal, loc, radius, params)。

    plane/cylinder 的 params 为 None；
    cone   params = {"semi": 半锥角(rad), "x": 参考方向}；
    sphere params = {"x": 参考方向}；
    torus  params = {"major": 主半径 R, "x": 参考方向}。
    """
    surfaces = {}
    for i, e in ent.items():
        if e[0] == "PLANE":
            loc, z, _ = placement(_ref(A(e, 1)))
            surfaces[i] = ("plane", v_unit(z), loc, None, None)
        elif e[0] == "CYLINDRICAL_SURFACE":
            loc, z, _ = placement(_ref(A(e, 1)))
            r = _num(A(e, 2))
            surfaces[i] = ("cylinder", v_unit(z), loc, r, None)
        elif e[0] == "CONICAL_SURFACE":
            # CONICAL_SURFACE(name, position, radius, semi_angle)
            # radius 是"过 position 原点、垂直于轴"那一圈的半径，半径沿 +axis
            # 按 tan(semi_angle) 增长（semi_angle 是与轴的夹角，不是与水平面）。
            loc, z, x = placement(_ref(A(e, 1)))
            surfaces[i] = ("cone", v_unit(z), loc, _num(A(e, 2)),
                           {"semi": _num(A(e, 3)), "x": x})
        elif e[0] == "SPHERICAL_SURFACE":
            # SPHERICAL_SURFACE(name, position, radius)；position 的 z 轴 = 南北极轴
            loc, z, x = placement(_ref(A(e, 1)))
            surfaces[i] = ("sphere", v_unit(z), loc, _num(A(e, 2)), {"x": x})
        elif e[0] == "TOROIDAL_SURFACE":
            # TOROIDAL_SURFACE(name, position, major_radius, minor_radius)
            loc, z, x = placement(_ref(A(e, 1)))
            surfaces[i] = ("torus", v_unit(z), loc, _num(A(e, 3)),
                           {"major": _num(A(e, 2)), "x": x})
        elif e[0] in ("B_SPLINE_SURFACE", "B_SPLINE_SURFACE_WITH_KNOTS",
                      "RATIONAL_B_SPLINE_SURFACE", "SURFACE_OF_REVOLUTION",
                      "SURFACE_OF_LINEAR_EXTRUSION", "OFFSET_SURFACE",
                      "BOUNDED_SURFACE", "RECTANGULAR_TRIMMED_SURFACE",
                      "CURVE_BOUNDED_SURFACE"):
            # 样条/自由曲面不做几何求值（见模块 docstring 的范围说明），
            # 但仍记录面片位置，免得后续诊断完全没有上下文。
            loc, z, _ = placement(_ref(A(e, 1)))
            surfaces[i] = ("other", v_unit(z), loc, None, None)
    return surfaces



def _build_curves(ent: dict, pts: dict, dirs: dict, placement) -> tuple[dict, dict]:
    """曲线表：CIRCLE -> (center, axis, radius)（圆弧离散用）；LINE -> (p0, dir)。"""
    circles: dict[int, tuple] = {}
    for i, e in ent.items():
        if e[0] == "CIRCLE":
            loc, z, _x = placement(_ref(A(e, 1)))
            r = _num(A(e, 2))
            circles[i] = (loc, v_unit(z), abs(r))

    lines: dict[int, tuple] = {}
    for i, e in ent.items():
        if e[0] == "LINE":
            p0 = pts.get(_ref(A(e, 1)), (0, 0, 0))
            vec = ent.get(_ref(A(e, 2)))
            d = dirs.get(_ref(A(vec, 1)), (1, 0, 0)) if vec else (1, 0, 0)
            lines[i] = (p0, v_unit(d))
    return circles, lines



def _build_edge_geom(ent: dict, pts: dict) -> dict:
    """EDGE_CURVE('',v1,v2,curve,sameSense) -> {id: (v1, v2, ctype, curve_ref)}。

    SURFACE_CURVE / SEAM_CURVE 是包装层，真实曲线在其第一参数。
    """
    edge_geom: dict[int, tuple] = {}
    for i, e in ent.items():
        if e[0] == "EDGE_CURVE":
            v1 = v2 = None
            for ref, idx in ((_ref(A(e, 1)), 0), (_ref(A(e, 2)), 1)):
                target = ent.get(ref)
                if not target:
                    continue
                if target[0] == "VERTEX_POINT":
                    pt_ref = _ref(A(target, 1))
                    p = pts.get(pt_ref)
                elif target[0] == "CARTESIAN_POINT":
                    p = pts.get(ref)
                else:
                    p = None
                if idx == 0:
                    v1 = p
                else:
                    v2 = p
            curve_ref = _ref(A(e, 3))
            curve = ent.get(curve_ref)
            ctype = curve[0] if curve else "?"
            # SURFACE_CURVE / SEAM_CURVE 是包装层，真实曲线在其第一参数
            if ctype in ("SURFACE_CURVE", "SEAM_CURVE") and curve:
                inner = ent.get(_ref(A(curve, 1)))
                if inner:
                    curve_ref = _ref(A(curve, 1))
                    ctype = inner[0]
            edge_geom[i] = (v1, v2, ctype, curve_ref)
    return edge_geom



def _edge_ring(bound: int, ent: dict, edge_geom: dict, circles: dict,
               edge_use: dict, edge_dir_use: dict, diags: list,
               max_sagitta: float) -> list:
    """走一个 FACE_BOUND 的 EDGE_LOOP，返回环顶点（含圆弧离散点）。

    有向边使用计数（edge_use/edge_dir_use）在此累计，供壳有效性检查用。
    """
    lp = ent.get(_ref(A(bound, 1)))
    if not lp or lp[0] != "EDGE_LOOP":
        return []
    ring_pts: list[tuple] = []
    for oeref in _refs(A(lp, 1)):
        oee = ent.get(oeref)
        if not oee or oee[0] != "ORIENTED_EDGE":
            continue
        ec_ref = _ref(A(oee, 3))
        ec = ent.get(ec_ref)
        if not ec:
            continue
        edge_use[ec_ref] = edge_use.get(ec_ref, 0) + 1
        rec = edge_dir_use.setdefault(ec_ref, {"fwd": 0, "rev": 0})
        geom = edge_geom.get(ec_ref, (None, None, "?", None))
        v1, v2, ctype = geom[0], geom[1], geom[2]
        curve_ref = geom[3] if len(geom) > 3 else None
        # ORIENTED_EDGE 第 5 参为方向：.F. 表示沿边曲线反方向遍历
        reversed_edge = A(oee, 4).strip().upper() == ".F."
        if reversed_edge:
            v1, v2 = v2, v1
            rec["rev"] += 1
        else:
            rec["fwd"] += 1
        arc_pts = None
        if ctype == "CIRCLE" and curve_ref in circles and v1 and v2:
            # 弦高容差是 mm 口径，而此处坐标仍是文件原始单位；
            # 英寸件必须把它折算成模型单位，否则离散精度会粗 25.4 倍
            # （实测同零件英寸版体积比毫米版小 0.63%）。
            arc_pts = _arc_points(circles[curve_ref], v1, v2, reversed_edge,
                                  max_sagitta=max_sagitta)
        elif ctype != "LINE":
            # 非直线且非圆弧（B 样条等）仍不支持，但已有诊断
            diags.append("G-CURVE-UNSUPPORTED")
        if arc_pts:
            # 圆弧离散：首点由前一段衔接，末尾点留给下一段衔接
            if not ring_pts or ring_pts[-1] != arc_pts[0]:
                ring_pts.append(arc_pts[0])
            ring_pts.extend(arc_pts[1:])
        else:
            if v1 is not None and (not ring_pts or ring_pts[-1] != v1):
                ring_pts.append(v1)
            if v2 is not None:
                ring_pts.append(v2)
    return ring_pts



def _surface_area(kind: str, normal: tuple, loc: tuple, radius,
                  sparams: dict | None, loop_pts: list, holes_pts: list,
                  face_no: int, surf_type: str, diags: list,
                  unsupported_face_types: dict) -> tuple:
    """按曲面类型求面积与中心；返回 (area, cen, normal)。

    求值不出的曲面记 G-SURFACE-UNSUPPORTED 并退回包围盒/三角形扇估算
    （STR-002：不静默）。环面/球面的解析结果本就是参数积分/推断，
    一律加 G-SURFACE-APPROX。
    """
    if kind == "plane":
        area = polygon_area_3d(loop_pts, normal)
        # 有孔洞时扣除内环面积（STR-004 面分析应给净面积）
        for h in holes_pts:
            if len(h) >= 3:
                area = max(0.0, area - polygon_area_3d(h, normal))
        cen = centroid_3d(loop_pts)
        # 平面度校验
        d = [abs(v_dot(v_sub(q, loc), normal)) for q in loop_pts]
        if d and max(d) > 0.05:
            diags.append("G-NONPLANAR-FACE")
        return area, cen, normal
    if kind == "cylinder":
        # 柱面面积 = 周长 × 轴向跨度。轴向跨度取环顶点在母线方向（loc→normal）
        # 上的投影范围；原实现写死 1.0 mm，导致面积与真实高度无关（可差数十倍）。
        r = radius or 0.0
        ax = v_unit(normal)          # 柱面母线方向
        proj = [v_dot(v_sub(q, loc), ax) for q in loop_pts]
        span = (max(proj) - min(proj)) if proj else 0.0
        if span <= 1e-9:
            # 单条圆边界（整圆柱面）：用 XY 方向的直径近似高度
            xs = [q[0] for q in loop_pts]
            ys = [q[1] for q in loop_pts]
            zs = [q[2] for q in loop_pts]
            span = max(max(xs) - min(xs), max(ys) - min(ys),
                       max(zs) - min(zs), 1e-3)
        return 2 * math.pi * r * span, centroid_3d(loop_pts), normal
    if kind == "cone":
        area, cen, normal, part = _cone_face(loop_pts, normal, loc, radius, sparams)
        if area <= 0.0:
            # 环界定不出轴向范围（如环整体落在同一高度）/越过锥顶 → 如实报不支持
            diags.append("G-SURFACE-UNSUPPORTED")
            unsupported_face_types[f"F{face_no}"] = surf_type
            area, cen, normal = _fallback_face(loop_pts, normal)
        elif part:
            # 部分圆周：面积按扇区解析式给出，但扇区范围是从环顶点反推的
            diags.append("G-SURFACE-APPROX")
        return area, cen, normal
    if kind == "sphere":
        area, cen, normal, part = _sphere_face(loop_pts, normal, loc, radius, sparams)
        if area <= 0.0:
            diags.append("G-SURFACE-UNSUPPORTED")
            unsupported_face_types[f"F{face_no}"] = surf_type
            area, cen, normal = _fallback_face(loop_pts, normal)
        elif part:
            # 单纬环/部分经度：面积解析但侧别与经度范围是推断的 → 诚实标近似
            diags.append("G-SURFACE-APPROX")
        return area, cen, normal
    if kind == "torus":
        area, cen, normal = _torus_face(loop_pts, normal, loc, radius, sparams)
        if area <= 0.0:
            diags.append("G-SURFACE-UNSUPPORTED")
            unsupported_face_types[f"F{face_no}"] = surf_type
            area, cen, normal = _fallback_face(loop_pts, normal)
        else:
            # 环面按 (u,v) 参数范围积分，不是精确裁剪面积 → 必须标近似
            diags.append("G-SURFACE-APPROX")
        return area, cen, normal
    area, cen, normal = _fallback_face(loop_pts, normal)
    # 面积是包围盒/三角形扇估算，不是精确曲面面积（STR-002 不静默）
    diags.append("G-SURFACE-UNSUPPORTED")
    unsupported_face_types[f"F{face_no}"] = surf_type
    return area, cen, normal



def _assemble_faces(ent: dict, surfaces: dict, circles: dict, edge_geom: dict,
                    tess_tol: float, unit_scale: float,
                    complex_parts: dict | None, complex_types: dict | None
                    ) -> tuple[list, dict, dict, dict, dict, int]:
    """ADVANCED_FACE 装配：环顶点 → 面积/中心 → Face 列表。

    返回 (faces, edge_use, edge_dir_use, unsupported_face_types, face_entity_id,
    face_no)。face_no 是 ADVANCED_FACE 计数，_read_tessellated 续接 F 编号时要用。
    外环 = 面积最大的环，其余为孔洞（内环）。
    """
    faces: list = []
    face_no = 0
    face_entity_id: dict[str, int] = {}   # Face.fid -> ADVANCED_FACE 实体号（装配展平用）
    edge_use: dict[int, int] = {}
    # 有向边使用记录：{ec_ref: {"fwd": n, "rev": n}}，用于绕向一致性检查
    edge_dir_use: dict[int, dict] = {}
    # 无法给出有效面片的曲面 → {Face.fid: 曲面类型名}（模型级诊断要说明是哪几类曲面）
    unsupported_face_types: dict[str, str] = {}
    for i, e in ent.items():
        if e[0] != "ADVANCED_FACE":
            continue
        face_no += 1
        surf_ref = _ref(e[3]) if len(e) > 3 else None
        kind, normal, loc, radius, sparams = surfaces.get(
            surf_ref, ("other", (0, 0, 1), (0, 0, 0), None, None))
        surf_type = _surface_type_name(ent, surf_ref, complex_parts, complex_types)
        diags: list[str] = []
        loop_pts: list[tuple] = []
        holes_pts: list[list[tuple]] = []
        for bref in _refs(e[2]) if len(e) > 2 else []:
            bound = ent.get(bref)
            if not bound or bound[0] not in ("FACE_OUTER_BOUND", "FACE_BOUND"):
                continue
            ring_pts = _edge_ring(bound, ent, edge_geom, circles,
                                  edge_use, edge_dir_use, diags,
                                  tess_tol / unit_scale)
            # 去掉重复末点
            if len(ring_pts) >= 2 and ring_pts[0] == ring_pts[-1]:
                ring_pts.pop()
            # 外环 = 面积最大的环；其余为孔洞（内环）
            if len(ring_pts) >= 3:
                if not loop_pts or _ring_area(ring_pts) > _ring_area(loop_pts):
                    if loop_pts:
                        holes_pts.append(loop_pts)
                    loop_pts = ring_pts
                else:
                    holes_pts.append(ring_pts)
            elif not loop_pts:
                loop_pts = ring_pts
        if len(loop_pts) < 3:
            diags.append("G-DEGEN-LOOP")
            area, cen = 0.0, centroid_3d(loop_pts or [(0, 0, 0)])
        else:
            area, cen, normal = _surface_area(
                kind, normal, loc, radius, sparams, loop_pts, holes_pts,
                face_no, surf_type, diags, unsupported_face_types)
            if area < 0.01:
                diags.append("G-TINY-FACE")
            # 自交只对**平面**面有意义：柱面的侧面环成"筒形"，投影到垂直法向的
            # 平面后必然是退化的线（实测 feature.stp 12 个柱面被误判自交）。
            if kind == "plane" and _loop_self_intersects(loop_pts, normal):
                diags.append("G-SELF-INTERSECT")
        faces.append(Face(f"F{face_no}", kind, normal, area, cen, loop_pts,
                          radius, sorted(set(diags)), holes_pts))
        face_entity_id[f"F{face_no}"] = i
    return (faces, edge_use, edge_dir_use, unsupported_face_types,
            face_entity_id, face_no)



def _check_shell(model, edge_use: dict, edge_dir_use: dict) -> list:
    """壳有效性检查（借鉴 trimesh 的 is_volume）：水密 + 绕向一致。

    返回单边列表，供体积校验判断"是否封闭实体"。
    法向朝外（第 3 项）在 _finalize_geometry 里做——体积要在单位换算后算，
    告警数值才能与标注单位一致。
    """
    # 1) 水密：每条边应被恰好 2 个有向边引用
    open_edges = [k for k, n in edge_use.items() if n == 1]
    if open_edges:
        model.diagnostics.append({"code": "G-OPEN-SHELL",
                                  "msg": f"壳存在 {len(open_edges)} 条单边（缺面或缝合失败）",
                                  "advice": "请在 CAD 中重新缝合后导出，本软件不静默修补"})

    # 2) 绕向一致：同一条边被两个面引用时，方向应相反；
    #    方向相同说明相邻面绕向矛盾（法向翻转），会导致法向朝内/外不一致
    bad_winding = sum(1 for k, n in edge_dir_use.items()
                      if n["fwd"] > 0 and n["rev"] > 0 and n["fwd"] != 1)
    if bad_winding:
        model.diagnostics.append({
            "code": "G-WINDING-INCONSISTENT",
            "msg": f"{bad_winding} 条边两侧面绕向相同（法向可能翻转）",
            "advice": "在 CAD 中统一实体法向后重新导出；本软件用绝对法向计算坡度，"
                      "但绕向不一致会影响体积与内外判定"})
    return open_edges



def _finalize_geometry(model, ent: dict, unit_scale: float, unit_name: str,
                       open_edges: list, unsupported_face_types: dict) -> None:
    """收尾：模型级诊断汇总、单位换算、bbox、体积与量级提示。"""
    # 法向朝外：封闭实体的有向体积应为正（散度定理）。
    # 为负说明整体内外翻转（曾出现 prism_faces 侧面法向朝内）。
    # 体积在单位换算之后计算，保证告警里的数值与标注单位一致。
    if unsupported_face_types:
        n_face = len(unsupported_face_types)
        types = "、".join(sorted(set(unsupported_face_types.values())))
        model.diagnostics.append({
            "code": "G-SURFACE-UNSUPPORTED",
            "msg": f"{n_face} 个面：不支持的曲面（{types}），面积按包围盒/三角形扇估算",
            "advice": "样条/自由曲面不做几何求值；如需精确面积请在 CAD 中转为平面、"
                      "柱面、圆锥或球面，或改用 AP242 三角化导出（届时按三角面片读取）",
            "face_count": n_face})
    model.solid_count = sum(1 for e in ent.values() if e[0] == "MANIFOLD_SOLID_BREP")

    if abs(unit_scale - 1.0) > 1e-9:
        _convert_inch_to_mm(model, factor=unit_scale)
        model.diagnostics.append({
            "code": "G-UNIT-INCH-CONVERTED",
            "msg": f"文件单位为 {unit_name}，已按 {unit_scale:g} 换算为毫米",
            "advice": "全软件统一 mm；如需原始尺寸请重新以毫米导出"})

    allp = [p for f in model.faces for p in f.loop] or [(0, 0, 0)]
    model.bbox = (min(p[0] for p in allp), min(p[1] for p in allp), min(p[2] for p in allp),
                  max(p[0] for p in allp), max(p[1] for p in allp), max(p[2] for p in allp))

    # ---- 面级诊断汇总到模型级（STR-002「不静默」） ----
    # 面级诊断只挂在 Face.diagnostics 上，用户界面上看不到。曾出现
    # occ214.stp 有 28 处 G-SURFACE-UNSUPPORTED / 28 处 G-TINY-FACE /
    # 18 处 G-SELF-INTERSECT / 41 处 G-CURVE-UNSUPPORTED，而 model.diagnostics
    # 完全为空——33/53 个面的面积是错的却零提示。
    _FACE_DIAG_TEXT = {
        "G-SURFACE-UNSUPPORTED": ("曲面类型不支持（样条/自由曲面），面积按包围盒/三角形扇估算",
                                  "如需精确面积请在 CAD 中转为平面、柱面、圆锥或球面，"
                                  "或改用 AP242 三角化导出（按三角面片读取）"),
        "G-SURFACE-APPROX": ("曲面已解析但为近似（环面参数积分 / 球冠侧别或部分经度按环推断），"
                             "面积与法向可用于分析，但不是精确裁剪结果",
                             "对精度敏感的场景请在 CAD 中把曲面细分为平面/柱面；"
                             "法向用于坡度判定时请人工复核该面"),
        "G-CURVE-UNSUPPORTED": ("环含不支持的曲线（样条等），面积按包围盒估算",
                                "同上：在 CAD 中把轮廓转成直线/圆弧，或改用三角化导出"),
        "G-TINY-FACE": ("存在极小面（面积 < 0.01 mm²）",
                        "极小面通常是导出误差或退化几何，建议在 CAD 中清理"),
        "G-SELF-INTERSECT": ("面的环投影自交，法向与面积可能不正确",
                             "请在 CAD 中检查该面是否有重叠/折返边界"),
        "G-DEGEN-LOOP": ("存在退化环（顶点数 < 3），该面无法形成有效区域",
                         "请在 CAD 中重新缝合或删除退化面"),
    }
    face_counts: dict[str, int] = {}
    for f in model.faces:
        for code in set(f.diagnostics or []):
            face_counts[code] = face_counts.get(code, 0) + 1
    already = {d["code"] for d in model.diagnostics}
    for code, n in sorted(face_counts.items()):
        if code in already:
            continue
        text = _FACE_DIAG_TEXT.get(code)
        if not text:
            continue
        msg, advice = text
        model.diagnostics.append({
            "code": code,
            "msg": f"{n} 个面：{msg}",
            "advice": advice,
            "face_count": n,
        })

    # 体积（散度定理）放在单位换算与 bbox 重建之后：数值与 mm³ 标注一致
    if len(model.faces) >= 4 and not open_edges:
        vol = _signed_volume(model.faces)
        if vol < 0:
            model.diagnostics.append({
                "code": "G-NORMAL-INWARD",
                "msg": f"实体有向体积为负（{vol:.1f} mm³），法向整体朝内",
                "advice": "请在 CAD 中翻转法向后重新导出；"
                          "本软件按绝对法向处理，但内外方向会与预期相反"})
        model.volume_mm3 = abs(vol)

    # 量级合理性提示：换算后尺寸异常大/小，通常意味着上游 CAD 的数值与单位标注不一致
    # （实测有零件导出的坐标本是毫米、却声明为英寸，换算后变成 3.5 m 级）。
    # 不擅自纠正单位——按文件声明换算是正确行为；这里只提示用户核对。
    span = max(model.bbox[3] - model.bbox[0], model.bbox[4] - model.bbox[1],
               model.bbox[5] - model.bbox[2])
    if span > 1000.0:
        model.diagnostics.append({
            "code": "G-SIZE-SUSPICIOUS",
            "msg": f"换算后最大尺寸 {span:.0f} mm，量级异常（通常 < 1000 mm）",
            "advice": "请核对 CAD 导出时的单位设置：文件声明单位与坐标数值可能不一致；"
                      "本软件按文件声明换算，不做猜测性纠正"})
    elif 0 < span < 0.5:
        model.diagnostics.append({
            "code": "G-SIZE-SUSPICIOUS",
            "msg": f"换算后最大尺寸 {span:.3f} mm，量级异常偏小",
            "advice": "请核对 CAD 导出单位是否被误设为更大单位（如把 mm 标成 m）"})



def _segments_cross(a, b, c, d) -> bool:
    """两条线段是否**严格内部**相交（端点接触、共线重叠都不算）。

    STEP 里相邻边本来就共享端点，把它当"自交"会大量误报。
    """
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    return (((d1 > 0 > d2) or (d1 < 0 < d2))
            and ((d3 > 0 > d4) or (d3 < 0 < d4)))


def _convert_inch_to_mm(model: StepModel, factor: float = 25.4):
    """把按英寸解析的模型整体换算为毫米（PRJ-008：全软件统一 mm）。"""
    for f in model.faces:
        f.loop = [tuple(c * factor for c in p) for p in f.loop]
        f.centroid = tuple(c * factor for c in f.centroid)
        f.area = f.area * factor * factor
        if f.radius:
            f.radius *= factor
        # 内环（孔洞）同样要换算，否则带孔零件的孔位置/尺寸会错 25.4 倍
        f.holes = [[tuple(c * factor for c in p) for p in ring] for ring in f.holes]
    b = model.bbox
    model.bbox = tuple(v * factor for v in b)
    # 体积是三次量：本函数在体积计算之前调用，故此处仅作防御性处理
    model.volume_mm3 *= factor ** 3
    model.unit = "mm"



def _loop_self_intersects(pts, normal) -> bool:
    """环在面法向投影上是否自交。

    **性能关键路径**：每个面都会调一次。实测 12000 面模型里这一步占 1.4 s，
    耗时几乎全在构造 shapely LinearRing 上。按顶点数分级处理：

    - **< 4 点**：三角形及退化环不可能自交 → 直接 False，不构造任何对象；
    - **== 4 点**：四边形只有"蝴蝶结"一种自交形态，两条对角线相交即自交。
      用叉积符号判定，纯算术，比 shapely 快两个数量级；
    - **≥ 5 点**：形态复杂，交给 shapely 的 ``is_simple``（可靠优先）。

    实测顶点分布：合成板件模型 100% 是四边形；真实文件里
    feature.stp 有 32/96 个四边形、occ214.stp 有 46/53 个——快路径覆盖绝大多数。

    判定结果与"总是调 shapely"完全一致（见 tests/test_stepio_perf.py 的
    等价性测试），只是不再为简单多边形付构造开销。
    """
    n_pts = len(pts)
    if n_pts < 3:
        return False          # 少于 3 点构不成环（退化数据，NFR-002 不崩）

    try:
        n = v_unit(normal)
        u = v_unit(v_cross(n, (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)))
        w = v_cross(n, u)
        p0 = pts[0]
        ring = [(v_dot(v_sub(p, p0), u), v_dot(v_sub(p, p0), w)) for p in pts]

        if n_pts == 4:
            # **投影退化检查**：若环在这个投影平面上退化成一条线
            # （所有点共线），几何上不存在"自交"概念，但 shapely 的
            # Ring.is_simple 会判 False。为了与旧实现保持完全一致，
            # 退化时回退到 shapely，不用快路径。
            #
            # 实测触发场景：法向为 (1,0,0) 这类垂直于 XY 的侧壁面。
            xs = [q[0] for q in ring]
            ys = [q[1] for q in ring]
            degenerate = (max(xs) - min(xs) < 1e-9) or (max(ys) - min(ys) < 1e-9)
            if not degenerate:
                # 四边形自交 ⇔ 边 (0,1)×(2,3) 或 (1,2)×(3,0) 严格相交
                return (_segments_cross(ring[0], ring[1], ring[2], ring[3])
                        or _segments_cross(ring[1], ring[2], ring[3], ring[0]))

        from shapely.geometry import LinearRing
        return not LinearRing(ring).is_simple
    except Exception:
        return False




# ---------------------------------------------------------------- 写出器



def _signed_volume(faces) -> float:
    """按散度定理算有向体积：Σ (1/6)·p0·(p1×p2) 三角化后求和。

    封闭实体体积为正表示法向朝外；为负说明整体内外翻转。
    仅对平面三角形/多边形有效（本项目首版范围）。
    """
    # 散度定理：V = Σ (1/6)·a·(b×c)，其中 a,b,c 为三角面的三个顶点（相对原点）。
    # 多边形用扇形三角化，但每个三角形的三个顶点都必须取**绝对坐标**，
    # 不能把 p0 提到循环外当作恒定点（那样只对过原点的三角形正确）。
    total = 0.0
    for f in faces:
        loop = f.loop
        if len(loop) < 3:
            continue
        for i in range(1, len(loop) - 1):
            a, b, c = loop[0], loop[i], loop[i + 1]
            total += v_dot(a, v_cross(b, c)) / 6.0
    return total



def _arc_points(circle: tuple, p_start, p_end, reversed_edge: bool,
                max_sagitta: float = 0.02) -> list[tuple]:
    """把一段圆弧离散成折线点（含首尾）。

    circle = (center, axis, radius)。段数按弦高误差自适应：
    sagitta = r(1-cos(θ/2)) ≤ max_sagitta（默认 0.02 mm，远低于机器定位精度）。
    首尾顶点由 STEP 保证在圆上，这里用它们定角度范围，避免圆心/半径的舍入误差。
    """
    center, axis, r = circle
    if r <= 1e-9 or p_start is None or p_end is None:
        return []
    axis = v_unit(axis)
    # 在圆平面内建立基向量
    ref = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = v_unit(v_cross(axis, ref))
    w = v_cross(axis, u)

    def angle(p):
        d = v_sub(p, center)
        return math.atan2(v_dot(d, w), v_dot(d, u))

    a0, a1 = angle(p_start), angle(p_end)
    same = math.hypot(p_start[0] - p_end[0], p_start[1] - p_end[1],
                      p_start[2] - p_end[2]) < 1e-9
    # 整圆（首尾同点）：走满 2π
    sweep = 2 * math.pi if same else (a1 - a0)
    if reversed_edge and not same:
        sweep = sweep
    # 归一到 (0, 2π]（STEP 的 EDGE_CURVE 默认沿曲线正向取较短的弧，
    # 但圆孔整圈由多条边拼成，单条边一般 < π）
    while sweep <= 0:
        sweep += 2 * math.pi
    while sweep > 2 * math.pi:
        sweep -= 2 * math.pi
    if sweep < 1e-9:
        sweep = 2 * math.pi
    # 段数：由弦高误差定
    if max_sagitta <= 0 or max_sagitta >= r:
        n = 8
    else:
        # θ ≤ 2*acos(1 - s/r)
        step = 2 * math.acos(max(-1.0, min(1.0, 1.0 - max_sagitta / r)))
        n = max(2, min(256, int(math.ceil(sweep / max(step, 1e-6)))))
    # 增量三角递推：cos(a+Δ)、sin(a+Δ) 由当前值一次旋转得到，
    # 避免每个离散点各调一次 math.cos + math.sin。
    #
    # 实测（proe.stp，112 次调用 / 10 240 点）：9.51 ms → 3.83 ms（2.48×）；
    # 18 MB 放大件上 `_assemble_faces` 4.17 s → 3.44 s（-17%）。
    # 与逐点 cos/sin 的最大绝对误差 2.84e-14（浮点噪声级，远低于 0.02 mm 的
    # 弦高容差）；段数上限 256，递推误差不会累积到可观测水平。
    step = sweep / n
    cs, sn = math.cos(step), math.sin(step)
    ca, sa = math.cos(a0), math.sin(a0)
    # 从 k=1 开始：先推进一次，再逐点生成
    pts = [tuple(p_start)]
    cx, cy, cz = center
    ux, uy, uz = u
    wx, wy, wz = w
    for _ in range(1, n):
        ca, sa = ca * cs - sa * sn, sa * cs + ca * sn
        pts.append((cx + r * (ca * ux + sa * wx),
                    cy + r * (ca * uy + sa * wy),
                    cz + r * (ca * uz + sa * wz)))
    pts.append(tuple(p_end))
    return pts


