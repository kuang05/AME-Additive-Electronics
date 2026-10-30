"""布局与自动布线算法（CIR-005/006）：A* 栅格寻路、器件摆放候选。

从 `connectivity.py` 拆出的"算法层"。原模块同时承担两件不同的事：

- **判定**：走线是否合法、网络是否连通（纯几何/拓扑，结果只有对错）；
- **搜索**：在约束下找一个较好的解（A*、候选排序，结果是"若干可选方案"）。

两者变更频率与测试方式完全不同——判定要逐条覆盖边界，搜索要跑性能基准。
拆开后 `connectivity.py` 只剩判定，本模块可独立做寻路基准。

坐标约定：板坐标 mm；栅格默认 1 mm，8 邻域，代价含转弯惩罚。

`auto_route_candidates` 按阶段拆为模块级私有函数（职责单一，便于逐段测试）：

- 障碍物构建   `_collect_route_obstacles`（焊盘/走线/器件/禁布区 → 几何集合）
- 栅格映射     `_grid_frame` / `_grid_to_world` / `_grid_to_cell`
- 可行栅格     `_blocked_raster_cells`（STRtree 加速，含 ImportError 回退）
- A* 寻路      `_astar_grid_path`（8 邻域 + 转弯惩罚）
- 路径后处理   `_simplify_collinear` / `_prune_redundant_points` / `_verify_route_clearance`
- 失败诊断     `_nearest_blocker_desc`
"""
from __future__ import annotations

import heapq
import math
from typing import NamedTuple

from shapely.geometry import LineString, Point, Polygon, box

from ..geometry.primitives import pad_polygon
from ..geometry.primitives import route_polygon as route_polyline

# A* 扩展方向（顺序影响堆的平手判定，不可重排）
_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1),
         (1, 1), (1, -1), (-1, 1), (-1, -1))


# ================================================================ CIR-005 器件摆放
def _flywire_total(by_net: dict[str, list[dict]], by_ref: dict[str, dict],
                   routed: set[str], pos: dict[str, tuple]) -> float:
    """给定器件位置下的飞线总长：未布线网络内焊盘两两曼哈顿距离之和。"""
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


def _greedy_pair_swap(movable: list[dict], total_of, start: dict[str, tuple],
                      max_rounds: int = 40) -> tuple[dict[str, tuple], float, list]:
    """贪心配对交换：每轮接受收益最大的一对，只降不升。

    返回 (最终位置, 最终飞线长, 交换记录 [[a, b], ...])。
    """
    pos = dict(start)
    cur = total_of(pos)
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
                val = total_of(trial)
                if val < cur - 1e-9 and (best is None or val < best[0]):
                    best = (val, a, b, trial)
        if best is None:
            break
        cur, a, b, pos = best
        swaps.append([a, b])
    return pos, cur, swaps


def _cluster_seed_positions(movable: list[dict], by_net: dict[str, list[dict]],
                            by_ref: dict[str, dict],
                            base_pos: dict[str, tuple]) -> dict[str, tuple]:
    """第二候选的种子布局：按"参与网络数"把器件重排到原位置的槽位。"""
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
    return seed


def _placement_candidate(name: str, pos: dict[str, tuple], val: float,
                         base_len: float, swaps: list) -> dict:
    """把一次优化结果包装成候选 dict（位置保留两位小数）。"""
    return {"name": name,
            "positions": {k: [round(v[0], 2), round(v[1], 2)]
                          for k, v in pos.items()},
            "ratline_mm": round(val, 1),
            "baseline_mm": round(base_len, 1), "swaps": swaps}


