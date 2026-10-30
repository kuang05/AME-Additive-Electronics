"""routing.py 拆分等价性测试（CIR-005/006 重构回归）。

`auto_route_candidates` 拆分为职责单一的模块级私有函数后，任何数值都不
允许变：本文件把**拆分前的原实现原样内嵌**为参照实现，在真实样本工程的
多个网络、多种宽度/栅格、禁布区、封锁走廊、边界输入下，逐场景断言新旧
实现的候选**完全一致**（数量、pts 顶点序列、length_mm、turns、failed
标志、失败原因）。

参照实现是有意保留的"历史快照"：它就是拆分前 `routing.py` 里的代码，
只改了函数名。若未来算法有意变更，应同步更新参照实现并在提交说明里写明
数值变化——否则本文件的任何失败都意味着拆分/改动破坏了等价性。

注意：A* 与后处理必须确定（依赖插入序 dict、有序堆平手判定）。若拆分引入
set/dict 遍历序等非确定性，`test_auto_route_repeat_calls_are_identical`
与等价性断言会暴露。
"""
from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Polygon, box

from ame.circuit.routing import auto_route_candidates, place_candidates
from ame.geometry.primitives import pad_polygon
from ame.geometry.primitives import route_polygon as route_polyline

from conftest import build_project


# ================================================================ 参照实现（拆分前快照）
def _reference_place_candidates(comps, pads, routes, outline, grid=2.5, top_k=3):
    """拆分前的 place_candidates 原实现（参照快照，勿优化）。"""
    if not comps or len(outline) < 3 or len(comps) < 2:
        return []
    by_ref = {c["ref"]: c for c in comps}
    movable = [c for c in comps if not c.get("locked")]
    if len(movable) < 2:
        return []

    by_net: dict[str, list[dict]] = {}
    for p in pads:
        if p.get("net"):
            by_net.setdefault(p["net"], []).append(p)
    routed = {r["net"] for r in routes}

    def total_len(pos):
        total = 0.0
        for net, ps in by_net.items():
            if net in routed or len(ps) < 2:
                continue
            moved = []
            for p in ps:
                comp = by_ref.get(p.get("ref"))
                if comp is None:
                    moved.append((p["x"], p["y"]))
                else:
                    dx = pos[comp["ref"]][0] - comp["x"]
                    dy = pos[comp["ref"]][1] - comp["y"]
                    moved.append((p["x"] + dx, p["y"] + dy))
            for i in range(len(moved)):
                for j in range(i + 1, len(moved)):
                    total += (abs(moved[i][0] - moved[j][0])
                              + abs(moved[i][1] - moved[j][1]))
        return total

    base_pos = {c["ref"]: (c["x"], c["y"]) for c in comps}
    base_len = total_len(base_pos)

    def improve(start, max_rounds=40):
        pos = dict(start)
        cur = total_len(pos)
        swaps = []
        for _ in range(max_rounds):
            best = None
            for i in range(len(movable)):
                for j in range(i + 1, len(movable)):
                    a, b = movable[i]["ref"], movable[j]["ref"]
                    if pos[a] == pos[b]:
                        continue
                    trial = dict(pos)
                    trial[a], trial[b] = trial[b], trial[a]
                    val = total_len(trial)
                    if val < cur - 1e-9 and (best is None or val < best[0]):
                        best = (val, a, b, trial)
            if best is None:
                break
            cur, a, b, pos = best
            swaps.append([a, b])
        return pos, cur, swaps

    out = []
    pos, val, swaps = improve(base_pos)
    if swaps:
        out.append({"name": "配对交换优化",
                    "positions": {k: [round(v[0], 2), round(v[1], 2)]
                                  for k, v in pos.items()},
                    "ratline_mm": round(val, 1),
                    "baseline_mm": round(base_len, 1), "swaps": swaps})

    net_refs: dict[str, list[str]] = {}
    for net, ps in by_net.items():
        refs = [p.get("ref") for p in ps if p.get("ref") in by_ref]
        if len(refs) >= 2:
            net_refs[net] = refs
    order = sorted(movable,
                   key=lambda c: (-len([1 for rs in net_refs.values() if c["ref"] in rs]),
                                  c["ref"]))
    slots = sorted(base_pos[c["ref"]] for c in movable)
    seed = dict(base_pos)
    for c, slot in zip(order, slots, strict=True):
        seed[c["ref"]] = slot
    pos2, val2, swaps2 = improve(seed)
    if swaps2 and val2 < base_len - 1e-9 and val2 < val:
        out.append({"name": "网络聚簇+交换优化",
                    "positions": {k: [round(v[0], 2), round(v[1], 2)]
                                  for k, v in pos2.items()},
                    "ratline_mm": round(val2, 1),
                    "baseline_mm": round(base_len, 1), "swaps": swaps2})
    out.sort(key=lambda c: c["ratline_mm"])
    return out[:top_k]


