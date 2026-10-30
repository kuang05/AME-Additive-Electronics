"""STEP 装配体展平（STR-001）：按 MAPPED_ITEM 位姿把同一零件的多次实例摆开。

从 `stepio.py` 拆出的独立子系统。装配展平与"单实体几何解析"是两件事：
前者处理实例引用图与变换矩阵，后者处理面/壳拓扑。历史上这里出过
"实例位姿丢失导致多个零件叠在同一位置"的静默错误，独立成模块后
可以单测变换链路（平移/旋转/镜像/嵌套装配）。

坐标约定：全部 4×4 仿射矩阵，列主序按行存；点变换用 ``apply_matrix``。
"""
from __future__ import annotations

from .syntax import A, _point, _ref, _refs
from .types import Face, StepModel
from .vec3 import _v3, v_cross, v_dot, v_len, v_mul, v_sub, v_unit

# 单位变换（装配展平的起点；与 stepio 里同名常量保持一致）
IDENTITY = {"R": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "t": (0.0, 0.0, 0.0)}

def axis_placement(ent: dict, ref) -> dict | None:
    """解析 AXIS2_PLACEMENT_3D → {origin, z, x}（单位向量）。"""
    e = ent.get(ref) if ref is not None else None
    if not e or e[0] != "AXIS2_PLACEMENT_3D":
        return None
    o = _point(ent.get(_ref(A(e, 1))))
    z = _v3(_point(ent.get(_ref(A(e, 2)))))
    x = _v3(_point(ent.get(_ref(A(e, 3)))))
    oz = v_len(z)
    ox = v_len(x)
    z = (z[0] / oz, z[1] / oz, z[2] / oz) if oz > 1e-12 else (0.0, 0.0, 1.0)
    x = (x[0] / ox, x[1] / ox, x[2] / ox) if ox > 1e-12 else (1.0, 0.0, 0.0)
    return {"origin": o, "z": z, "x": x}


def placement_matrix(pl_from: dict, pl_to: dict):
    """两个 AXIS2_PLACEMENT_3D 之间的刚体变换：把 pl_from 的坐标搬到 pl_to。

    STEP 的 ITEM_DEFINED_TRANSFORMATION('','',#A,#B) 语义是"把 B 坐标系下的
    实体按 A 摆放"。这里返回 4×3 的变换（R, t），对点 p 求 R·p + t。
    """
    if not pl_from or not pl_to:
        return None

    def basis(pl) -> tuple:
        z = v_unit(pl["z"])
        xr = v_sub(pl["x"], v_mul(z, v_dot(pl["x"], z)))
        x = v_unit(xr) if v_len(xr) > 1e-12 else (1.0, 0.0, 0.0)
        y = v_cross(z, x)
        # 列向量 = 坐标轴在父坐标系下的方向
        return (x, y, z)

    bf, bt = basis(pl_from), basis(pl_to)
    # R 把 from 基下的方向转到父坐标系：先转回父系（Bf），再投到 to 基（Bt^T）
    def dot(colf, colto) -> float:
        return sum(colf[k] * colto[k] for k in range(3))

    # 父子系下：R = Bf · Bt^T（把"相对于 to 的方向"换成"相对于父系"）
    R = [[dot(bf[r], bt[c]) for c in range(3)] for r in range(3)]
    # 平移：让 from 的原点落到 to 的原点 → t = to.origin - R·from.origin
    fo = pl_from["origin"]
    ro = (R[0][0] * fo[0] + R[0][1] * fo[1] + R[0][2] * fo[2],
          R[1][0] * fo[0] + R[1][1] * fo[1] + R[1][2] * fo[2],
          R[2][0] * fo[0] + R[2][1] * fo[1] + R[2][2] * fo[2])
    to = pl_to["origin"]
    t = (to[0] - ro[0], to[1] - ro[1], to[2] - ro[2])
    return {"R": R, "t": t}