def place_candidates(comps: list[dict], pads: list[dict], routes: list[dict],
                     outline: list, grid: float = 2.5, top_k: int = 3) -> list[dict]:
    """器件摆放候选（CIR-005）：在现有布局上做“交换两个器件”的局部改进。

    每次交换后按飞线总长评估，只接受严格变短的交换；因此候选的飞线长度
    **一定不劣于**当前布局（需求要求“应用候选后飞线总长度下降”）。
    候选只给建议位置，不修改工程（应用走 cmd_apply_placement，可撤销）。
    """
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

    def total_of(pos: dict[str, tuple]) -> float:
        return _flywire_total(by_net, by_ref, routed, pos)

    base_pos = {c["ref"]: (c["x"], c["y"]) for c in comps}
    base_len = total_of(base_pos)

    out = []
    pos, val, swaps = _greedy_pair_swap(movable, total_of, base_pos)
    if swaps:
        out.append(_placement_candidate("配对交换优化", pos, val, base_len, swaps))

    # 第二候选：先按“参与网络数”重排到原位置的槽位，再做同样的局部优化
    seed = _cluster_seed_positions(movable, by_net, by_ref, base_pos)
    pos2, val2, swaps2 = _greedy_pair_swap(movable, total_of, seed)
    if swaps2 and val2 < base_len - 1e-9 and val2 < val:
        out.append(_placement_candidate("网络聚簇+交换优化", pos2, val2, base_len, swaps2))
    out.sort(key=lambda c: c["ratline_mm"])
    return out[:top_k]


def count_overlaps(pos: dict[str, tuple], size: float = 3.0) -> int:
    """候选方案里器件重叠数量（可打性排序用）。"""
    items = list(pos.values())
    n = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if abs(items[i][0] - items[j][0]) < size and \
                    abs(items[i][1] - items[j][1]) < size:
                n += 1
    return n


def total_ratline_length(pads: list[dict], routes: list[dict]) -> float:
    """飞线总长（CIR-005 排序用）：同网络未连通焊盘对的曼哈顿距离。"""
    by_net: dict[str, list[dict]] = {}
    for p in pads:
        if p.get("net"):
            by_net.setdefault(p["net"], []).append(p)
    routed_nets = {r["net"] for r in routes}
    total = 0.0
    for net, ps in by_net.items():
        if net in routed_nets:
            continue
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                total += abs(ps[i]["x"] - ps[j]["x"]) + abs(ps[i]["y"] - ps[j]["y"])
    return total


# ================================================================ CIR-006 障碍物构建
def _pad_obstacles(net: str, pads: list[dict], own_ids: set,
                   w2: float) -> list[tuple]:
    """异网焊盘障碍（外扩半线宽）。同网焊盘与起止焊盘不是障碍。"""
    out: list[tuple] = []
    for p in pads:
        if p.get("net") == net or p.get("id") in own_ids:
            continue
        out.append((pad_polygon(p).buffer(w2),
                    f"焊盘 {p['ref']}.{p['pad_no']}（网络 {p['net']}）"))
    return out


def _route_obstacles(net: str, routes: list[dict], w2: float) -> list[tuple]:
    """异网已布走线障碍（外扩半线宽）。"""
    out: list[tuple] = []
    for r in routes:
        if r["net"] == net:
            continue
        g = route_polyline(r["pts"], r["width"])
        if g is not None:
            out.append((g.buffer(w2), f"走线 {r['net']}"))
    return out


def _comp_obstacles(comps: list[dict], own_refs: set) -> list[tuple]:
    """非起止器件本体障碍（3×3 本体外扩 0.82 mm 喷头安全半径）。"""
    out: list[tuple] = []
    for c in comps:
        if c.get("ref") in own_refs:
            continue
        out.append((box(c["x"] - 1.5, c["y"] - 1.5,
                        c["x"] + 1.5, c["y"] + 1.5).buffer(0.82),
                    f"器件 {c['ref']}"))
    return out


def _keepout_obstacles(keepouts: list | None, w2: float) -> list[tuple]:
    """禁布区障碍（CIR-006）：用户指定的不允许走线的区域，外扩半线宽保证
    导体不越界。顶点不足、非法（自交）或退化的多边形直接跳过。"""
    out: list[tuple] = []
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
        out.append((g.buffer(w2),
                    f"禁布区 {ko.get('name') or '未命名'}"))
    return out


def _collect_route_obstacles(net: str, pads: list[dict], routes: list[dict],
                             comps: list[dict], own_ids: set, own_refs: set,
                             w2: float, keepouts: list | None) -> list[tuple]:
    """布线障碍全集：(几何, 描述) 列表。

    顺序固定为 焊盘 → 走线 → 器件 → 禁布区（各输入的原顺序）；
    该顺序参与“最近障碍”的平手判定与复核报错，不可重排。
    """
    return (_pad_obstacles(net, pads, own_ids, w2)
            + _route_obstacles(net, routes, w2)
            + _comp_obstacles(comps, own_refs)
            + _keepout_obstacles(keepouts, w2))