def _reference_auto_route_candidates(net, pads, routes, comps, outline,
                                     width=0.6, max_candidates=3,
                                     grid_mm=1.0, keepouts=None):
    """拆分前的 auto_route_candidates 原实现（参照快照，勿优化）。"""
    own = [p for p in pads if p.get("net") == net]
    if len(own) < 2 or len(outline) < 3:
        return []
    own.sort(key=lambda p: (p["x"], p["y"]))
    a, b = own[0], own[-1]
    w2 = width / 2

    poly = Polygon(outline)
    own_refs = {a.get("ref"), b.get("ref")}
    own_ids = {a.get("id"), b.get("id")}
    obstacle_geoms = []       # (geometry, 描述)
    for p in pads:
        if p.get("net") == net or p.get("id") in own_ids:
            continue
        obstacle_geoms.append((pad_polygon(p).buffer(w2),
                               f"焊盘 {p['ref']}.{p['pad_no']}（网络 {p['net']}）"))
    for r in routes:
        if r["net"] == net:
            continue
        g = route_polyline(r["pts"], r["width"])
        if g is not None:
            obstacle_geoms.append((g.buffer(w2), f"走线 {r['net']}"))
    for c in comps:
        if c.get("ref") in own_refs:
            continue
        obstacle_geoms.append((box(c["x"] - 1.5, c["y"] - 1.5,
                                  c["x"] + 1.5, c["y"] + 1.5).buffer(0.82),
                               f"器件 {c['ref']}"))
    for ko in (keepouts or []):
        pts = ko.get("pts") or []
        if len(pts) < 3:
            continue
        try:
            g = Polygon([(float(p[0]), float(p[1])) for p in pts])
            if not g.is_valid:
                g = g.buffer(0)
        except Exception:
            continue
        if g.is_empty:
            continue
        obstacle_geoms.append((g.buffer(w2),
                               f"禁布区 {ko.get('name') or '未命名'}"))

    minx, miny, maxx, maxy = poly.bounds
    nx = max(2, int((maxx - minx) / grid_mm) + 1)
    ny = max(2, int((maxy - miny) / grid_mm) + 1)

    def to_world(ix, iy):
        return (minx + ix * grid_mm, miny + iy * grid_mm)

    def to_cell(x, y):
        ix = int(round((x - minx) / grid_mm))
        iy = int(round((y - miny) / grid_mm))
        return max(0, min(nx - 1, ix)), max(0, min(ny - 1, iy))

    blocked_cells: set = set()
    try:
        from shapely.strtree import STRtree
        from shapely.geometry import Point
        tree = STRtree([g for g, _ in obstacle_geoms])
        for ix in range(nx):
            for iy in range(ny):
                wx, wy = to_world(ix, iy)
                pt = Point(wx, wy)
                if not poly.covers(pt):
                    blocked_cells.add((ix, iy))
                    continue
                for k in tree.query(pt):
                    if obstacle_geoms[int(k)][0].contains(pt):
                        blocked_cells.add((ix, iy))
                        break
    except ImportError:
        for ix in range(nx):
            for iy in range(ny):
                wx, wy = to_world(ix, iy)
                pt = Point(wx, wy)
                if not poly.covers(pt) or any(g.contains(pt)
                                              for g, _ in obstacle_geoms):
                    blocked_cells.add((ix, iy))

    start_cell = to_cell(a["x"], a["y"])
    goal_cell = to_cell(b["x"], b["y"])
    blocked_cells.discard(start_cell)
    blocked_cells.discard(goal_cell)

    def nearest_blocker(cell):
        wx, wy = to_world(*cell)
        pt = Point(wx, wy)
        best = None
        for g, desc in obstacle_geoms:
            d = g.distance(pt)
            if best is None or d < best[0]:
                best = (d, desc)
        return best[1] if best else "未知障碍"

    if start_cell in blocked_cells or goal_cell in blocked_cells:
        return [{"net": net, "failed": True,
                 "reason": f"起点或终点落在障碍内：{nearest_blocker(start_cell)}"}]

    import heapq

    def h(c):
        return max(abs(c[0] - goal_cell[0]), abs(c[1] - goal_cell[1]))

    DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1),
            (1, 1), (1, -1), (-1, 1), (-1, -1)]
    open_heap = [(h(start_cell), 0.0, start_cell, None)]
    best_cost = {start_cell: 0.0}
    came: dict = {start_cell: None}
    move_dir: dict = {start_cell: None}
    found = False
    guard = 0
    while open_heap and guard < 400000:
        guard += 1
        _f, g_cost, cur, prev_dir = heapq.heappop(open_heap)
        if cur == goal_cell:
            found = True
            break
        if g_cost > best_cost.get(cur, 1e18) + 1e-9:
            continue
        for dx, dy in DIRS:
            nxt = (cur[0] + dx, cur[1] + dy)
            if not (0 <= nxt[0] < nx and 0 <= nxt[1] < ny):
                continue
            if nxt in blocked_cells:
                continue
            if dx and dy:
                if (cur[0] + dx, cur[1]) in blocked_cells or (cur[0], cur[1] + dy) in blocked_cells:
                    continue
            step = 1.4142 if (dx and dy) else 1.0
            turn = 0.0 if prev_dir is None or prev_dir == (dx, dy) else 0.35
            ng = g_cost + step + turn
            if ng < best_cost.get(nxt, 1e18) - 1e-9:
                best_cost[nxt] = ng
                came[nxt] = cur
                move_dir[nxt] = (dx, dy)
                heapq.heappush(open_heap, (ng + h(nxt), ng, nxt, (dx, dy)))

    if not found:
        wx, wy = to_world(*start_cell)
        return [{"net": net, "failed": True,
                 "reason": f"无可行路径（A* 未找到连通走廊），最近障碍："
                           f"{nearest_blocker(start_cell)}"}]

    cells = []
    cur = goal_cell
    while cur is not None:
        cells.append(cur)
        cur = came[cur]
    cells.reverse()
    pts = [list(to_world(ix, iy)) for ix, iy in cells]
    pts[0] = [a["x"], a["y"]]
    pts[-1] = [b["x"], b["y"]]

    def simplify_axis(seq):
        if len(seq) <= 2:
            return list(seq)
        out = [seq[0]]
        for i in range(1, len(seq) - 1):
            p0, p1, p2 = out[-1], seq[i], seq[i + 1]
            d1 = (round(p1[0] - p0[0], 6), round(p1[1] - p0[1], 6))
            d2 = (round(p2[0] - p1[0], 6), round(p2[1] - p1[1], 6))
            if d1 != d2:
                out.append(p1)
        out.append(seq[-1])
        return out

    simplified = simplify_axis(pts)
    changed = True
    guard2 = 0
    while changed and guard2 < 4:
        changed = False
        guard2 += 1
        i = 1
        while i < len(simplified) - 1:
            trial = simplified[:i] + simplified[i + 1:]
            tl = LineString(trial)
            if poly.covers(tl) and not any(tl.intersects(g)
                                            for g, _ in obstacle_geoms):
                simplified = trial
                changed = True
            else:
                i += 1

    line = LineString(simplified)
    if not poly.covers(line):
        return [{"net": net, "failed": True, "reason": "路径超出板框"}]
    for g, desc in obstacle_geoms:
        if line.intersects(g):
            return [{"net": net, "failed": True, "reason": f"路径被 {desc} 挡住"}]

    length = sum(math.dist(simplified[i], simplified[i + 1])
                 for i in range(len(simplified) - 1))
    return [{"net": net, "from_pad": a.get("id"), "to_pad": b.get("id"),
             "pts": [[round(x, 3), round(y, 3)] for x, y in simplified],
             "length_mm": round(length, 2), "turns": len(simplified) - 2,
             "width": width, "grid_mm": grid_mm}]


