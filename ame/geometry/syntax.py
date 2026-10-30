"""STEP 词法与语句解析原语（不做几何解释）。

从 `stepio.py` 拆出。这一层只回答"文件里写了什么实体、参数是什么"，
不判断面/壳/装配语义——语义在 `stepio.read_step` 与 `assembly.py` 里。

拆分的实际收益：这些原语（尤其 `_parse_entities` 的跨行/复杂实例处理）
是历史上缺陷最集中的地方，独立成模块后可以针对语法单独写测试，
不必构造完整 STEP 文件。
"""
from __future__ import annotations

import re


_ENTITY_RE = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\)\s*;")


_REF_RE = re.compile(r"#(\d+)")
_STRUCT_RE = re.compile(r"[(),']")


_ARG_QUOTED_RE = re.compile(r"'[^']*'|[^,]+")


_NEED_ARGS = frozenset({
    "SI_UNIT", "CARTESIAN_POINT", "DIRECTION", "AXIS2_PLACEMENT_3D",
    "PLANE", "CYLINDRICAL_SURFACE",
    # 圆锥/球/环面需要读参数（半径、半锥角、主/管半径）才能走解析面积；
    # 不在此白名单里时 _parse_entities 只保留类型名，参数全丢。
    "CONICAL_SURFACE", "SPHERICAL_SURFACE", "TOROIDAL_SURFACE",
    "LINE", "VERTEX_POINT", "EDGE_CURVE",
    "ORIENTED_EDGE", "EDGE_LOOP", "FACE_OUTER_BOUND", "FACE_BOUND",
    "ADVANCED_FACE", "CONVERSION_BASED_UNIT", "LENGTH_MEASURE_WITH_UNIT",
    "LENGTH_MEASURE",
    # 圆/圆弧：需要读取圆心、轴线与半径（圆孔、圆角边的离散）
    "CIRCLE", "SURFACE_CURVE", "SEAM_CURVE", "VECTOR",
    # 装配体（STR-001）：解析实例引用与位姿，才能把同一零件的多次引用分别摆放
    "NEXT_ASSEMBLY_USAGE_OCCURRENCE", "MAPPED_ITEM", "REPRESENTATION_MAP",
    "ITEM_DEFINED_TRANSFORMATION", "CONTEXT_DEPENDENT_SHAPE_REPRESENTATION",
    "REPRESENTATION_RELATIONSHIP",
    "SHAPE_DEFINITION_REPRESENTATION", "PRODUCT_DEFINITION_SHAPE",
    "PRODUCT_DEFINITION",
    # 壳/实体的引用链（面 → shell → solid → representation，装配展平要按 rep 取面）
    "CLOSED_SHELL", "OPEN_SHELL", "MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS",
    "FACETED_BREP", "ADVANCED_BREP_SHAPE_REPRESENTATION", "SHAPE_REPRESENTATION",
    "MANIFOLD_SURFACE_SHAPE_REPRESENTATION",
    # AP242 三角化几何（STR-001）：网格型 AP242 只有三角面片，没有 ADVANCED_FACE
    "TESSELLATED_SHAPE_REPRESENTATION", "TESSELLATED_SOLID", "TESSELLATED_SHELL",
    "TRIANGULATED_FACE_SET", "COMPLEX_TRIANGULATED_FACE_SET",
    "COORDINATES_LIST", "TRIANGULATED_FACE",
})


_STMT_RE = re.compile(r"#(\d+)\s*=\s*(.*?);", re.S)


_COMPLEX_PART_RE = re.compile(r"([A-Z0-9_]+)\s*\(([^()]*)\)")


# 复杂实例里所有"名字后紧跟左括号"的类型名（含嵌套子实体）。
# _COMPLEX_PART_RE 匹配不到带嵌套括号的子实体，而 #248=(BOUNDED_SURFACE()
# B_SPLINE_SURFACE(1,3,(...))) 的真实语义就在被漏掉的那个子实体里。
_COMPLEX_TYPE_RE = re.compile(r"([A-Z0-9_]+)\s*\(")


def _complex_type_names(body: str) -> list[str]:
    """复杂实例的顶层类型名序列（保持书写顺序，去重）。"""
    out: list[str] = []
    for m in _COMPLEX_TYPE_RE.finditer(body):
        name = m.group(1)
        if name not in out:
            out.append(name)
    return out


def _split_complex(body: str) -> list[list]:
    """拆复杂实例 #id=(A(...)B(...)C(...)) → [[A, args...], [B, ...], ...]。"""
    out = []
    for m in _COMPLEX_PART_RE.finditer(body):
        etype, args = m.group(1), m.group(2)
        parts = ([a.strip() for a in args.split(",")] if args.strip() else [])
        out.append([etype] + parts)
    return out