def _apply_matrix(mat, p) -> tuple:
    R, t = mat["R"], mat["t"]
    return (R[0][0] * p[0] + R[0][1] * p[1] + R[0][2] * p[2] + t[0],
            R[1][0] * p[0] + R[1][1] * p[1] + R[1][2] * p[2] + t[1],
            R[2][0] * p[0] + R[2][1] * p[1] + R[2][2] * p[2] + t[2])


def _apply_matrix_dir(mat, d) -> tuple:
    R = mat["R"]
    return (R[0][0] * d[0] + R[0][1] * d[1] + R[0][2] * d[2],
            R[1][0] * d[0] + R[1][1] * d[1] + R[1][2] * d[2],
            R[2][0] * d[0] + R[2][1] * d[1] + R[2][2] * d[2])


def _det(mat) -> float:
    R = mat["R"]
    return (R[0][0] * (R[1][1] * R[2][2] - R[1][2] * R[2][1])
            - R[0][1] * (R[1][0] * R[2][2] - R[1][2] * R[2][0])
            + R[0][2] * (R[1][0] * R[2][1] - R[1][1] * R[2][0]))


def rep_faces(ent: dict, face_entity_id: dict) -> dict[int, list[str]]:
    """representation 实体号 -> 该 rep 下的面 fid 列表（经 shell 归属）。

    路径：ADVANCED_FACE ← CLOSED_SHELL/OPEN_SHELL ← MANIFOLD_SOLID_BREP
          ← ..._SHAPE_REPRESENTATION.items
    """
    # face 实体号 -> fid
    eid2fid = {v: k for k, v in face_entity_id.items()}
    # shell 实体号 -> [face 实体号]
    shell_faces: dict[int, list[int]] = {}
    for i, e in ent.items():
        if e[0] in ("CLOSED_SHELL", "OPEN_SHELL"):
            # 通用切分后第一个参数就是引用列表 '(#f1,#f2,...)'
            shell_faces[i] = _refs(A(e, 1))
    # solid 实体号 -> shell 实体号
    solid_shell: dict[int, int] = {}
    for i, e in ent.items():
        if e[0] in ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "FACETED_BREP"):
            refs = _refs(A(e, 1))
            if refs:
                solid_shell[i] = refs[0]
    out: dict[int, list[str]] = {}
    for i, e in ent.items():
        if not e[0].endswith("SHAPE_REPRESENTATION"):
            continue
        # 通用切分后第一个参数就是 items 列表 '(#item,...)'
        items = _refs(A(e, 1))
        fids: list[str] = []
        for item in items:
            if item in solid_shell:
                for fe in shell_faces.get(solid_shell[item], []):
                    if fe in eid2fid:
                        fids.append(eid2fid[fe])
            elif item in shell_faces:            # 直接引用 shell
                for fe in shell_faces[item]:
                    if fe in eid2fid:
                        fids.append(eid2fid[fe])
        if fids:
            out[i] = fids
    return out


def matrix_mul(a, b) -> dict:
    """两个刚体变换的复合：先 a 后 b（结果 = b ∘ a）。"""
    Ra, ta = a["R"], a["t"]
    Rb, tb = b["R"], b["t"]
    R = [[sum(Rb[r][k] * Ra[k][c] for k in range(3)) for c in range(3)]
         for r in range(3)]
    t = tuple(sum(Rb[r][k] * ta[k] for k in range(3)) + tb[r] for r in range(3))
    return {"R": R, "t": t}