# ================================================================ 对照脚手架
def _assert_routing_equivalent(net, pads, routes, comps, outline, *,
                               keepouts=None, ctx="", **kwargs):
    """新旧实现同输入跑一遍，断言候选完全一致（数量/pts/length_mm/turns/failed）。"""
    new = auto_route_candidates(net, pads, routes, comps, outline,
                                keepouts=keepouts, **kwargs)
    ref = _reference_auto_route_candidates(net, pads, routes, comps, outline,
                                           keepouts=keepouts, **kwargs)
    assert len(new) == len(ref), \
        f"{ctx}网络 {net}：候选数不一致 {len(new)} != {len(ref)}"
    for i, (c_new, c_ref) in enumerate(zip(new, ref, strict=True)):
        assert c_new.get("failed") == c_ref.get("failed"), \
            f"{ctx}网络 {net} 候选{i}：failed 标志不一致"
        assert c_new.get("pts") == c_ref.get("pts"), \
            f"{ctx}网络 {net} 候选{i}：pts 顶点序列不一致\n新:{c_new.get('pts')}\n旧:{c_ref.get('pts')}"
        assert c_new.get("length_mm") == c_ref.get("length_mm"), \
            f"{ctx}网络 {net} 候选{i}：length_mm 不一致"
        assert c_new.get("turns") == c_ref.get("turns"), \
            f"{ctx}网络 {net} 候选{i}：turns 不一致"
        assert c_new == c_ref, \
            f"{ctx}网络 {net} 候选{i}：整体不一致\n新:{c_new}\n旧:{c_ref}"
    return new


