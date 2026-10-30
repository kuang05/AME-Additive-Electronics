"""板文件导入（CIR-001）：KiCad .kicad_pcb 子集 + 网表/坐标表(JSON)。

KiCad 子集：Edge.Cuts 的 gr_rect/gr_line → 板框；footprint → 器件；pad → 焊盘。
纯网表缺焊盘坐标时只能做连通性检查（§1.6-4），本导入器要求坐标齐备。
"""
from __future__ import annotations

import json
import math
import re


# ---------------------------------------------------------------- S 表达式
def parse_sexp(text: str):
    """极简 S 表达式：返回嵌套 list，原子为 str；引号内含空格。"""
    tokens = re.findall(r'"[^"]*"|\(|\)|[^\s()"]+', text)
    root, stack = [], [None]

    def push(item):
        if stack[-1] is None:
            root.append(item)
        else:
            stack[-1].append(item)

    for tok in tokens:
        if tok == "(":
            new = []
            push(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            push(tok)
    return root


def _tag(node) -> str:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else ""


def _find_all(nodes, tag):
    return [n for n in nodes if isinstance(n, list) and _tag(n) == tag]


def _find(nodes, tag):
    l = _find_all(nodes, tag)
    return l[0] if l else None


def _unquote(tok) -> str:
    """去掉 KiCad 的引号包裹（字符串 token 形如 '"R1"'）。"""
    if not isinstance(tok, str):
        return ""
    return tok.strip('"')


def _num(tok, default=0.0) -> float:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return default


def _at(node) -> tuple[float, float, float]:
    """(at x y [rot]) → (x, y, rot)。"""
    if not node:
        return (0.0, 0.0, 0.0)
    return (_num(node[1]), _num(node[2]) if len(node) > 2 else 0.0,
            _num(node[3]) if len(node) > 3 else 0.0)


# ---------------------------------------------------------------- 导入结果
class BoardData:
    def __init__(self):
        self.format = "kicad_pcb"
        self.outline: list[tuple[float, float]] = []
        self.comps: list[dict] = []      # {ref, footprint, x, y, surface, rot, height}
        self.pads: list[dict] = []       # {ref, pad_no, net, x, y, shape, w, h}
        self.nets: dict[str, dict] = {}  # name -> {klass}
        self.routes: list[dict] = []     # {net, pts, width, surface}（导入原样保留，合规交给 MRC）
        self.warnings: list[str] = []


# ---------------------------------------------------------------- 导入阶段
def _parse_outline(node) -> list[tuple[float, float]]:
    """板框（CIR-001）：Edge.Cuts 上的 gr_rect / gr_line → 包围盒四角。

    逐条收集图形端点后取包围盒；无 Edge.Cuts 图形时返回空表，
    是否告警由调用方决定。非 Edge.Cuts 层的图形一律忽略。
    """
    pts: list[tuple[float, float]] = []
    for gr in _find_all(node, "gr_rect") + _find_all(node, "gr_line"):
        layer = _find(gr, "layer")
        lay = layer[1].strip('"') if layer else ""
        if lay != "Edge.Cuts":
            continue
        s, e = _find(gr, "start"), _find(gr, "end")
        if s and e:
            x0, y0, _ = _at(s)
            x1, y1, _ = _at(e)
            pts.extend([(x0, y0), (x1, y1)])
    if not pts:
        return []
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [(min(xs), min(ys)), (max(xs), min(ys)),
            (max(xs), max(ys)), (min(xs), max(ys))]


def _extract_reference(fp, index: int) -> tuple[str, list[str]]:
    """单个 footprint 的位号：返回 (位号, 告警列表)，index 为器件序号（从 0 起）。

    位号两种写法都要支持：
      老格式（KiCad ≤5）：(fp_text reference "R1" ...)
      新格式（KiCad 6+）：(property "Reference" "R1" ...)
    只认前者会让现代板文件的位号静默变成 COMP1/COMP2（器件数/焊盘/网络
    全对，只有位号错且不告警），用户导入自己刚画的板子满屏 COMPn。
    两种写法都没有时临时命名 COMP<index+1> 并告警（CIR-001：不静默）。
    """
    warnings: list[str] = []
    ref = ""
    ftxt = _find(fp, "fp_text")
    if ftxt and len(ftxt) > 2 and _tag(ftxt) == "fp_text" and ftxt[1] == "reference":
        ref = _unquote(ftxt[2])
    if not ref:
        for prop in _find_all(fp, "property"):
            if len(prop) > 2 and _unquote(prop[1]) == "Reference":
                ref = _unquote(prop[2])
                break
    if not ref:
        ref = f"COMP{index + 1}"
        warnings.append(
            f"第 {index + 1} 个器件未找到位号（Reference），"
            f"临时命名为 {ref}；请在原理图/封装中补齐位号后重新导出")
    return ref, warnings


def _extract_pad(pad, ref: str, fx: float, fy: float,
                 frot: float) -> tuple[dict, str | None]:
    """单个 pad → 焊盘记录：返回 (焊盘记录, 网络名)，无网络时网络名为 None。

    焊盘绝对位置 = 器件位姿 + 旋转后的相对坐标；形状映射：circle→circle、
    roundrect/oval 且宽高不等→oval，其余→rect。
    """
    name = pad[1].strip('"') if len(pad) > 1 else "?"
    shape_tok = pad[3] if len(pad) > 3 else "rect"
    px, py, prot = _at(_find(pad, "at"))
    size = _find(pad, "size")
    w, h = (_num(size[1]), _num(size[2])) if size and len(size) > 2 else (1.5, 1.0)
    # 焊盘绝对位置 = 器件位姿 + 旋转后的相对坐标
    a = math.radians(frot)
    rx = px * math.cos(a) - py * math.sin(a)
    ry = px * math.sin(a) + py * math.cos(a)
    axx, ayy = fx + rx, fy + ry
    net = _find(pad, "net")
    netname = net[2].strip('"') if net and len(net) > 2 and net[2].strip('"') else None
    shape = {"circle": "circle"}.get(shape_tok, "rect")
    if shape_tok in ("roundrect", "oval") and abs(w - h) > 0.01:
        shape = "oval"
    rec = {"ref": ref, "pad_no": name, "net": netname,
           "x": round(axx, 4), "y": round(ayy, 4),
           "shape": shape, "w": w, "h": h,
           "rot": (prot + frot) % 360}
    return rec, netname


def _parse_footprints(node) -> tuple[list[dict], list[dict], dict[str, dict], list[str]]:
    """器件与焊盘提取：遍历 footprint，返回 (comps, pads, nets, warnings)。

    comps/pads 为记录列表，nets 只登记焊盘上出现过的网络
    （name → {klass: signal}），warnings 为缺位号告警；
    调用方按此顺序并入 BoardData（与原实现保持一致）。
    """
    comps: list[dict] = []
    pads: list[dict] = []
    nets: dict[str, dict] = {}
    warnings: list[str] = []
    for fp in _find_all(node, "footprint"):
        fx, fy, frot = _at(_find(fp, "at"))
        layer = _find(fp, "layer")
        surface = "bottom" if (layer and layer[1].strip('"').startswith("B.")) else "top"
        ref, warns = _extract_reference(fp, len(comps))
        warnings.extend(warns)
        comps.append({
            "ref": ref,
            "footprint": fp[1].strip('"') if len(fp) > 1 and isinstance(fp[1], str) else "",
            "x": fx, "y": fy, "surface": surface, "rot": frot,
            "height": 1.5,  # 默认器件高度，可在属性面板修改
        })
        for pad in _find_all(fp, "pad"):
            rec, netname = _extract_pad(pad, ref, fx, fy, frot)
            pads.append(rec)
            if netname and netname not in nets:
                nets[netname] = {"klass": "signal"}
    return comps, pads, nets, warnings


def _parse_segments(node) -> tuple[dict[int, str], dict[str, list]]:
    """走线 segment 提取：返回 (net 编号→名称表, 按网络分组的原始线段)。

    segment 导入原样读入（含违规线宽），合规判定交给 MRC（CIR-010 只拦
    交互修改）。net 编号未声明或线段未挂网络的直接丢弃（与原实现一致）；
    每条原始线段为 ((x0, y0), (x1, y1), 线宽, 面别)。
    """
    net_by_no: dict[int, str] = {}
    for n in _find_all(node, "net"):
        if len(n) >= 3 and _num(n[1], -1) >= 0:
            net_by_no[int(_num(n[1]))] = n[2].strip('"')
    raw_segs: dict[str, list] = {}
    for seg in _find_all(node, "segment"):
        s, e = _find(seg, "start"), _find(seg, "end")
        if not s or not e:
            continue
        # 取节点第 1 个参数，容错到"节点不存在"与"节点存在但无参数"两种情况。
        # 早期写 `(_find(...) or [None, 0.6])[1]`：`_find` 对 `(width)` 这种
        # 只有标签没有值的畸形节点会返回 `["width"]`（长度 1），`[1]` 直接
        # IndexError —— 整个导入崩掉。实测可复现，违反 NFR-002（坏数据不崩溃）。
        def _arg(node, idx: int, default):
            return node[idx] if node is not None and len(node) > idx else default

        w = _num(_arg(_find(seg, "width"), 1, 0.6), 0.6)
        layer = _find(seg, "layer")
        lay = _arg(layer, 1, "F.Cu").strip('"')
        netno = int(_num(_arg(_find(seg, "net"), 1, 0), 0))
        name = net_by_no.get(netno)
        if not name:
            continue
        raw_segs.setdefault(name, []).append(
            ((_num(s[1]), _num(s[2])), (_num(e[1]), _num(e[2])), w,
             "bottom" if lay.startswith("B.") else "top"))
    return net_by_no, raw_segs


def import_kicad_pcb(text: str) -> BoardData:
    """导入 KiCad .kicad_pcb（CIR-001）：只做编排，各阶段见 _parse_*。

    阶段：板框（_parse_outline）→ 器件/焊盘/网络（_parse_footprints）→
    走线段（_parse_segments）→ 同网络线段串接成折线（_chain_segments，
    KiCad 把一条走线拆成多个 segment）。
    """
    bd = BoardData()
    root = parse_sexp(text)
    node = next((n for n in root if isinstance(n, list) and _tag(n) == "kicad_pcb"),
                root)

    outline = _parse_outline(node)
    if outline:
        bd.outline = outline
    else:
        bd.warnings.append("板文件无 Edge.Cuts 板框，请手工指定")

    comps, pads, nets, warns = _parse_footprints(node)
    bd.comps.extend(comps)
    bd.pads.extend(pads)
    bd.nets.update(nets)
    bd.warnings.extend(warns)
    if not bd.comps:
        bd.warnings.append("板文件未解析到器件")

    _, raw_segs = _parse_segments(node)
    for name in raw_segs:
        bd.nets.setdefault(name, {"klass": "signal"})
    # 同网络线段串接成折线（KiCad 把一条走线拆成多个 segment）
    for name, segs in raw_segs.items():
        bd.routes.extend(_chain_segments(name, segs))
    return bd


def _key(p) -> tuple:
    return (round(p[0], 4), round(p[1], 4))


def _chain_segments(net: str, segs: list) -> list[dict]:
    """把同网络线段按端点串接为折线；同网络多条独立走线各自成一条。

    KiCad 把一条折线走线拆成多个 segment；这里还原成连续点列，
    便于连通性/切片按折线处理（避免逐段当成独立走线）。
    """
    by_w: dict[tuple, list] = {}
    for a, b, w, surf in segs:
        by_w.setdefault((w, surf), []).append((_key(a), _key(b)))
    out = []
    for (w, surf), items in sorted(by_w.items()):
        adj: dict[tuple, list] = {}
        for a, b in items:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        used: set[frozenset] = set()

        def walk(start: tuple, adj: dict = adj,
                 used: set = used) -> list[tuple]:
            """从 start 沿未用边走到端点，返回点列。

            ``adj`` / ``used`` 用默认参数绑定当前迭代的容器：虽然这里
            每次迭代都立即调用完（不会出现"闭包延迟执行导致读到最后一个
            循环变量"的经典 bug），但显式绑定让意图无歧义，也便于把
            ``walk`` 单独提出来测试。
            """
            path, cur = [start], start
            while True:
                step = next((k for k in adj[cur]
                             if frozenset((cur, k)) not in used), None)
                if step is None:
                    break
                used.add(frozenset((cur, step)))
                path.append(step)
                cur = step
                if len(adj[cur]) == 1:
                    break
            return path

        # 先从真正的端点（度=1）起步，保证一条折线只产生一条记录
        for start in sorted(k for k in adj if len(adj[k]) == 1):
            if all(frozenset((start, k)) in used for k in adj[start]):
                continue
            path = walk(start)
            if len(path) >= 2:
                out.append({"net": net, "pts": [[x, y, 0.0] for x, y in path],
                            "width": w, "surface": surf})
        # 剩余为闭环走线
        for start in sorted(adj):
            if all(frozenset((start, k)) in used for k in adj[start]):
                continue
            path = walk(start)
            if len(path) >= 2:
                out.append({"net": net, "pts": [[x, y, 0.0] for x, y in path],
                            "width": w, "surface": surf})
    return out


def import_netlist_json(text: str) -> BoardData:
    """网表 + 坐标表（DAT-003）：JSON 格式，坐标必填（缺坐标拒绝，CIR-001）。"""
    data = json.loads(text)
    bd = BoardData()
    bd.format = "netlist+xy"
    bd.outline = [(p[0], p[1]) for p in data.get("outline", [])]
    if not bd.outline:
        bd.warnings.append("坐标表缺板框")
    for c in data.get("comps", []):
        bd.comps.append({"ref": c["ref"], "footprint": c.get("footprint", ""),
                         "x": float(c["x"]), "y": float(c["y"]),
                         "surface": c.get("surface", "top"),
                         "rot": float(c.get("rot", 0)), "height": float(c.get("height", 1.5))})
    # 坐标校验必须先于取值（CIR-001/DAT-003：纯网表缺焊盘坐标时给出可读
    # ValueError；若先按 p["x"] 取值会抛 KeyError，校验成为不可达代码）
    for p in data.get("pads", []):
        if "x" not in p or "y" not in p:
            raise ValueError(f"纯网表缺焊盘坐标（{p.get('ref')}/{p.get('pad')}），"
                             "只能做连通性检查，不能布线")
    for p in data.get("pads", []):
        bd.pads.append({"ref": p["ref"], "pad_no": str(p["pad"]),
                        "net": p.get("net"), "x": float(p["x"]), "y": float(p["y"]),
                        "shape": p.get("shape", "rect"),
                        "w": float(p.get("w", 1.5)), "h": float(p.get("h", 1.0)),
                        "rot": float(p.get("rot", 0))})
        if p.get("net") and p["net"] not in bd.nets:
            bd.nets[p["net"]] = {"klass": p.get("klass", "signal")}
    # 只有 nets、没有 pads 时会"导入成功但一个对象都没有"，用户看到的是空板。
    # CIR-001 要求无坐标必须明确告知，不能静默产出空工程。
    if not bd.pads:
        if data.get("nets"):
            raise ValueError(
                "该文件只有网络连接关系、没有焊盘坐标，无法建立可布线电路。"
                "请提供带坐标的网表（netlist+xy），或改用 KiCad 板文件（.kicad_pcb）")
        raise ValueError("文件里没有任何器件/焊盘数据（comps 与 pads 均为空）")
    return bd