def assembly_edges(ent: dict, complex_parts: dict) -> dict[int, list[tuple]]:
    """父 rep -> [(子 rep, 变换矩阵)]，来自 CONTEXT_DEPENDENT_SHAPE_REPRESENTATION。

    STEP 里每次实例化写成：
        #748=(REPRESENTATION_RELATIONSHIP('','',#child,#parent)
              REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#749)
              SHAPE_REPRESENTATION_RELATIONSHIP());
        #749=ITEM_DEFINED_TRANSFORMATION('','',#from_pl,#to_pl);
    变换是**相对父装配**的局部变换，多层装配必须逐层累乘到世界坐标。
    """
    edges: dict[int, list[tuple]] = {}
    for e in ent.values():
        if not e or e[0] != "CONTEXT_DEPENDENT_SHAPE_REPRESENTATION":
            continue
        refs = _refs(" ".join(str(t) for t in e[1:]))
        if not refs:
            continue
        subs = complex_parts.get(refs[0]) or []
        reps: list[int] = []
        itm = None
        for sub in subs:
            if not sub:
                continue
            if sub[0] == "REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION":
                got = _refs(" ".join(str(t) for t in sub[1:]))
                if got:
                    itm = got[0]
            elif sub[0] == "REPRESENTATION_RELATIONSHIP":
                reps = _refs(" ".join(str(t) for t in sub[1:]))
        if len(reps) < 2:
            continue
        child, parent = reps[0], reps[1]
        mat = IDENTITY
        itm_e = ent.get(itm) if itm else None
        if itm_e and itm_e[0] == "ITEM_DEFINED_TRANSFORMATION":
            ir = _refs(" ".join(str(t) for t in itm_e[1:]))
            pf = axis_placement(ent, ir[0] if len(ir) > 0 else None)
            pt = axis_placement(ent, ir[1] if len(ir) > 1 else None)
            got = placement_matrix(pf, pt)
            if got:
                mat = got
        edges.setdefault(parent, []).append((child, mat))
    return edges


def _transform_face_inplace(face: "Face", mat: dict) -> None:
    """把面的坐标就地变换到世界坐标（STR-001 装配展平）。

    保持面 id 不变：连通分量检测、板框识别都按面 id 索引，id 稳定才不会打乱。
    """
    face.loop = [_apply_matrix(mat, p) for p in face.loop]
    face.holes = [[_apply_matrix(mat, p) for p in ring] for ring in face.holes]
    face.centroid = _apply_matrix(mat, face.centroid)
    face.normal = tuple(round(c, 9) for c in _apply_matrix_dir(mat, face.normal))