def _nets_with_two_pads(state):
    from collections import Counter
    cnt = Counter(p["net"] for p in state["pads"])
    return [n for n, v in sorted(cnt.items()) if v >= 2]


# ================================================================ 等价性：常规布线
@pytest.mark.parametrize("board", ["board_T19A.kicad_pcb", "board_10.kicad_pcb"])
def test_auto_route_equivalence_all_nets_both_boards(data_root, board):
    """真实样本工程（T19A / board_10）全部网络：拆分前后候选完全一致。"""
    ws = build_project(data_root, name=f"EQ-{board}", board=board)
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    keepouts = ws.keepouts()
    nets = _nets_with_two_pads(st)
    assert len(nets) >= 5, "样本工程应有足够多的可布线网络"
    for net in nets:
        out = _assert_routing_equivalent(net, st["pads"], st["routes"],
                                         st["comps"], outline,
                                         keepouts=keepouts, ctx=f"{board}/")
        assert out, f"{board}/{net} 不应返回空"


@pytest.mark.parametrize("width,grid", [(0.6, 1.0), (0.3, 1.0), (1.0, 0.5)])
def test_auto_route_equivalence_width_grid_sweep(data_root, width, grid):
    """不同线宽/栅格参数：代价与避障数值必须逐位一致。"""
    ws = build_project(data_root, name=f"EQ-参数{width}-{grid}",
                       board="board_10.kicad_pcb")
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    for net in _nets_with_two_pads(st)[:6]:
        _assert_routing_equivalent(net, st["pads"], st["routes"], st["comps"],
                                   outline, width=width, grid_mm=grid,
                                   ctx=f"w{width}/g{grid}/")


# ================================================================ 等价性：禁布区
def test_auto_route_equivalence_keepout_detour_and_failure(data_root):
    """大禁布区（CIR-006）：绕行成功与"无可行路径"两条分支都要与原实现一致。"""
    ws = build_project(data_root, name="EQ-禁区", board="board_10.kicad_pcb")
    ws.cmd_add_keepout([[30, 10], [70, 10], [70, 50], [30, 50]], "大禁区")
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    keepouts = ws.keepouts()
    hit_failed = False
    ko = Polygon([[30, 10], [70, 10], [70, 50], [30, 50]])
    for net in _nets_with_two_pads(st):
        out = _assert_routing_equivalent(net, st["pads"], st["routes"],
                                         st["comps"], outline,
                                         keepouts=keepouts, ctx="禁区/")
        hit_failed = hit_failed or any(c.get("failed") for c in out)
        # 禁布区语义不变：成功候选不得穿越禁区
        for c in out:
            if not c.get("failed"):
                band = LineString([(p[0], p[1]) for p in c["pts"]]).buffer(0.3)
                assert band.intersection(ko).area < 1e-6, f"{net} 穿越禁布区"
    assert hit_failed, "该禁布区应迫使部分网络失败（覆盖失败分支）"