# ================================================================ CIR-006 栅格映射
class _GridFrame(NamedTuple):
    """栅格坐标系：板框左下角原点 + 栅距 + 两轴格数。"""
    minx: float
    miny: float
    grid_mm: float
    nx: int
    ny: int


def _grid_frame(poly: Polygon, grid_mm: float) -> _GridFrame:
    """由板框外接矩形确定栅格规模（每轴至少 2 格）。"""
    minx, miny, maxx, maxy = poly.bounds
    return _GridFrame(minx=minx, miny=miny, grid_mm=grid_mm,
                      nx=max(2, int((maxx - minx) / grid_mm) + 1),
                      ny=max(2, int((maxy - miny) / grid_mm) + 1))


def _grid_to_world(frame: _GridFrame, ix: int, iy: int) -> tuple[float, float]:
    """格索引 → 世界坐标（格中心即格点）。"""
    return (frame.minx + ix * frame.grid_mm, frame.miny + iy * frame.grid_mm)


def _grid_to_cell(frame: _GridFrame, x: float, y: float) -> tuple[int, int]:
    """世界坐标 → 最近格索引，并夹到板框栅格范围内。"""
    ix = int(round((x - frame.minx) / frame.grid_mm))
    iy = int(round((y - frame.miny) / frame.grid_mm))
    return max(0, min(frame.nx - 1, ix)), max(0, min(frame.ny - 1, iy))


def _blocked_raster_cells(frame: _GridFrame, poly: Polygon,
                          obstacle_geoms: list[tuple]) -> set[tuple[int, int]]:
    """预计算不可行栅格：出板框的格 + 被任一障碍覆盖的格。

    优先用 STRtree 加速"该格是否被障碍覆盖"查询；无 strtree 时回退为
    逐障碍遍历（结果一致）。
    """
    blocked_cells: set[tuple[int, int]] = set()
    try:
        from shapely.strtree import STRtree

        tree = STRtree([g for g, _ in obstacle_geoms])
        for ix in range(frame.nx):
            for iy in range(frame.ny):
                wx, wy = _grid_to_world(frame, ix, iy)
                pt = Point(wx, wy)
                if not poly.covers(pt):
                    blocked_cells.add((ix, iy))
                    continue
                for k in tree.query(pt):
                    if obstacle_geoms[int(k)][0].contains(pt):
                        blocked_cells.add((ix, iy))
                        break
    except ImportError:
        for ix in range(frame.nx):
            for iy in range(frame.ny):
                wx, wy = _grid_to_world(frame, ix, iy)
                pt = Point(wx, wy)
                if not poly.covers(pt) or any(g.contains(pt)
                                              for g, _ in obstacle_geoms):
                    blocked_cells.add((ix, iy))
    return blocked_cells


def _nearest_blocker_desc(cell: tuple[int, int], frame: _GridFrame,
                          obstacle_geoms: list[tuple]) -> str:
    """离该格中心最近的障碍描述（失败原因用，帮用户判断该挪谁）。"""
    wx, wy = _grid_to_world(frame, *cell)
    pt = Point(wx, wy)
    best = None
    for g, desc in obstacle_geoms:
        d = g.distance(pt)
        if best is None or d < best[0]:
            best = (d, desc)
    return best[1] if best else "未知障碍"


# ================================================================ CIR-006 A* 寻路
def _astar_step_allowed(cur: tuple[int, int], nxt: tuple[int, int],
                        dx: int, dy: int, blocked: set,
                        frame: _GridFrame) -> bool:
    """单步扩展的合法性：不出界、不撞阻挡格；对角移动不允许“擦角穿墙”。"""
    if not (0 <= nxt[0] < frame.nx and 0 <= nxt[1] < frame.ny):
        return False
    if nxt in blocked:
        return False
    if dx and dy:
        if (cur[0] + dx, cur[1]) in blocked or (cur[0], cur[1] + dy) in blocked:
            return False
    return True