_STMT_HEAD_RE = re.compile(r"\s*#(\d+)\s*=\s*(.*)", re.S)


# `_iter_statements`（引号感知的逐字符切分）已删除：
# 它是零引用死代码，且 docstring 宣称"正则 273 ms / split 25 ms（10.8×）"，
# 性能审计实测**正好相反**——`_STMT_RE.finditer` 583 ms（20 MB），
# 该函数 2 575 ms（慢 4.4×）。留着这段注释与死代码只会误导后来人。


def _data_body(text: str) -> str:
    """取 DATA 段正文（HEADER 里也可能有 # 号，不能误收）。"""
    di = text.find("DATA;")
    if di < 0:
        return ""
    body = text[di + 5:]
    for stop in ("ENDSEC;", "END-ISO"):
        si = body.find(stop)
        if si >= 0:
            return body[:si]
    return body


def _parse_complex_instance(eid: int, stmt: str, entities: dict,
                            complex_parts: dict | None,
                            complex_types: dict | None) -> None:
    """解析复杂实例 `#id=(A()B()C())`，就地写入 entities。

    装配体的 REPRESENTATION_RELATIONSHIP 常写成
      #748=(REPRESENTATION_RELATIONSHIP('','',#child,#parent)
            REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#749)
            SHAPE_REPRESENTATION_RELATIONSHIP());
    只取第一个子实体（原实现）会把**变换引用丢掉**，装配无法展平。
    这里不改主记录形态（避免影响单位等既有解析），而是把全部子实体
    另存到 complex_parts，供装配展平按 id 查；第 k 个子实体同时以
    `-(eid*100+k)` 的合成键进入 entities，供按类型统计读到。
    """
    parts = _split_complex(stmt)
    if not parts:
        return
    entities[eid] = parts[0]
    if complex_types is not None:
        names = _complex_type_names(stmt)
        if len(names) > 1:
            complex_types[eid] = names
    if len(parts) > 1:
        if complex_parts is not None:
            complex_parts[eid] = parts
        for k, sub in enumerate(parts):
            if k:
                entities[-(eid * 100 + k)] = sub


def _parse_simple_statement(eid: int, stmt: str, entities: dict) -> None:
    """解析普通语句 `#id=TYPE(args)`，就地写入 entities。

    类型白名单：几何引擎不读参数的实体只记类型名，省下大量切分开销。
    CARTESIAN_POINT / DIRECTION / EDGE_LOOP 与 ADVANCED_FACE 形态固定，
    按形态直取，避免通用切分的正则/递归开销。
    """
    paren = stmt.find("(")
    if paren < 0:
        entities[eid] = [stmt]
        return
    etype = stmt[:paren].strip()
    body = stmt[paren + 1:stmt.rfind(")")] if ")" in stmt else stmt[paren + 1:]
    if etype not in _NEED_ARGS:
        entities[eid] = [etype]        # 引擎只按类型统计，不读参数
        return
    if etype in ("CARTESIAN_POINT", "DIRECTION", "EDGE_LOOP"):
        a = body.find("(")
        if a < 0:
            entities[eid] = [etype, body.strip()]
        else:
            entities[eid] = [etype, body[:a].strip(),
                             body[a + 1:body.rfind(")")]]
    elif etype == "ADVANCED_FACE":
        a = body.find("(")
        b = body.find(")")
        rest = body[b + 2:].lstrip() if b >= 0 else ""
        surf = rest.split(",")[0].strip()
        sense = rest.split(",")[1].strip() if "," in rest else ".T."
        entities[eid] = [etype, body[:a].strip() if a >= 0 else "",
                         body[a:b + 1] if a >= 0 and b >= 0 else "",
                         surf, sense]
    elif "(" not in body:
        if "'" in body:
            entities[eid] = [etype] + [p.strip() for p in
                                       _ARG_QUOTED_RE.findall(body)]
        elif body:
            entities[eid] = [etype] + body.split(",")
        else:
            entities[eid] = [etype]
    else:
        entities[eid] = [etype] + _split_args(body)