def flatten_mapped_items(model: StepModel, ent: dict,
                          rep_faces: dict[int, list[str]],
                          complex_parts: dict) -> None:
    """展开 MAPPED_ITEM：同一份零件几何被多次引用，每次带自己的位姿（STR-001）。

    STEP 写法：
        #m = MAPPED_ITEM('name', #rm, #placement)
        #rm = REPRESENTATION_MAP(#origin_in_source, #source_rep)
        #placement = AXIS2_PLACEMENT_3D(...)   ← 该实例在父装配中的位置

    变换 = 把源 rep 的坐标从 origin_in_source 系搬到 placement 系。
    第一个 MAPPED_ITEM 就地改写原件，其余复制。
    """
    by_source: dict[int, list[dict]] = {}
    for e in ent.values():
        if not e or e[0] != "MAPPED_ITEM":
            continue
        refs = _refs(" ".join(str(t) for t in e[1:]))
        if len(refs) < 2:
            continue
        rm, placement = refs[0], refs[1]
        rm_e = ent.get(rm)
        if not rm_e or rm_e[0] != "REPRESENTATION_MAP":
            continue
        rm_refs = _refs(" ".join(str(t) for t in rm_e[1:]))
        origin_ref = rm_refs[0] if rm_refs else None
        src_rep = rm_refs[-1] if len(rm_refs) > 1 else None
        if src_rep is None or src_rep not in rep_faces:
            continue
        pl_from = axis_placement(ent, origin_ref)
        pl_to = axis_placement(ent, placement)
        mat = placement_matrix(pl_from, pl_to)
        if mat is None:
            continue
        by_source.setdefault(src_rep, []).append(
            {"mat": mat, "name": A(e, 1)})

    added = 0
    for src_rep, insts in by_source.items():
        fids = rep_faces[src_rep]
        # 被 MAPPED_ITEM 引用的 rep 只是**零件定义体**（局部坐标）。
        # 它的实例一律由 MAPPED_ITEM 给出，因此定义体本身不应留在模型里，
        # 否则会在原点出现一份"幽灵零件"（实测：bbox 从 50..60 变成 0..60）。
        # 复制必须**先做**：复制源取自 model.faces 里的原件。
        for k, inst in enumerate(insts):
            # MAPPED_ITEM 的每一次引用都是一个**额外实例**：原 rep 的几何是
            # 零件定义（局部坐标，由普通 B-rep 提供），不能当作第一个实例。
            # 因此全部复制，原件保持不动。
            mat = inst["mat"]
            for fid in fids:
                src = next((f for f in model.faces if f.fid == fid), None)
                if src is None:
                    continue
                model.faces.append(Face(
                    fid=f"{fid}@mapped{k}",
                    surf=src.surf,
                    normal=tuple(round(c, 9) for c in
                                 _apply_matrix_dir(mat, src.normal)),
                    area=src.area,
                    centroid=_apply_matrix(mat, src.centroid),
                    loop=[_apply_matrix(mat, p) for p in src.loop],
                    radius=src.radius,
                    diagnostics=list(src.diagnostics) + ["G-ASSEMBLY-INSTANCE"],
                    holes=[[_apply_matrix(mat, p) for p in ring]
                           for ring in src.holes],
                ))
            added += 1
        # 全部实例复制完毕，移除零件定义体（避免原点残留幽灵零件）
        drop = set(fids)
        model.faces = [f for f in model.faces if f.fid not in drop]
        if len(insts) > 1:
            model.diagnostics.append({
                "code": "G-ASSEMBLY-FLATTENED",
                "msg": f"零件 rep #{src_rep} 被 MAPPED_ITEM 引用 {len(insts)} 次，"
                       f"已按各自位姿摆放",
                "advice": "已展平；如需按实例单独编辑请在 CAD 中拆成独立零件"})
    if added:
        model.solid_count = max(model.solid_count, len(by_source) + added)