def _astar_grid_path(start_cell: tuple[int, int], goal_cell: tuple[int, int],
                     blocked_cells: set[tuple[int, int]],
                     frame: _GridFrame) -> list[tuple[int, int]] | None:
    """A*：8 邻域，直线代价 1、对角 1.4142，转弯 +0.35 惩罚避免锯齿。

    返回 start→goal 的格序列；无可行路径（含 400000 步保护上限）返回 None。
    """
    def h(c):
        return max(abs(c[0] - goal_cell[0]), abs(c[1] - goal_cell[1]))

    open_heap = [(h(start_cell), 0.0, start_cell, None)]
    best_cost = {start_cell: 0.0}
    came: dict[tuple, tuple | None] = {start_cell: None}
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
        for dx, dy in _DIRS:
            nxt = (cur[0] + dx, cur[1] + dy)
            if not _astar_step_allowed(cur, nxt, dx, dy, blocked_cells, frame):
                continue
            step = 1.4142 if (dx and dy) else 1.0
            turn = 0.0 if prev_dir is None or prev_dir == (dx, dy) else 0.35
            ng = g_cost + step + turn
            if ng < best_cost.get(nxt, 1e18) - 1e-9:
                best_cost[nxt] = ng
                came[nxt] = cur
                heapq.heappush(open_heap, (ng + h(nxt), ng, nxt, (dx, dy)))

    if not found:
        return None
    cells = []
    cur: tuple[int, int] | None = goal_cell
    while cur is not None:
        cells.append(cur)
        cur = came[cur]
    cells.reverse()
    return cells


# ================================================================ CIR-006 路径后处理
def _simplify_collinear(seq: list) -> list:
    """共线合并：只保留方向改变的点（栅格路径大部分是直线段）。"""
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


def _prune_redundant_points(simplified: list, poly: Polygon,
                            obstacle_geoms: list[tuple]) -> list:
    """逐点贪心删减：若删掉某点后前后直连仍不穿障碍，就删掉它。

    A* 栅格路径常有"沿边走一格"的冗余拐点，用户看到会以为走线很乱。
    最多扫 4 轮防止退化死循环。
    """
    changed = True
    guard = 0
    while changed and guard < 4:
        changed = False
        guard += 1
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
    return simplified


def _verify_route_clearance(line: LineString, poly: Polygon,
                            obstacle_geoms: list[tuple]) -> str | None:
    """精确几何复核（栅格近似可能擦边）：返回失败原因，通过则返回 None。"""
    if not poly.covers(line):
        return "路径超出板框"
    for g, desc in obstacle_geoms:
        if line.intersects(g):
            return f"路径被 {desc} 挡住"
    return None


# ================================================================ CIR-006 候选入口
def _route_multi_pad(net: str, own: list[dict], pads: list[dict],
                     routes: list[dict], comps: list[dict], outline: list,
                     width: float, grid_mm: float,
                     keepouts: list | None) -> list[dict]:
    """多焊盘网络布线：MST 定边 → 逐边 A* → 合成单条连通路径。

    为什么合成一条而不是返回多条：切片的银浆道次对同一网络只需一条连通路径；
    多条独立路径在转折点处可能不接触，反而切开连通性。
    """
    edges = _mst_edges(own)
    leg = _route_all_legs(net, own, edges, routes, comps, outline,
                          width, grid_mm, keepouts)
    if isinstance(leg, str):           # 某条边失败，leg 是失败原因
        return [{"net": net, "failed": True, "reason": leg}]

    order = _edge_chain_order(edges, len(own))
    pts = _join_legs([leg[e] for e in order])
    if len(pts) < 2:
        return [{"net": net, "failed": True, "reason": "多焊盘路径合成失败"}]
    length = sum(math.dist(pts[k][:2], pts[k + 1][:2]) for k in range(len(pts) - 1))
    return [{"net": net, "from_pad": own[0].get("id"),
             "to_pad": own[-1].get("id"), "pads_connected": len(own),
             "pts": pts, "length_mm": round(length, 2),
             "turns": max(0, len(pts) - 2), "width": width, "grid_mm": grid_mm}]