def test_auto_route_equivalence_keepout_edge_inputs(data_root):
    """禁布区边界输入（顶点不足/自交/坏点/无名）：与原实现行为一致。"""
    ws = build_project(data_root, name="EQ-禁区边界", board="board_10.kicad_pcb")
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    cases = [
        ("两点", [{"pts": [[0, 0], [1, 1]], "name": "两点"}]),
        ("自交", [{"pts": [[40, 20], [60, 20], [60, 40], [40, 40],
                           [45, 25], [55, 25], [55, 35], [40, 25]],
                   "name": "自交"}]),
        ("坏点", [{"pts": [["a", "b"], [1, 2], [2, 3]], "name": "坏点"}]),
        ("无名", [{"pts": [[10, 20], [50, 20], [50, 35], [10, 35]]}]),
    ]
    for tag, kos in cases:
        for net in ("N2", "N4"):
            _assert_routing_equivalent(net, st["pads"], st["routes"], st["comps"],
                                       outline, keepouts=kos, ctx=f"禁区{tag}/")


# ================================================================ 等价性：封锁与避让
def test_auto_route_equivalence_blocked_corridor(data_root):
    """异网走线封死直连（T08 场景）：失败原因 / 绕行路径与原实现一致。"""
    ws = build_project(data_root, name="EQ-封锁", board="board_10.kicad_pcb")
    for y in (29.0, 30.0, 31.0):
        ws.cmd_add_route("N6", [[28.0, y], [36.0, y]], 0.8)
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    for net in ("N2", "N4"):
        out = _assert_routing_equivalent(net, st["pads"], st["routes"],
                                         st["comps"], outline,
                                         keepouts=ws.keepouts(), ctx="封锁/")
        for c in out:
            if c.get("failed"):
                assert ("挡住" in c["reason"] or "板框" in c["reason"]
                        or "走廊" in c["reason"]), f"{net} 失败原因不可读：{c['reason']}"


def test_auto_route_equivalence_after_placement_and_routing(data_root):
    """应用布局候选、再布 N1 后给其余网络布线（走线成为障碍）：逐网络一致。"""
    ws = build_project(data_root, name="EQ-先布后避", board="board_10.kicad_pcb")
    cands = ws.cmd_place_candidates()
    assert cands, "board_10 应有布局候选"
    ws.cmd_apply_placement(cands[0]["positions"])
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    first = _assert_routing_equivalent("N1", st["pads"], st["routes"],
                                       st["comps"], outline, ctx="布N1前/")
    assert first and not first[0].get("failed"), "N1 应能布通"
    ws.cmd_add_route("N1", first[0]["pts"], first[0]["width"])
    st = ws.mrc_state()
    for net in ("N2", "N3", "N4", "N5"):
        _assert_routing_equivalent(net, st["pads"], st["routes"], st["comps"],
                                   outline, keepouts=ws.keepouts(),
                                   ctx=f"布N1后/{net}/")


# ================================================================ 等价性：边界输入
def test_auto_route_equivalence_edge_inputs(data_root):
    """单焊盘网络 / 退化板框 / 未知网络：新旧实现都返回空。"""
    ws = build_project(data_root, name="EQ-边界", board="board_T19A.kicad_pcb")
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    for net in ("TP1", "TP2"):          # 单焊盘网络
        out = _assert_routing_equivalent(net, st["pads"], st["routes"],
                                         st["comps"], outline, ctx="单焊盘/")
        assert out == []
    for net in ("N1", "N2"):
        out = _assert_routing_equivalent(net, st["pads"], st["routes"],
                                         st["comps"], [[0, 0], [1, 1]],
                                         ctx="退化板框/")
        assert out == []
    out = _assert_routing_equivalent("不存在", st["pads"], st["routes"],
                                     st["comps"], outline, ctx="未知网络/")
    assert out == []


# ================================================================ 确定性
def test_auto_route_repeat_calls_are_identical(data_root):
    """同输入重复调用结果完全一致：拆分不得引入 set/dict 遍历序非确定性。"""
    ws = build_project(data_root, name="EQ-确定性", board="board_10.kicad_pcb")
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    for net in _nets_with_two_pads(st):
        kw = {"keepouts": ws.keepouts()}
        first = auto_route_candidates(net, st["pads"], st["routes"],
                                      st["comps"], outline, **kw)
        second = auto_route_candidates(net, st["pads"], st["routes"],
                                       st["comps"], outline, **kw)
        assert first == second, f"网络 {net} 出现非确定性结果"