def _parse_entities(text: str, complex_parts: dict | None = None,
                    complex_types: dict | None = None) -> dict[int, list]:
    """解析 DATA 段：#id = TYPE(args...)，支持跨行语句与复杂实例。

    设计要点（对照真实 CAD 导出文件）：
    - **语句级切分**而非逐行匹配：STEP 允许一条语句跨多行，逐行解析会把
      长语句（含多个内环的 ADVANCED_FACE、大 EDGE_LOOP）整条丢弃，
      进而导致 G-OPEN-SHELL 假阳性；
    - **复杂实例** `#id=(A()B()C())`：单位的 SI_UNIT 常写在这种形式里，
      原来的 `#id=TYPE(...)` 正则匹配不到，会让所有真实文件误报 G-UNIT-MISSING；

    complex_parts: 可选 dict；传入时把每个复杂实例的**全部**子实体写进去
    （{eid: [[TYPE, args...], ...]}），供装配展平读取
    REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION（它总是第二个子实体）。

    complex_types: 可选 dict；传入时记录每个复杂实例的**全部顶层类型名**
    （{eid: ["BOUNDED_SURFACE", "B_SPLINE_SURFACE", ...]}）。子实体参数里可能
    含嵌套括号，_split_complex 会漏掉它们，而诊断信息恰恰需要真实类型名
    （报 "BOUNDED_SURFACE" 对用户没有信息量）。
    """
    entities: dict[int, list] = {}
    body_all = _data_body(text)
    for m in _STMT_RE.finditer(body_all):
        eid = int(m.group(1))
        stmt = m.group(2).strip()
        if not stmt:
            continue
        if stmt.startswith("("):
            _parse_complex_instance(eid, stmt, entities,
                                    complex_parts, complex_types)
        else:
            _parse_simple_statement(eid, stmt, entities)
    return entities


def _split_args(body: str) -> list:
    """按顶层逗号切分参数（引号内与括号内的逗号不算）。

    性能：用正则只迭代结构字符 ( ) , ' ——大段坐标文本由 C 层跳过，
    比 Python 逐字符循环快一个数量级（20MB 文件的主要热点）。
    """
    args: list[str] = []
    cur: list[str] = []
    depth = 0
    pos = 0
    in_str = False
    for m in _STRUCT_RE.finditer(body):
        ch = m.group(0)
        if in_str:
            cur.append(body[pos:m.end()])
            pos = m.end()
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            cur.append(body[pos:m.end()])
            pos = m.end()
            in_str = True
        elif ch == "(":
            depth += 1
            cur.append(body[pos:m.end()])
            pos = m.end()
        elif ch == ")":
            depth -= 1
            cur.append(body[pos:m.end()])
            pos = m.end()
        elif ch == "," and depth == 0:
            cur.append(body[pos:m.start()])
            args.append("".join(cur).strip())
            cur = []
            pos = m.end()
        else:
            cur.append(body[pos:m.end()])
            pos = m.end()
    cur.append(body[pos:])
    tail = "".join(cur).strip()
    if tail:
        args.append(tail)
    return args


def _num(tok: str, default=0.0) -> float:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return default


def _ref(tok: str):
    """'#123' → 123（避免 fullmatch 的编译开销：热路径每实体调用多次）。"""
    if not isinstance(tok, str):
        return None
    t = tok.strip()
    if len(t) > 1 and t[0] == "#" and t[1:].isdigit():
        return int(t[1:])
    return None


def _refs(tok: str) -> list[int]:
    """提取形如 (#1,#2) 或 #1 里的全部实体引用。"""
    if not isinstance(tok, str):
        return []
    if "#" not in tok:
        return []
    return [int(m) for m in _REF_RE.findall(tok)]


def _point(ent) -> tuple:
    # CARTESIAN_POINT('',(x,y,z)) —— 参数区（跳过名称位）
    if len(ent) <= 2:
        return (0.0, 0.0, 0.0)
    raw = A(ent, 1).strip("()").split(",")
    if len(raw) < 3:
        return (0.0, 0.0, 0.0)
    return (_num(raw[0]), _num(raw[1]), _num(raw[2]))


def A(ent, i: int) -> str:
    """取实体第 i 个参数（1 = 名称后的第一个实际参数）。"""
    return ent[i + 1] if len(ent) > i + 1 else ""


def _tess_float_list(tok: str) -> list[float]:
    """从 '(0.,0.,0.)' 或 '0.,0.,0.' 形式里取出浮点数列表。"""
    out = []
    for part in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(tok)):
        try:
            out.append(float(part))
        except ValueError:
            pass
    return out


def _tess_int_triplets(tok: str) -> list[tuple[int, int, int]]:
    """从 '((1,2,3),(1,3,4))' 里取出三角形索引三元组（STEP 是 1-based）。"""
    tris = []
    for grp in re.findall(r"\(([^()]*)\)", str(tok)):
        nums = [int(x) for x in re.findall(r"-?\d+", grp)]
        for k in range(0, len(nums) - 2, 3):
            tris.append((nums[k], nums[k + 1], nums[k + 2]))
    return tris