def _route_all_legs(net: str, own: list[dict], edges: list[tuple[int, int]],
                    routes: list[dict], comps: list[dict], outline: list,
                    width: float, grid_mm: float,
                    keepouts: list | None):
    """对 MST 的每条边单独寻路。返回路径列表；任一失败则返回失败原因字符串。

    复用单边 A*（已验证），不另写多点寻路——多点的风险在"连哪几对"，
    不在"怎么连一条"。
    """
    leg_pts: list[list[list[float]]] = []
    for (i, j) in edges:
        res = auto_route_candidates(net, [own[i], own[j]], routes, comps,
                                    outline, width=width, grid_mm=grid_mm,
                                    keepouts=keepouts)
        if not res or res[0].get("failed"):
            why = res[0].get("reason", "未知原因") if res else "无候选"
            a, b = own[i], own[j]
            return (f"焊盘 {a.get('ref')}.{a.get('pad_no')} → "
                    f"{b.get('ref')}.{b.get('pad_no')} 无法布通：{why}")
        leg_pts.append(res[0]["pts"])
    return leg_pts


def _edge_chain_order(edges: list[tuple[int, int]], n_nodes: int) -> list[int]:
    """把 MST 的边排成一条可依次拼接的顺序（从度为 1 的节点深度优先）。

    为什么要排序：各边是独立寻路的，必须按"接得上"的顺序拼，
    否则合成后的折线会出现大跨度跳线（视觉上穿过整板）。
    """
    graph: dict[int, list[int]] = {k: [] for k in range(n_nodes)}
    for e, (i, j) in enumerate(edges):
        graph[i].append(e)
        graph[j].append(e)
    order: list[int] = []
    visited: set[int] = set()

    def walk(node: int) -> None:
        for e in graph[node]:
            if e in visited:
                continue
            visited.add(e)
            order.append(e)
            i, j = edges[e]
            walk(j if i == node else i)

    for k in range(n_nodes):
        if len(graph[k]) == 1:      # 从端点起步，避免从中间接出分叉
            walk(k)
            break
    for e in range(len(edges)):     # 兜底：全闭环（MST 不会闭环，防御性保留）
        if e not in visited:
            order.append(e)
    return order


def _join_legs(legs: list[list]) -> list[list[float]]:
    """把已按拼接顺序排好的各段路径连成一条折线，自动选"接得上的那一端"。

    排序在上游 `_edge_chain_order` 完成；这里只管几何拼接。
    """
    pts: list[list[float]] = []
    for leg in legs:
        if not pts:
            pts.extend(leg)
            continue
        d_head = math.dist(pts[-1][:2], leg[0][:2])
        d_tail = math.dist(pts[-1][:2], leg[-1][:2])
        pts.extend(leg[1:] if d_head <= d_tail else list(reversed(leg))[1:])
    return pts


def _mst_edges(points: list[dict]) -> list[tuple[int, int]]:
    """按欧氏距离构造最小生成树的边（Prim 算法）。

    为什么需要：一个网络可能有 3 个以上同网焊盘（电源、地、总线最常见）。
    早期实现只取"排序后的首尾两个焊盘"接一条线，中间焊盘完全没接入——
    这意味着"布通"不等于"全网连通"。用 MST 决定"连哪几对"是标准做法：
    n 个焊盘只需 n−1 条边即可全连通，且总长度最小（在完全图上）。

    这不是严格的 Steiner 树（不允许在空白处引入新的分支点），
    但对"必须落在焊盘上"的 PCB 布线来说 MST 已经足够——
    Steiner 点的收益只在线长上，而 PCB 里多一个 T 型节点反而增加工艺风险。
    """
    n = len(points)
    if n < 2:
        return []
    coord = [(float(p["x"]), float(p["y"])) for p in points]
    in_tree = [False] * n
    in_tree[0] = True
    edges: list[tuple[int, int]] = []
    # best[j] = (到树的最小距离, 连到树里的哪个点)
    best = [(math.dist(coord[0], coord[j]), 0) for j in range(n)]
    for _ in range(n - 1):
        # 选一个不在树里且距离最近的点（平手时按下标，保证确定性）
        cand = min((j for j in range(n) if not in_tree[j]),
                   key=lambda j: (best[j][0], j))
        edges.append((best[cand][1], cand))
        in_tree[cand] = True
        for j in range(n):
            if in_tree[j]:
                continue
            d = math.dist(coord[cand], coord[j])
            if d < best[j][0] - 1e-12:
                best[j] = (d, cand)
    return edges