def test_auto_route_endpoint_selection_is_stable(data_root):
    """端点选取（按 x,y 排序取首尾）不受拆分影响：from_pad/to_pad 一致。"""
    ws = build_project(data_root, name="EQ-端点", board="board_10.kicad_pcb")
    st = ws.mrc_state()
    outline, _ = ws.board_outline()
    for net in _nets_with_two_pads(st):
        own = sorted([p for p in st["pads"] if p["net"] == net],
                     key=lambda p: (p["x"], p["y"]))
        out = auto_route_candidates(net, st["pads"], st["routes"], st["comps"],
                                    outline)
        ref = _reference_auto_route_candidates(net, st["pads"], st["routes"],
                                               st["comps"], outline)
        assert out == ref
        if out and not out[0].get("failed"):
            assert out[0]["from_pad"] == own[0].get("id")
            assert out[0]["to_pad"] == own[-1].get("id")


# ================================================================ 等价性：布局候选
def test_place_candidates_equivalence_both_boards(data_root):
    """place_candidates 拆分（total_len/improve 提为模块级）：候选完全一致。"""
    for board in ("board_T19A.kicad_pcb", "board_10.kicad_pcb"):
        ws = build_project(data_root, name=f"EQ-布局{board}", board=board)
        st = ws.mrc_state()
        outline, _ = ws.board_outline()
        new = place_candidates(st["comps"], st["pads"], st["routes"], outline)
        ref = _reference_place_candidates(st["comps"], st["pads"], st["routes"],
                                          outline)
        assert new == ref, f"{board}：布局候选与原实现不一致"
        for c in new:               # 原需求：候选飞线必须下降，拆分不得破坏
            assert c["ratline_mm"] < c["baseline_mm"]