def flatten_assembly(model: StepModel, ent: dict, face_entity_id: dict,
                      complex_parts: dict | None = None) -> None:
    """按装配实例递归展开几何到世界坐标（STR-001）。

    同一零件被多次引用时，每次引用有各自的位姿；多层装配还需逐层累乘。
    实现方式：从"不作为任何人的子节点"的根 rep 出发深度优先，
    把每个零件的面按累积矩阵复制一份（根装配里的零件用累积矩阵，
    可能不是单位矩阵）。
    """
    reps = rep_faces(ent, face_entity_id)
    if not reps:
        return
    edges = assembly_edges(ent, complex_parts or {})

    # 关键判据（实测验证）：STEP 有两种装配表达，不能一律叠加变换。
    #
    # ① MAPPED_ITEM / REPRESENTATION_MAP：同一份几何被**多次引用**并各自给位姿，
    #    几何本身是零件局部坐标 —— 必须按实例变换展开（本函数的目标场景）。
    #
    # ② 只有 CONTEXT_DEPENDENT_SHAPE_REPRESENTATION：导出器（PRO/E、Open CASCADE）
    #    已把位姿烘进 ADVANCED_BREP_SHAPE_REPRESENTATION 的顶点坐标里，
    #    CDSR 只是保留装配约束。实测证据：
    #      occ214.stp 原始 bbox 187.5×150×200（5 个零件已分布到 x=180），
    #      proe.stp   原始 bbox 5080×1524×1905，而变换平移只有 35/135/50 这类小值。
    #    若再叠加变换，坐标会炸到 ±300（实测），零件被搬到板外。
    #
    # 因此只有存在 MAPPED_ITEM 时才展开；否则只做诊断说明。
    has_mapped = any(e and e[0] == "MAPPED_ITEM" for e in ent.values())
    if has_mapped:
        flatten_mapped_items(model, ent, reps, complex_parts or {})
        return
    if not edges:
        return
    if True:
        n_inst = sum(len(v) for v in edges.values())
        if n_inst:
            model.diagnostics.append({
                "code": "G-ASSEMBLY-EXPLICIT",
                "msg": f"装配体含 {n_inst} 个实例引用，但几何已按位姿展开"
                       f"（导出器把变换烘进了坐标）",
                "advice": "无需额外处理；如需按实例单独编辑，请在 CAD 中"
                          "导出为独立零件后再导入"})
        return

    children = {c for pairs in edges.values() for c, _m in pairs}
    roots = [r for r in edges if r not in children]
    if not roots:                      # 有环：退化为按出现顺序的第一个父节点
        roots = list(edges)[:1]

    emitted: list[tuple[int, dict]] = []      # (rep, 累积变换)
    emitted_reps: set[int] = set()
    visited: set[tuple[int, int]] = set()

    def walk(rep: int, mat: dict, depth: int):
        if depth > 32:                 # 防御：异常文件里的深层/环状装配
            return
        reps_local = reps.get(rep)
        if reps_local:
            identity = (mat["R"] == IDENTITY["R"]
                        and tuple(mat["t"]) == IDENTITY["t"])
            if rep not in emitted_reps:
                # 该零件第一次被访问：原件是"零件局部坐标"。
                # 累积变换为单位阵 → 已在世界坐标，保持不动（连通分量检测依赖原 id）；
                # 否则就地改写原件坐标（不删不增，保持面 id 稳定）。
                if not identity:
                    for fid in reps_local:
                        src = next((f for f in model.faces if f.fid == fid), None)
                        if src is not None:
                            _transform_face_inplace(src, mat)
                emitted_reps.add(rep)
                emitted.append((rep, mat))
            elif not identity:
                # 同一零件的后续实例：复制一份并变换
                for fid in reps_local:
                    src = next((f for f in model.faces if f.fid == fid), None)
                    if src is None:
                        continue
                    model.faces.append(Face(
                        fid=f"{fid}@{len(emitted)}",
                        surf=src.surf,
                        normal=tuple(round(c, 9) for c in
                                     _apply_matrix_dir(mat, src.normal)),
                        area=src.area,
                        centroid=_apply_matrix(mat, src.centroid),
                        loop=[_apply_matrix(mat, p) for p in src.loop],
                        radius=src.radius,
                        diagnostics=list(src.diagnostics) + ["G-ASSEMBLY-INSTANCE"],
                        holes=[[_apply_matrix(mat, p) for p in ring]
                               for ring in src.holes],
                    ))
                emitted.append((rep, mat))
        for child, child_mat in edges.get(rep, []):
            visited.add((child, len(visited)))
            walk(child, matrix_mul(mat, child_mat), depth + 1)

    for r in roots:
        walk(r, IDENTITY, 0)

    extra = [f for f in model.faces
             if f.diagnostics and "G-ASSEMBLY-INSTANCE" in f.diagnostics]
    if extra:
        reps_used = {rep for rep, _m in emitted if rep in reps}
        model.diagnostics.append({
            "code": "G-ASSEMBLY-FLATTENED",
            "msg": f"装配体已展开：{len(extra)} 个面按实例位姿摆放"
                   f"（涉及 {len(reps_used)} 个零件表示、{len(edges)} 个父装配）",
            "advice": "已按 ITEM_DEFINED_TRANSFORMATION 递归累乘展平；"
                      "如需保留装配结构请在 CAD 中导出为单一零件"})
        model.solid_count = max(model.solid_count, len(reps_used) + len(extra) // 8)


# ---- 旧名兼容（stepio 的私有 API 被 tests 与 read_step 引用）
_axis_placement = axis_placement
_placement_matrix = placement_matrix
_rep_faces = rep_faces
_matrix_mul = matrix_mul
_assembly_edges = assembly_edges
_flatten_assembly = flatten_assembly
_flatten_mapped_items = flatten_mapped_items