def auto_route_candidates(net: str, pads: list[dict], routes: list[dict],
                          comps: list[dict], outline: list,
                          width: float = 0.6, max_candidates: int = 3,
                          grid_mm: float = 1.0, keepouts: list | None = None) -> list[dict]:
    """对单个网络生成避障走线候选（CIR-006）。

    用 A* 在板框内做栅格寻路（默认 1 mm 栅格）：
      - 障碍 = 异网焊盘/走线（外扩线宽/2）+ 非起止器件本体（外扩喷头安全半径）
        + **禁布区**（CIR-006 明确要求"避开焊盘、已布线和禁布区"）；
      - 起止焊盘允许作为出入口（其所属器件不视为障碍）；
      - 路径按正/斜 8 邻域扩展，代价含转弯惩罚，避免锯齿；
      - 结果做共线点合并后再用精确几何复核。
    失败时说明被谁挡住（取最近障碍）。候选按“转弯数 + 长度”排序，不自动应用。

    keepouts: [{"pts": [[x,y],...], "name": "..."}] 多边形禁布区。

    阶段编排：障碍构建 → 可行栅格 → A* → 端点对齐/简化/复核 → 候选装配。
    """
    own = [p for p in pads if p.get("net") == net]
    if len(own) < 2 or len(outline) < 3:
        return []
    own.sort(key=lambda p: (p["x"], p["y"]))

    # 多焊盘网络（≥3）：用 MST 决定要连哪几对，逐边寻路后合成一条连通路径。
    # 早期只取首尾两点，中间焊盘不接入——"布通"不等于"全网连通"。
    if len(own) > 2:
        return _route_multi_pad(net, own, pads, routes, comps, outline,
                                width, grid_mm, keepouts)

    a, b = own[0], own[-1]
    w2 = width / 2

    poly = Polygon(outline)
    own_refs = {a.get("ref"), b.get("ref")}
    own_ids = {a.get("id"), b.get("id")}
    obstacle_geoms = _collect_route_obstacles(net, pads, routes, comps,
                                              own_ids, own_refs, w2, keepouts)

    frame = _grid_frame(poly, grid_mm)
    blocked_cells = _blocked_raster_cells(frame, poly, obstacle_geoms)

    # 起止格：焊盘中心格，若被自身焊盘外扩区判为阻挡也要放行
    start_cell = _grid_to_cell(frame, a["x"], a["y"])
    goal_cell = _grid_to_cell(frame, b["x"], b["y"])
    blocked_cells.discard(start_cell)
    blocked_cells.discard(goal_cell)

    if start_cell in blocked_cells or goal_cell in blocked_cells:
        return [{"net": net, "failed": True,
                 "reason": "起点或终点落在障碍内："
                           f"{_nearest_blocker_desc(start_cell, frame, obstacle_geoms)}"}]

    cells = _astar_grid_path(start_cell, goal_cell, blocked_cells, frame)
    if cells is None:
        # 说明最近的阻挡物，帮用户判断该挪器件还是该拉间距
        return [{"net": net, "failed": True,
                 "reason": f"无可行路径（A* 未找到连通走廊），最近障碍："
                           f"{_nearest_blocker_desc(start_cell, frame, obstacle_geoms)}"}]

    pts = [list(_grid_to_world(frame, ix, iy)) for ix, iy in cells]
    # 端点精确对齐焊盘中心
    pts[0] = [a["x"], a["y"]]
    pts[-1] = [b["x"], b["y"]]
    simplified = _prune_redundant_points(_simplify_collinear(pts),
                                         poly, obstacle_geoms)

    line = LineString(simplified)
    reason = _verify_route_clearance(line, poly, obstacle_geoms)
    if reason is not None:
        return [{"net": net, "failed": True, "reason": reason}]

    length = sum(math.dist(simplified[i], simplified[i + 1])
                 for i in range(len(simplified) - 1))
    return [{"net": net, "from_pad": a.get("id"), "to_pad": b.get("id"),
             "pts": [[round(x, 3), round(y, 3)] for x, y in simplified],
             "length_mm": round(length, 2), "turns": len(simplified) - 2,
             "width": width, "grid_mm": grid_mm}]