def test_place_candidates_equivalence_edge_inputs():
    """空输入 / 顶点不足板框 / 全锁定：新旧实现都返回空。"""
    assert place_candidates([], [], [], []) == \
        _reference_place_candidates([], [], [], []) == []
    comps = [{"ref": "A", "x": 0, "y": 0, "locked": False},
             {"ref": "B", "x": 5, "y": 5, "locked": False}]
    short = [[0, 0], [1, 1]]
    assert place_candidates(comps, [], [], short) == \
        _reference_place_candidates(comps, [], [], short) == []
    locked = [{"ref": "A", "x": 0, "y": 0, "locked": True},
              {"ref": "B", "x": 5, "y": 5, "locked": True}]
    pads = [{"net": "N1", "x": 0, "y": 0, "ref": "A"},
            {"net": "N1", "x": 5, "y": 5, "ref": "B"}]
    outline = [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert place_candidates(locked, pads, [], outline) == \
        _reference_place_candidates(locked, pads, [], outline) == []


# ================================================================ 结构：阶段拆分
def test_routing_split_stage_helpers_exist():
    """auto_route_candidates 的各阶段应拆为模块级私有函数（编排层保持薄）。"""
    import ame.circuit.routing as routing

    for name in ("_pad_obstacles", "_route_obstacles", "_comp_obstacles",
                 "_keepout_obstacles", "_collect_route_obstacles",
                 "_grid_frame", "_grid_to_world", "_grid_to_cell",
                 "_blocked_raster_cells", "_nearest_blocker_desc",
                 "_astar_step_allowed", "_astar_grid_path",
                 "_simplify_collinear", "_prune_redundant_points",
                 "_verify_route_clearance",
                 "_flywire_total", "_greedy_pair_swap",
                 "_cluster_seed_positions", "_placement_candidate"):
        assert callable(getattr(routing, name, None)), f"缺少阶段函数 {name}"


def test_routing_split_no_function_exceeds_complexity_budget():
    """AST 圈复杂度门禁：routing.py 所有函数（嵌套并入父函数计）必须 < 20。"""
    import ast
    from pathlib import Path

    class CC(ast.NodeVisitor):
        def __init__(self):
            self.cc = 1

        def visit_If(self, n):
            self.cc += 1
            self.generic_visit(n)

        def visit_For(self, n):
            self.cc += 1
            self.generic_visit(n)

        def visit_While(self, n):
            self.cc += 1
            self.generic_visit(n)

        def visit_ExceptHandler(self, n):
            self.cc += 1
            self.generic_visit(n)

        def visit_IfExp(self, n):
            self.cc += 1
            self.generic_visit(n)

        def visit_BoolOp(self, n):
            self.cc += len(n.values) - 1
            self.generic_visit(n)

        def visit_comprehension(self, n):
            self.cc += 1 + len(n.ifs)
            self.generic_visit(n)

        def visit_FunctionDef(self, n):
            sub = CC()
            for stmt in n.body:
                sub.visit(stmt)
            self.cc += sub.cc       # 嵌套函数复杂度并入父函数（总计视角）

    src = Path(__file__).resolve().parent.parent / "ame" / "circuit" / "routing.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            v = CC()
            for stmt in node.body:
                v.visit(stmt)
            assert v.cc < 20, \
                f"{node.name} 圈复杂度 {v.cc} ≥ 20，应继续按阶段拆分"


# ================================================================ 多焊盘网络（MST）
class TestMultiPadRouting:
    """≥3 个同网焊盘必须全部接入（早期只连首尾两点，中间焊盘被忽略）。

    这是真实缺陷：电源/地/总线网络常有 3 个以上焊盘，"布通"不等于"全网连通"。
    修法是用最小生成树决定连哪几对，再逐边复用已验证的 A* 寻路。
    """

    @staticmethod
    def _pads(coords, net="N9"):
        return [{"id": f"P{i}", "ref": "U1", "pad_no": str(i),
                 "net": net, "x": x, "y": y, "w": 1.5, "h": 1.0,
                 "shape": "rect", "rot": 0}
                for i, (x, y) in enumerate(coords, 1)]

    def test_three_pads_all_connected(self):
        from ame.circuit.connectivity import check_connectivity
        from ame.circuit.routing import auto_route_candidates

        pads = self._pads([(10, 10), (50, 40), (90, 10)])
        board = [[0, 0], [100, 0], [100, 60], [0, 60]]
        res = auto_route_candidates("N9", pads, [], [], board)
        assert res and not res[0].get("failed"), res
        assert res[0].get("pads_connected") == 3

        # 路径必须真的经过每个焊盘中心
        pts = res[0]["pts"]
        for p in pads:
            assert any(abs(q[0] - p["x"]) < 0.6 and abs(q[1] - p["y"]) < 0.6
                       for q in pts), f"路径未经过焊盘 {p['pad_no']}"

        # 连通性检查必须通过
        routes = [{"id": "R1", "net": "N9", "pts": pts, "width": 0.6}]
        conn = check_connectivity(["N9"], pads, routes, [])
        assert conn["ok"], f"多焊盘网络应全连通，实际断路 {conn['opens']}"

    def test_mst_edge_count_is_n_minus_one(self):
        from ame.circuit.routing import _mst_edges

        pads = self._pads([(0, 0), (10, 0), (20, 0), (30, 0), (40, 0)])
        edges = _mst_edges(pads)
        assert len(edges) == len(pads) - 1, "n 个点的 MST 恰有 n−1 条边"

        # 连通性：每个点都要出现在边里，且不形成环
        seen = set()
        for i, j in edges:
            assert i not in seen or j not in seen, "不应出现环"
            seen.update((i, j))
        assert len(seen) == len(pads)

    def test_mst_is_deterministic(self):
        """同一组点两次构造必须得到同一组边（影响 G-code 可复现性）。"""
        from ame.circuit.routing import _mst_edges

        pads = self._pads([(13, 7), (55, 21), (78, 44), (22, 38)])
        assert _mst_edges(pads) == _mst_edges(pads)

    def test_two_pad_path_unchanged(self):
        """2 个焊盘仍走原路径（不能被 MST 分支改变行为）。"""
        from ame.circuit.routing import auto_route_candidates

        pads = self._pads([(10, 10), (90, 10)])
        board = [[0, 0], [100, 0], [100, 60], [0, 60]]
        res = auto_route_candidates("N9", pads, [], [], board)
        assert res and not res[0].get("failed")
        assert res[0]["pts"][0] == [10, 10]
        assert res[0]["pts"][-1] == [90, 10]

    def test_multi_pad_failure_names_the_pair(self):
        """某条边布不通时，错误必须点名是哪一对焊盘——用户才知道挪哪个器件。"""
        from ame.circuit.routing import auto_route_candidates

        pads = self._pads([(10, 10), (50, 30), (90, 50)])
        # 一条横贯全板的禁布区，把中间焊盘和两侧隔开
        keepouts = [{"pts": [[20, 0], [20, 60], [22, 60], [22, 0]],
                     "name": "隔断"}]
        res = auto_route_candidates("N9", pads, [], [], 
                                    [[0, 0], [100, 0], [100, 60], [0, 60]],
                                    keepouts=keepouts)
        assert res[0].get("failed") is True
        assert "焊盘" in res[0]["reason"], res[0]["reason"]
