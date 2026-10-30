"""连通性与走线合法性（CIR-004/009/010/011/012）。

导体几何：焊盘矩形（含旋转）+ 走线中心线 buffer(width/2) + 跳线。
连通 = 同网络全部焊盘落在同一连通分量；异网共享分量即短路。
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..geometry.primitives import pad_polygon  # noqa: F401  (对外沿用)
from ..geometry.primitives import route_polygon as route_polyline  # noqa: F401

# 布局/自动布线算法已拆到 circuit/routing.py；re-export 保持旧调用方可用。
from .routing import (auto_route_candidates, count_overlaps,  # noqa: F401
                      place_candidates, total_ratline_length)

PROCESS_MIN_WIDTH = 0.5   # 工艺线宽下限（R1 阈值；低于此值拒绝落线 CIR-010）


# ================================================================ 落线前校验（CIR-010/011/006）
#
# validate_route 是一张检查清单：每类非法情形由一个 _check_* 单项检查负责，
# 返回 (是否通过, 失败原因)。_ROUTE_CHECKS 的排列顺序即报错优先级，不得调整——
# 例如先线宽后板框，顺序一变，同一条非法走线就会报不同的错
# （tests/test_validate_split.py 逐字锁定顺序与文案）。
# 各检查统一签名 (pts, width, outline, keepouts)，未用到的参数以下划线开头。


def _check_width_finite(_pts: list, width: float, _outline: object,
                        _keepouts: list | None) -> tuple[bool, str]:
    """线宽必须是有限数值。"""
    # NaN/Inf 必须先拦：NaN 与任何数比较都是 False，会绕过线宽下限检查
    # （实测 NaN 线宽一路走到数据库才被 NOT NULL 拦下、Inf 直接落库）。
    try:
        if not math.isfinite(float(width)):
            return False, f"线宽必须是有限数值，收到 {width!r}"
    except (TypeError, ValueError):
        return False, f"线宽必须是数值，收到 {width!r}"
    return True, ""


def _check_width_min(_pts: list, width: float, _outline: object,
                     _keepouts: list | None) -> tuple[bool, str]:
    """线宽不低于工艺下限（CIR-010）。"""
    if width < PROCESS_MIN_WIDTH:
        return False, f"线宽 {width:.2f} mm 低于工艺下限 {PROCESS_MIN_WIDTH} mm"
    return True, ""


def _check_point_count(pts: list, _width: float, _outline: object,
                       _keepouts: list | None) -> tuple[bool, str]:
    """走线至少需要两个点。"""
    if len(pts) < 2:
        return False, "走线至少需要两个点"
    return True, ""


def _check_point_coords_finite(pts: list, _width: float, _outline: object,
                               _keepouts: list | None) -> tuple[bool, str]:
    """每个点的 X/Y 坐标必须是有限数值。

    坐标同样要拦住 NaN/Inf：否则几何库会给出 NaN 距离，后续判断全部失效。
    """
    for i, p in enumerate(pts):
        for k, v in enumerate(p[:2]):
            try:
                if not math.isfinite(float(v)):
                    return False, f"第 {i + 1} 个点的 {'XY'[k]} 坐标不是有限数值：{v!r}"
            except (TypeError, ValueError):
                return False, f"第 {i + 1} 个点的 {'XY'[k]} 坐标不是数值：{v!r}"
    return True, ""


def _check_point_complete(pts: list, _width: float, _outline: object,
                          _keepouts: list | None) -> tuple[bool, str]:
    """每个点至少要有 X/Y 两个分量。"""
    for i, p in enumerate(pts):
        if len(p) < 2:
            return False, f"第 {i + 1} 个点坐标不完整"
    return True, ""


def _check_zero_length(pts: list, _width: float, _outline: object,
                       _keepouts: list | None) -> tuple[bool, str]:
    """相邻两点重合即零长度段。"""
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6:
            return False, f"第 {i + 1} 段为零长度"
    return True, ""


def _check_duplicate_points(pts: list, _width: float, _outline: object,
                            _keepouts: list | None) -> tuple[bool, str]:
    """非相邻的两个点重合必须拒绝。

    重复点等价于零长度段（会被 G-code 当成停顿点，造成浆料堆积）。
    相邻重复已由零长度检查覆盖；首尾同点是合法的闭合走线
    （天线线圈、地环、屏蔽框），不算重复。
    """
    seen: dict[tuple[float, float], int] = {}
    last = len(pts) - 1
    for i, p in enumerate(pts):
        key = (round(p[0], 6), round(p[1], 6))
        if key in seen and not (seen[key] == 0 and i == last and last >= 3):
            return False, f"第 {seen[key] + 1} 个点与第 {i + 1} 个点重复 ({p[0]:.1f},{p[1]:.1f})"
        seen[key] = i
    return True, ""


def _check_outline(pts: list, _width: float, outline: object,
                   _keepouts: list | None) -> tuple[bool, str]:
    """走线的每个点和每一段都必须落在板框内（CIR-011）。"""
    poly = Polygon(outline) if len(outline) >= 3 else None
    if poly is None:
        return True, ""
    for i, p in enumerate(pts):
        if not poly.covers(Point(p[0], p[1])):
            return False, f"第 {i + 1} 个点 ({p[0]:.1f},{p[1]:.1f}) 超出板框"
    # 只校验端点会漏掉"两端在框内、线段却穿过缺口"的凹形板框
    # （实测两点 (30,50)-(70,50) 在 U 形板的缺口上，端点都在框内但线穿了出去）
    for i in range(len(pts) - 1):
        seg = LineString([(pts[i][0], pts[i][1]), (pts[i + 1][0], pts[i + 1][1])])
        if not poly.covers(seg):
            return False, f"第 {i + 1} 段穿出板框"
    return True, ""


def _check_keepouts(pts: list, width: float, _outline: object,
                    keepouts: list | None) -> tuple[bool, str]:
    """导体带（中心线按线宽/2 外扩）不得落入禁区（CIR-006）。

    只判中心线是不够的——线宽 0.6 的走线中心线擦边过去，导体仍会压进禁区。
    """
    if not keepouts:
        return True, ""
    try:
        band = LineString([(p[0], p[1]) for p in pts]).buffer(float(width) / 2)
    except Exception:
        band = None
    for ko in keepouts:
        kp = ko.get("pts") or []
        if len(kp) < 3:
            continue
        try:
            g = Polygon([(float(p[0]), float(p[1])) for p in kp])
            if not g.is_valid:
                g = g.buffer(0)
        except Exception:
            continue
        if g.is_empty:
            continue
        target = band if band is not None else LineString(
            [(p[0], p[1]) for p in pts])
        if target.intersects(g) and target.intersection(g).area > 1e-9:
            return False, (f"走线进入禁布区 {ko.get('name') or '未命名'}"
                           "（CIR-006：禁区不可布线）")
    return True, ""


def _check_self_intersection(pts: list, _width: float, _outline: object,
                             _keepouts: list | None) -> tuple[bool, str]:
    """只拦"真正的交叉"：相邻段共享端点、闭合环的首尾相接都合法。

    原实现把闭合环（首尾同点）的第 1 段与最后一段误判为自交，导致无法落
    闭合走线（天线线圈、地环、屏蔽框都是这种拓扑）。
    """
    n_seg = len(pts) - 1
    closed = (n_seg >= 3
              and abs(pts[0][0] - pts[-1][0]) < 1e-6
              and abs(pts[0][1] - pts[-1][1]) < 1e-6)
    segs = [(pts[i][:2], pts[i + 1][:2]) for i in range(n_seg)]
    for i in range(n_seg):
        for j in range(i + 1, n_seg):
            # 相邻段（含闭合环的末段与首段）共享端点，不算自交
            if j == i + 1:
                continue
            if closed and i == 0 and j == n_seg - 1:
                continue
            a = LineString(segs[i])
            b = LineString(segs[j])
            if not a.intersects(b):
                continue
            # 只有"共享端点"（合法连接）才放行，其余一律算自交。
            # 判据：两条段的四个端点里，恰好有一对重合且交集就是一个点。
            shared = [p for p in segs[i] if p in segs[j]]
            inter = a.intersection(b)
            if len(shared) == 1 and inter.geom_type == "Point":
                continue
            return False, f"走线自交于第 {i + 1} 段与第 {j + 1} 段"
    return True, ""


# 单项检查执行清单：按序执行，第一个失败立即终止（顺序即报错优先级）。
_ROUTE_CHECKS = (
    _check_width_finite,         # 1. 线宽是有限数值（NaN/Inf 先拦）
    _check_width_min,            # 2. 线宽不低于工艺下限（CIR-010）
    _check_point_count,          # 3. 至少两个点
    _check_point_coords_finite,  # 4. 坐标是有限数值
    _check_point_complete,       # 5. 每个点有 X/Y 两个分量
    _check_zero_length,          # 6. 无零长度段
    _check_duplicate_points,     # 7. 无非相邻重复点（闭合走线豁免）
    _check_outline,              # 8. 点与段都在板框内（CIR-011）
    _check_keepouts,             # 9. 导体带不落入禁布区（CIR-006）
    _check_self_intersection,    # 10. 无自交
)


def validate_route(pts: list[list[float]], width: float, outline,
                   occupied=None, keepouts: list | None = None) -> tuple[bool, str]:
    """落线前校验（CIR-011/010/006）。失败必须给出具体原因。

    keepouts: 禁布区 [{"pts":[[x,y],...], "name": "..."}]；导体落入禁区即拒绝。

    主体是检查清单：按序执行 _ROUTE_CHECKS 里的单项检查，
    第一个失败立即返回 (False, 原因)，其余检查不再执行。
    """
    for check in _ROUTE_CHECKS:
        ok, reason = check(pts, width, outline, keepouts)
        if not ok:
            return False, reason
    return True, ""


def _net_conductor(net_name: str, pads: list[dict], routes: list[dict], jumpers: list[dict],
                   surface: str | None = None):
    """某网络在指定物理层上的导体几何（surface=None 表示不分层，合在一起）。

    CIR-008：首版支持顶面/底面走线。分层判定时焊盘与跳线属于两面共享的
    互连点，故始终计入；走线只取本层的。
    """
    geoms = []
    for p in pads:
        if p.get("net") == net_name:
            geoms.append(pad_polygon(p))
    for r in routes:
        if r["net"] != net_name:
            continue
        if surface is not None and r.get("surface", "top") != surface:
            continue
        g = route_polyline(r["pts"], r["width"])
        if g is not None:
            geoms.append(g)
    for j in jumpers:
        if j["net"] == net_name:
            p1 = next((p for p in pads if p["id"] == j["from_pad"]), None)
            p2 = next((p for p in pads if p["id"] == j["to_pad"]), None)
            if p1 and p2:
                geoms.append(route_polyline([[p1["x"], p1["y"]], [p2["x"], p2["y"]]], 0.6))
    return geoms


def _shorts_by_strtree(layer_union: dict, names_l: list[str], layer: str,
                       shorts: list) -> None:
    """STRtree 预筛出包围盒重叠的网络对后再求交（CIR-009）。

    300 网络下全对比较要 45k 次 intersects，索引后只查包围盒重叠的少数对。
    """
    from shapely.strtree import STRtree
    geoms_l = [layer_union[n] for n in names_l]
    tree_l = STRtree(geoms_l)
    for i, g in enumerate(geoms_l):
        for j in tree_l.query(g):
            j = int(j)
            if j <= i or not g.intersects(geoms_l[j]):
                continue
            inter = g.intersection(geoms_l[j])
            c = inter.representative_point() if not inter.is_empty else None
            shorts.append({"nets": [names_l[i], names_l[j]],
                           "at": [round(c.x, 2), round(c.y, 2)] if c
                           else [0.0, 0.0], "surface": layer})


def _shorts_all_pairs(layer_union: dict, names_l: list[str], layer: str,
                      shorts: list) -> None:
    """无 STRtree 时的回退：全对比较。"""
    for i in range(len(names_l)):
        for j in range(i + 1, len(names_l)):
            a, b = layer_union[names_l[i]], layer_union[names_l[j]]
            if a.intersects(b):
                shorts.append({"nets": [names_l[i], names_l[j]],
                               "at": [0.0, 0.0], "surface": layer})


def _layer_shorts(nets: list[str], pads: list[dict], routes: list[dict],
                  jumpers: list[dict]) -> list[dict]:
    """异网短路逐层判定（CIR-008/009）：不同层的导体不算互相短路。

    CIR-008：顶/底面走线属不同物理层，层间不导通。
    只有出现过走线的层才需要单独判短路；断路则按"每层连通 + 焊盘共享"综合判。
    """
    layers = sorted({r.get("surface", "top") for r in routes}) or ["top"]
    shorts: list[dict] = []
    for layer in layers:
        layer_union: dict[str, object] = {}
        for name in nets:
            g = _net_conductor(name, pads, routes, jumpers, surface=layer)
            if g:
                layer_union[name] = unary_union(g)
        names_l = [n for n in layer_union if n in layer_union]
        if len(names_l) < 2:
            continue
        try:
            _shorts_by_strtree(layer_union, names_l, layer, shorts)
        except ImportError:
            _shorts_all_pairs(layer_union, names_l, layer, shorts)
    return shorts


def check_connectivity(nets: list[str], pads: list[dict], routes: list[dict],
                       jumpers: list[dict] | None = None) -> dict:
    """返回 {ok, opens:[{net, at:[x,y], reason}], shorts:[{nets, at}]}.（CIR-009）"""
    jumpers = jumpers or []
    opens: list[dict] = []
    for name in nets:
        geoms = _net_conductor(name, pads, routes, jumpers)
        if not geoms:
            continue
        u = unary_union(geoms)
        if u.geom_type == "MultiPolygon":
            comps = list(u.geoms)
        else:
            comps = [u]
        # 焊盘所在分量
        pad_comp = {}
        for p in pads:
            if p.get("net") != name:
                continue
            pt = Point(p["x"], p["y"])
            for k, c in enumerate(comps):
                if c.covers(pt):
                    pad_comp[p["id"]] = k
                    break
            else:
                pad_comp[p["id"]] = -1
        groups = {}
        for pid, k in pad_comp.items():
            groups.setdefault(k, []).append(pid)
        if len(groups) > 1:
            isolated = groups.get(-1, None) or min(groups.values(), key=len)
            pad = next((p for p in pads if p["id"] == isolated[0]), None)
            at = [pad["x"], pad["y"]] if pad else [0.0, 0.0]
            opens.append({"net": name, "at": [round(v, 2) for v in at],
                          "reason": f"网络 {name} 分裂为 {len(groups)} 个孤立导体组"})

    # 异网短路：逐层判定（不同层的导体不算互相短路），
    # 每层内用 STRtree 预筛出包围盒重叠的网络对后再求交
    shorts = _layer_shorts(nets, pads, routes, jumpers)

    return {"ok": not opens and not shorts, "opens": opens, "shorts": shorts}


def ratlines(pads: list[dict], routes: list[dict],
             net: str | None = None) -> list[dict]:
    """飞线几何（CIR-003）：返回 [{"net", "a":[x,y], "b":[x,y], "len"}]。

    画布用它画"还没连上的连接关系"。同一网络内的焊盘按最小生成树连接，
    而不是画成完全图——完全图在 4 焊盘网络上会画出 6 条线，视觉上无法阅读。
    """
    by_net: dict[str, list[dict]] = {}
    for p in pads:
        if p.get("net") and (net is None or p["net"] == net):
            by_net.setdefault(p["net"], []).append(p)
    routed_nets = {r["net"] for r in routes}
    out: list[dict] = []
    for name, ps in by_net.items():
        if name in routed_nets or len(ps) < 2:
            continue
        # Prim 最小生成树：从第一个焊盘出发，每次连最近的未连焊盘
        linked = [0]
        rest = list(range(1, len(ps)))
        while rest:
            best = None
            for i in linked:
                for j in rest:
                    d = (abs(ps[i]["x"] - ps[j]["x"])
                         + abs(ps[i]["y"] - ps[j]["y"]))
                    if best is None or d < best[0]:
                        best = (d, i, j)
            d, i, j = best
            out.append({"net": name,
                        "a": [round(ps[i]["x"], 3), round(ps[i]["y"], 3)],
                        "b": [round(ps[j]["x"], 3), round(ps[j]["y"], 3)],
                        "len": round(d, 3)})
            linked.append(j)
            rest.remove(j)
    return out




def jumper_candidates(pads: list[dict], routes: list[dict], jumpers: list[dict]) -> list[dict]:
    """跳线候选（CIR-007）：对每个连通性失败的网络，给出未连通焊盘对的直连候选。"""
    res = check_connectivity(sorted({p["net"] for p in pads if p.get("net")}),
                             pads, routes, jumpers)
    out = []
    for o in res["opens"]:
        ps = [p for p in pads if p.get("net") == o["net"]]
        if len(ps) < 2:
            continue
        # 离断点最近的焊盘对
        best = None
        for a in ps:
            for b in ps:
                if a["id"] >= b["id"]:
                    continue
                d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
                if best is None or d < best[0]:
                    best = (d, a, b)
        if best:
            _, a, b = best
            out.append({"net": o["net"], "from_pad": a["id"], "to_pad": b["id"],
                        "reason": f"网络 {o['net']} 在 ({o['at'][0]},{o['at'][1]}) 断开，"
                                  f"建议 {a['ref']}.{a['pad_no']} ↔ {b['ref']}.{b['pad_no']} 加 0Ω 跳线"})
    return out


# ================================================================ 自动布局（CIR-005，P1）




# ================================================================ 自动布线（CIR-006，P1）

