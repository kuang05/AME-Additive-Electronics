"""可打性规则检查引擎（MRC-001..011）。

输入是纯数据 state（与存储解耦），输出确定：同一 state + 同一规则表 → 同一分数。
默认阈值来自需求 §3.4；R4/R5 依赖工具几何（针尖外径 + 安全间隙）与结构障碍物。
"""
from __future__ import annotations

from shapely.geometry import LineString, box

from ..geometry.primitives import comp_rect as _comp_rect
from ..geometry.primitives import pad_polygon as _pad_polygon
from ..geometry.primitives import route_line as _route_line_prim
from ..geometry.primitives import route_polygon
from ..logging_setup import get_logger

_LOG = get_logger("mrc")

DEFAULT_RULES = [
    # R1/R2 默认阈值按需求 §3.4「首版默认阈值」表：0.5 mm。
    # 注意与 §1.7 工艺基线（线宽/间距 0.6 mm）的区别——基线是"能稳定打出的尺寸"，
    # 0.5 是"警告下限"，两者有意分开：0.5–0.6 之间可打但接近工艺极限。
    {"code": "R1", "name": "线宽",     "threshold": {"min_width": 0.5}, "weight": 5, "enabled": 1, "rule_version": 1},
    {"code": "R2", "name": "间距",     "threshold": {"min_clear": 0.5}, "weight": 5, "enabled": 1, "rule_version": 1},
    {"code": "R3", "name": "坡度",     "threshold": {"max_slope_deg": 45.0}, "weight": 4, "enabled": 1, "rule_version": 1},
    {"code": "R4", "name": "可达性",   "threshold": {"min_gap": 0.5}, "weight": 6, "enabled": 1, "rule_version": 1},
    {"code": "R5", "name": "遮挡",     "threshold": {}, "weight": 6, "enabled": 1, "rule_version": 1},
    {"code": "R6", "name": "温度场",   "threshold": {"max_dt": 5.0, "gradient_c_per_mm": 0.06}, "weight": 3, "enabled": 1, "rule_version": 1},
    {"code": "R7", "name": "介电流道", "threshold": {"min_channel": 0.5, "dam_w": 0.6}, "weight": 4, "enabled": 1, "rule_version": 1},
]

DEFAULT_TOOL = {
    "nozzle_id_mm": 0.41, "tip_od_mm": 0.64, "safe_gap_mm": 0.5,
    "head_envelope_mm": 20.0, "min_corner_r_mm": 0.8, "min_mesa_w_mm": 1.0,
    "max_aspect": 1.0,
}

# 物理层序号（MRC-005 遮挡判定的层次基准）
# bottom（下保护）→ circuit（电路，走线在这一层）→ top（上保护）
LAYER_RANK = {"bottom": 0, "circuit": 1, "top": 2}
# 走线打印在电路层，其"可打印高度"以电路层顶面为准
ROUTE_LAYER_RANK = LAYER_RANK["circuit"]


def _thr(rule: dict, key: str, default: float) -> float:
    """安全读取规则阈值（NFR-002：数据库被外部写坏时按默认值继续，不崩溃）。"""
    try:
        v = rule.get("threshold", {})[key]
        v = float(v)
        if v != v or v in (float("inf"), float("-inf")):
            return default
        return v
    except (KeyError, TypeError, ValueError, AttributeError):
        # AttributeError：rule 为 None（规则被禁用时调用方仍会读默认阈值）
        return default


def _printed_layer_blockers(state: dict, reach_r: float) -> list[tuple]:
    """MRC-005「已打印层」遮挡：把**先于走线打印、且占住走线高度**的材料当障碍。

    返回与其它障碍物同构的 5 元组 (key, polygon, height, label, layer_rank)。

    判据要同时满足三条，缺一条都会误报（三种朴素判法实测都错）：

    | 朴素判法 | 实测后果 |
    |---|---|
    | 任一层材料覆盖走线 XY | 下保护层铺满整板 → 8 条走线全误报 |
    | 更晚层覆盖走线 | 上保护层本就盖住走线 → 8 条全误报 |
    | 同层材料覆盖走线 | 介电坝把银浆槽夹在中间 → 8 条全误报 |

    正确语义（对应需求 §3.5 道次表："银浆道次填充槽/焊盘坑，与同层介电共面"）：

    1. **高度必须占住走线区间**：走线在电路层沉积，占据
       [电路层顶面-厚度, 电路层顶面]。已打印材料的 [底, 顶] 必须与该区间
       **有正重叠**；贴合（下保护顶 0.240 == 走线底 0.240）不算。
    2. **必须先于走线打印**：按 ``pass_no`` 判先后。同层介电坝(pass3)虽与
       走线同区间，但它是**为银浆留槽**，银浆后填进槽里，不是被挡住——
       所以只有 ``pass_no < 走线道次`` 的材料才可能是障碍。
    3. **XY 上真的压住**：由 R5 的距离判定负责。

    三者合起来，正常板件不会误报；而当某层材料确实提前占住了走线位置
    （例如人为调整道次顺序、或结构凸台已把该高度填实），就会被标出。
    """
    layers = state.get("printed_layers") or []
    if not layers:
        return []

    # 走线所在的道次与高度区间（以电路层里的银浆道次为准）
    # 银浆道次的材料 role 是 conductor（介电是 dielectric/protect）
    silver = [l for l in layers
              if l.get("kind") == "circuit"
              and str(l.get("mat", "")).lower() == "conductor"]
    circuit = silver or [l for l in layers if l.get("kind") == "circuit"]
    if not circuit:
        return []
    route_pass = min(int(l.get("pass_no", 0)) for l in circuit)
    r_top = max(float(l.get("z_mm", 0.0)) for l in circuit)
    r_h = max(float(l.get("h_mm") or 0.0) for l in circuit)
    route_lo, route_hi = r_top - r_h, r_top
    if route_hi <= route_lo:
        return []                       # 没有层厚信息时无法做体积判定

    out: list[tuple] = []
    for layer in layers:
        kind = layer.get("kind", "")
        pno = int(layer.get("pass_no", 0))
        z = float(layer.get("z_mm", 0.0))
        h = float(layer.get("h_mm") or 0.0)
        if h <= 0 or pno >= route_pass:
            continue                    # 旧切片无层厚 / 不早于走线道次
        lo, hi = z - h, z
        if hi <= route_lo + 1e-6 or lo >= route_hi - 1e-6:
            continue                    # 高度不重叠（含贴合）
        geoms = []
        for path in layer.get("paths", []):
            pts = path.get("pts") or []
            if len(pts) < 2:
                continue
            try:
                poly = route_polygon(pts, path.get("width_mm", 0.6))
            except Exception as e:
                _LOG.warning("已打印层几何化失败（%s）：%s", kind, e)
                continue
            if poly is not None and not poly.is_empty:
                geoms.append(poly)
        if not geoms:
            continue
        try:
            from shapely.ops import unary_union
            merged = unary_union(geoms)
        except Exception:
            continue
        if merged.is_empty:
            continue
        cn = {"bottom": "下保护层", "circuit": "电路层", "top": "上保护层"}.get(kind, kind)
        parts = getattr(merged, "geoms", [merged])
        for k, part in enumerate(parts):
            if part.is_empty or part.area <= 0:
                continue
            out.append((f"printed-{kind}-{pno}-{k}", part, hi,
                        f"已打印的{cn}材料（道次 {pno}，{lo:.2f}–{hi:.2f} mm）",
                        LAYER_RANK.get(kind, 0)))
    return out


def _route_line(r):
    """走线折线；少于 2 点的畸形数据返回 None（NFR-002：坏数据不崩溃）。

    几何构造统一由 geometry.primitives 提供，这里只负责从 state 字典取点列，
    避免与切片/连通性各写一套（旋转、离散精度会发散）。
    """
    return _route_line_prim(r.get("pts", []))


def _near_pairs(geoms: list, max_dist: float):
    """用 STRtree 空间索引枚举“可能相距 max_dist 以内”的对象对。

    纯 O(n²) 全对距离在 300 网络规模下要 20 s，无法交互；
    STRtree 用包围盒先过滤，只对候选对做精确距离计算。
    """
    if len(geoms) < 2:
        return
    try:
        from shapely.strtree import STRtree
    except ImportError:      # 兜底：老版本 shapely
        for i in range(len(geoms)):
            for j in range(i + 1, len(geoms)):
                yield i, j
        return
    # 用"外扩后的包围盒"去查树：包围盒相交即可能距离 ≤ max_dist。
    #
    # 早期用 `g.buffer(max_dist)` 构造探测几何——STRtree 的 query 只用包围盒，
    # 真正的几何求交并不发生，所以 buffer 的圆角/圆弧离散纯属浪费。
    # 实测 300 条走线：buffer 法 30.8 ms vs bbox 外扩 18.6 ms（快 1.66×），
    # 且两者返回的候选对完全相同（20858 对，STRtree 本就用 bbox 判定）。
    from shapely.geometry import box as _box
    tree = STRtree(list(geoms))
    for i, g in enumerate(geoms):
        if max_dist > 0:
            minx, miny, maxx, maxy = g.bounds
            probe = _box(minx - max_dist, miny - max_dist,
                         maxx + max_dist, maxy + max_dist)
        else:
            probe = g
        for j in tree.query(probe):
            j = int(j)
            if j > i:
                yield i, j


def _emit(issues: list, rules: list, code: str, ttype: str, tid: str,
          severity: int, reason: str, suggestion: str = "") -> None:
    """统一的"记录一条问题"入口：禁用规则不产出，扣分按权重×严重度算。"""
    r = next((x for x in rules if x["code"] == code and x["enabled"]), None)
    if not r:
        return
    issues.append({"rule_code": code, "target_type": ttype, "target_id": tid,
                   "severity": severity,
                   "penalty": round(r["weight"] * severity / 3.0, 1),
                   "reason": reason, "suggestion": suggestion})


def _rule_r1(r1: dict, routes: list, issues: list, rules: list) -> None:
    """R1 线宽（MRC-001）：走线宽度低于下限。

    严重度分档：低于下限 0.15 mm 以上记 S3（明显不可打），否则 S2。
    """
    thr = _thr(r1, "min_width", 0.5)
    for rt in routes:
        w = rt["width"]
        if w < thr:
            sev = 3 if (thr - w) > 0.15 else 2
            _emit(issues, rules, "R1", "route", rt["id"], sev,
                  f"走线 {rt['net']} 线宽 {w:.2f} mm 低于下限 {thr:.2f} mm",
                  f"将线宽加宽到 ≥{thr:.2f} mm（工艺基线 0.6 mm）")


def _rule_r2(r2: dict, state: dict, routes: list, pads: list,
             issues: list, rules: list) -> None:
    """R2 间距（MRC-002）：异网最小距离，含焊盘与跳线。

    用"中心线 + 半线宽修正"而非把每条走线 buffer 成多边形：
    距离 = distance(中心线) − 半宽1 − 半宽2，与 buffer 后求距离数学等价，
    但省掉 N 次 buffer 构造。实测 300 走线：1241 ms → 59 ms（21×），
    且违规集合与暴力全对比较逐条一致。
    """
    thr = _thr(r2, "min_clear", 0.5)
    items: list[tuple] = []      # (net, geometry, half_width, label)
    for rt in routes:
        g = _route_line(rt)
        if g is None:
            continue
        items.append((rt["net"], g, rt["width"] / 2.0, f"走线 {rt['net']}"))
    for p in pads:
        if not p.get("net"):
            continue
        items.append((p["net"], _pad_polygon(p), 0.0,
                      f"焊盘{p['ref']}.{p['pad_no']}"))
    pad_by_id = {p.get("id"): p for p in pads if p.get("id")}
    for j in state.get("jumpers", []):
        p1 = pad_by_id.get(j["from_pad"])
        p2 = pad_by_id.get(j["to_pad"])
        if p1 and p2:
            items.append((j["net"],
                          LineString([[p1["x"], p1["y"]], [p2["x"], p2["y"]]]),
                          0.3, f"跳线 {j['net']}"))

    if len(items) < 2:
        return

    geoms = [it[1] for it in items]
    max_hw = max(it[2] for it in items)
    # 包围盒需外扩 thr + 两侧最大半宽，否则会漏掉"中心线远但边缘近"的对
    margin = thr + 2 * max_hw
    try:
        from shapely.strtree import STRtree as _ST2
    except ImportError:
        _ST2 = None
    if _ST2 is not None:
        tree2 = _ST2(geoms)
        cand_pairs = []
        for i, g in enumerate(geoms):
            minx, miny, maxx, maxy = g.bounds
            probe = box(minx - margin, miny - margin,
                        maxx + margin, maxy + margin)
            for j in tree2.query(probe):
                j = int(j)
                if j > i:
                    cand_pairs.append((i, j))
    else:
        cand_pairs = [(i, j) for i in range(len(geoms))
                      for j in range(i + 1, len(geoms))]
    for i, j in cand_pairs:
        n1, g1, hw1, l1 = items[i]
        n2, g2, hw2, l2 = items[j]
        if n1 == n2:
            continue          # 同网络不误报
        clear = g1.distance(g2) - hw1 - hw2
        if clear < thr:
            # 严重度分档带 1e-6 容差：clear 恰好等于 thr/2 时归入较轻档。
            # 无容差时中心线法与 buffer 法会在边界浮点上给出不同严重度，
            # 导致同一设计评分不稳定（实测 60 vs 58）。
            sev = 3 if clear < thr / 2 - 1e-6 else 2
            _emit(issues, rules, "R2", "net", f"{l1}|{l2}", sev,
                  f"{l1} 与 {l2}（异网 {n1}/{n2}）间距 {max(clear, 0.0):.2f} mm "
                  f"低于下限 {thr:.2f} mm",
                  "错开走线、缩窄焊盘或改用跳线跨接")


def _rule_r3(r3: dict, state: dict, issues: list, rules: list) -> None:
    """R3 坡度（MRC-003）：面法向与水平面夹角超阈值。

    上限 85° 排除近垂直面（那是侧壁，不是"斜坡流淌"问题）。
    """
    thr = _thr(r3, "max_slope_deg", 45.0)
    for f in state.get("faces", []):
        s = f.get("slope_deg", 0.0)
        if thr < s <= 85.0:
            _emit(issues, rules, "R3", "face", f["fid"], 3 if s > 60 else 2,
                  f"面 {f['fid']} 坡度 {s:.0f}° 超过阈值 {thr:.0f}°"
                  f"（面积 {f.get('area', 0):.0f} mm²），"
                  "浆料在斜面上易流淌、搭接不可达",
                  "放缓坡度至阈值以内，或在坡下加介电台阶")


def _build_blockers(comps: list, obstacles: list) -> list[tuple]:
    """器件与结构凸台 → 统一 5 元组障碍 (key, 多边形, 高度, 名称, 层序号)。

    层序号 None 表示"不是打印层"（器件/结构凸台，永远算遮挡）；
    数字表示它属于哪个物理层，供 R5 判断是否正确挡住走线所在层。
    """
    blockers = [(c["ref"], _comp_rect(c), c["height"], f"器件{c['ref']}", None)
                for c in comps]
    for ob in obstacles:
        rect = box(ob["x0"], ob["y0"], ob["x1"], ob["y1"])
        blockers.append((ob.get("name", "结构"), rect, ob["height"],
                         ob.get("name", "结构凸台"), None))
    return blockers


def _rule_r4_r5(state: dict, tool: dict, routes: list, comps: list,
                obstacles: list, r4, r5, issues: list, rules: list) -> None:
    """R4 可达性 / R5 遮挡（MRC-004/005）。

    R4 的 min_gap 必须真正参与判定：早期版本从不读它，把阈值 0.5 改到 5.0
    分数一点不变（阈值形同虚设）。

    打印层障碍**只参与 R5，不参与 R4**：介电坝贴着走线两侧留出 0.1–0.3 mm
    的槽壁，这是"留槽配合"而非需要避让的凸起。若让 R4 也吃这些障碍，
    正常板件 8 条走线全被报"间隙不足"，分数从 60 掉到 28（实测）。
    """
    gap_thr = _thr(r4, "min_gap", 0.5)
    # 实际要求 = 针尖半径 + 用户阈值与工艺基线中较大者（基线是物理下限）
    reach_r = tool["tip_od_mm"] / 2 + max(gap_thr, tool["safe_gap_mm"])
    blockers = _build_blockers(comps, obstacles)
    printed = _printed_layer_blockers(state, reach_r)
    # R5 用全量（器件 + 结构凸台 + 已打印层）；R4 只用真正高出沉积面的障碍
    r5_blockers = list(blockers) + list(printed)
    r4_blockers = list(blockers)

    # 用 STRtree 做宽相筛选：原先对每条走线遍历全部遮挡物（O(n×m)），
    # 300 走线 × 150 器件实测 1893 ms；索引后只查包围盒邻近的少数对（67 ms，28×）。
    def _build_tree(items):
        geoms = [b[1] for b in items]
        if not geoms:
            return None, items        # 空列表时 _nearby 原样返回
        try:
            from shapely.strtree import STRtree as _ST
        except ImportError:
            return None, items
        return _ST(geoms), items

    r5_tree, r5_items = _build_tree(r5_blockers)
    r4_tree, r4_items = _build_tree(r4_blockers)

    def _nearby(tree, items, probe):
        if tree is not None:
            cand = [int(i) for i in tree.query(probe)]
            return [items[i] for i in sorted(cand)]
        return items

    for rt in routes:
        line = _route_line(rt)
        if line is None:
            continue
        minx, miny, maxx, maxy = line.bounds
        probe = box(minx - reach_r, miny - reach_r, maxx + reach_r, maxy + reach_r)
        # R5 遮挡：器件 + 结构凸台 + 已打印层
        if r5:
            for _key, rect, _h, label, _rank in _nearby(r5_tree, r5_items, probe):
                if line.distance(rect) <= 1e-6:
                    _emit(issues, rules, "R5", "route", rt["id"], 3,
                          f"走线 {rt['net']} 被 {label} 遮挡，路径不可达",
                          f"绕开 {label} 或移动器件")
        # R4 可达性：只针对高出走线沉积面的障碍（器件、结构凸台）
        if r4:
            for _key, rect, _h, label, _rank in _nearby(r4_tree, r4_items, probe):
                d = line.distance(rect)
                if 1e-6 < d < reach_r:
                    _emit(issues, rules, "R4", "route", rt["id"], 2,
                          f"走线 {rt['net']} 与 {label} 的水平间隙 {d:.2f} mm "
                          f"小于要求的最小间隙 {reach_r:.2f} mm"
                          f"（针尖外径 {tool['tip_od_mm']:.2f} 的一半 + "
                          f"间隙 {max(gap_thr, tool['safe_gap_mm']):.2f}）",
                          f"与 {label} 保持 ≥{reach_r:.2f} mm 间距，或调小安全间隙")


def _rule_r6(r6: dict, state: dict, routes: list, issues: list, rules: list) -> None:
    """R6 温度场（MRC-006）：位置估算温差。

    炉门在 x=0 侧；参考点取板框最远端（烘箱内温差取决于板内跨度，
    而非台面原点——早期用台面原点导致 200 mm 板上所有走线都误报）。
    """
    thr = _thr(r6, "max_dt", 5.0)
    grad = _thr(r6, "gradient_c_per_mm", 0.06)
    outline = state.get("outline") or []
    ref_x = max((p[0] for p in outline), default=state.get("bed", [200, 200])[0])
    for rt in routes:
        xs = [p[0] for p in rt["pts"]]
        if not xs:
            continue          # 空点列：R6 依赖坐标，无可算（R1/R2 各自会跳过）
        dt = grad * (ref_x - min(xs))    # 靠门更冷
        if dt > thr:
            _emit(issues, rules, "R6", "route", rt["id"], 1,
                  f"走线 {rt['net']} 靠近炉门侧，估算温差 {dt:.1f} ℃ "
                  f"超过允许 {thr:.0f} ℃",
                  "将关键网络远离炉门侧，或延长保温时间")


def _rule_r7(r7: dict, routes: list, issues: list, rules: list) -> None:
    """R7 介电流道（MRC-007）：坝间通道宽度不足。

    只有"间距 < min_ch + 2*dam_w"（即 mesa < min_ch）才需精确计算，
    因此先用该上限做邻近对筛选，避免全对比较。
    """
    min_ch = _thr(r7, "min_channel", 0.5)
    dam_w = _thr(r7, "dam_w", 0.6)
    limit = min_ch + 2 * dam_w
    # route_geoms 与 valid_routes 一一对应（None 的行被剔除时索引同步收缩）
    # 用"中心线 + 半宽修正"代替 buffer 多边形：
    # 导体间距 = distance(中心线) − 半宽1 − 半宽2，与 buffer 后求距离等价，
    # 但省掉 N 次 buffer 构造。R2 已用这个算法（实测 21× 提速），
    # R7 早期仍保留 buffer（占单次 MRC 的 40%），本轮统一。
    route_geoms = []
    half_w = []
    valid_routes = []
    for r in routes:
        g = _route_line(r)
        if g is not None:
            route_geoms.append(g)
            half_w.append(float(r.get("width", 0.6)) / 2.0)
            valid_routes.append(r)
    for i, j in _near_pairs(route_geoms, limit):
        a, b = valid_routes[i], valid_routes[j]
        if a["net"] == b["net"]:
            continue
        clear = route_geoms[i].distance(route_geoms[j]) - half_w[i] - half_w[j]
        mesa = clear - 2 * dam_w
        if mesa < min_ch:
            _emit(issues, rules, "R7", "trench",
                  f"走线 {a['net']}|走线 {b['net']}", 2,
                  f"走线 {a['net']} 与 {b['net']} 之间的介电坝通道 "
                  f"{mesa:.2f} mm（间距 {clear:.2f} − 两侧坝宽 {2 * dam_w:.1f}）"
                  f"低于下限 {min_ch:.2f} mm",
                  f"拉开异网走线间距至 ≥{min_ch + 2 * dam_w:.2f} mm")


def _safe_list(state: dict, key: str) -> list[dict]:
    """取 state 里的列表字段，容错到"不是列表就返回空"（NFR-002）。

    模糊测试实测：state["routes"] 被外部写成字符串时，规则代码会在
    ``r["width"]`` 处抛 TypeError；整个检查崩掉，界面拿不到任何结果。
    坏数据应当是"该字段视为空"，不是"全盘失败"。
    """
    v = state.get(key)
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]


def _safe_num(v, default: float = 0.0) -> float:
    """把任意输入转成有限浮点数；转不了就用默认值（NFR-002）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _normalize_routes(raw: list[dict]) -> list[dict]:
    """走线字段规范化：width 必须有限，pts 必须是可用的二维点列。

    保留"少于 2 点"的走线（规则里靠 ``_route_line`` 返回 None 跳过），
    但把 width=None 归零——否则 R1 的 ``w < thr`` 直接抛 TypeError。
    """
    out = []
    for r in raw:
        pts = r.get("pts")
        if not isinstance(pts, list):
            continue
        clean_pts = []
        for p in pts:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                x, y = _safe_num(p[0], float("nan")), _safe_num(p[1], float("nan"))
                if x == x and y == y:      # 丢弃 NaN 点
                    clean_pts.append([x, y] + list(p[2:]))
        out.append({**r, "pts": clean_pts,
                    "width": _safe_num(r.get("width"), 0.0),
                    "net": str(r.get("net") or ""),
                    "surface": r.get("surface") or "top",
                    "layer_index": int(_safe_num(r.get("layer_index"), 0))})
    return out


def _normalize_faces(raw: list[dict]) -> list[dict]:
    """面坡度字段规范化：非数值坡度视为 0（不参与 R3 判定）。"""
    out = []
    for f in raw:
        out.append({**f, "fid": str(f.get("fid", "")),
                    "slope_deg": _safe_num(f.get("slope_deg"), 0.0),
                    "area": _safe_num(f.get("area"), 0.0)})
    return out


def _normalize_obstacles(raw: list[dict]) -> list[dict]:
    """障碍物矩形规范化：四个坐标缺一不可，缺了整条丢弃。"""
    out = []
    for ob in raw:
        try:
            box_vals = [_safe_num(ob[k], float("nan"))
                        for k in ("x0", "y0", "x1", "y1")]
        except KeyError:
            continue
        if any(v != v for v in box_vals):      # 有 NaN
            continue
        out.append({**ob, "x0": box_vals[0], "y0": box_vals[1],
                    "x1": box_vals[2], "y1": box_vals[3],
                    "height": _safe_num(ob.get("height"), 0.0),
                    "name": str(ob.get("name") or "结构")})
    return out


def _normalize_pads(raw: list[dict]) -> list[dict]:
    """焊盘规范化：必须有 x/y，缺失的整条丢弃（无法参与任何间距判定）。"""
    out = []
    for p in raw:
        x, y = _safe_num(p.get("x"), float("nan")), _safe_num(p.get("y"), float("nan"))
        if x != x or y != y:
            continue
        out.append({**p, "x": x, "y": y,
                    "ref": str(p.get("ref") or ""),
                    "pad_no": str(p.get("pad_no") or ""),
                    "net": p.get("net")})
    return out


def check(state: dict, rules: list[dict] | None = None, tool: dict | None = None) -> dict:
    """跑全部启用的规则，返回问题列表与分数（纯函数，同一输入同一输出）。

    每条规则是独立函数（见上），这里只做编排：准备上下文 → 按固定顺序
    调用 → 汇总评分。固定顺序保证问题列表顺序稳定（界面与导出可比对）。

    **输入先规范化**：外部数据（旧库、被手工改过的库、导入失败残留）
    可能在字段层面就是坏的。统一在入口做一次规范化，规则函数内部就不用
    到处 try/except——坏字段按"该条不参与检查"处理，整体仍给出可用的分数
    （NFR-002：坏数据不崩溃，且仍有结论）。
    """
    rules = rules or DEFAULT_RULES
    tool = {**DEFAULT_TOOL, **(tool or {})}
    if not isinstance(state, dict):
        state = {}
    issues: list[dict] = []
    # 高度 > 0.3 mm 的器件才构成遮挡（贴片薄件不挡针尖）
    comps = [c for c in _safe_list(state, "comps")
             if _safe_num(c.get("height"), 0.0) > 0.3]
    ctx = {
        "state": state, "rules": rules, "tool": tool, "issues": issues,
        "routes": _normalize_routes(_safe_list(state, "routes")),
        "pads": _normalize_pads(_safe_list(state, "pads")),
        "comps": comps,
        "obstacles": _normalize_obstacles(_safe_list(state, "obstacles")),
        "faces": _normalize_faces(_safe_list(state, "faces")),
    }
    # 规范化后的 faces 供 R3 使用（原 state 里的可能是脏的）
    ctx["state"] = {**state, "faces": ctx["faces"]}
    # R4 与 R5 共用一次遍历（同一批障碍与探测框），因此只在 R4 处执行
    for code in ("R1", "R2", "R3", "R4", "R6", "R7"):
        enabled = next((r for r in rules
                        if r["code"] == code and r["enabled"]), None)
        if not enabled:
            continue
        ctx["r" + code[1]] = enabled
        if code == "R4":
            ctx["r5"] = next((r for r in rules
                              if r["code"] == "R5" and r["enabled"]), None)
            _rule_r4_r5(ctx["state"], ctx["tool"], ctx["routes"], ctx["comps"],
                        ctx["obstacles"], ctx["r4"], ctx["r5"],
                        ctx["issues"], ctx["rules"])
            continue
        _RULES[code](ctx)

    # ---------------- 评分（MRC-008）：满分 100，下限 0
    total = sum(i["penalty"] for i in issues)
    score = max(0, round(100 - total))
    rule_versions = {r["code"]: r["rule_version"] for r in rules}
    return {"issues": issues, "score": score, "penalty_total": round(total, 1),
            "rule_versions": rule_versions}


_RULES = {
    "R1": lambda c: _rule_r1(c["r1"], c["routes"], c["issues"], c["rules"]),
    "R2": lambda c: _rule_r2(c["r2"], c["state"], c["routes"], c["pads"],
                             c["issues"], c["rules"]),
    "R3": lambda c: _rule_r3(c["r3"], c["state"], c["issues"], c["rules"]),
    "R6": lambda c: _rule_r6(c["r6"], c["state"], c["routes"],
                             c["issues"], c["rules"]),
    "R7": lambda c: _rule_r7(c["r7"], c["routes"], c["issues"], c["rules"]),
}



_RULE_NAMES = {
    "R1": "线宽", "R2": "间距", "R3": "坡度", "R4": "可达性",
    "R5": "遮挡", "R6": "温度场", "R7": "介电流道",
}


def _target_display(issue: dict, state: dict) -> str:
    """把 target_id 翻成可读对象名（UI-008：不暴露内部 UUID）。"""
    tid = issue.get("target_id", "")
    ttype = issue.get("target_type", "")
    try:
        if ttype == "route":
            r = next((x for x in state.get("routes", []) if x["id"] == tid), None)
            if r:
                return f"走线 {r['net']}（宽 {r['width']} mm）"
        if ttype == "pad":
            p = next((x for x in state.get("pads", []) if x["id"] == tid), None)
            if p:
                return f"焊盘 {p['ref']}.{p['pad_no']}（网络 {p.get('net')}）"
        if ttype == "face":
            return f"面 {tid}"
    except Exception:
        pass
    return tid[:32]


def explain_issue(issue: dict, state: dict) -> str:
    """点"解释"：给出涉及对象与数值（MRC-009）。"""
    name = _RULE_NAMES.get(issue["rule_code"], "")
    return (f"规则 {issue['rule_code']}（{name}）\n"
            f"对象：{_target_display(issue, state)}\n"
            f"原因：{issue['reason']}\n"
            f"严重度 {issue['severity']}（扣 {issue['penalty']} 分）\n"
            f"建议：{issue['suggestion'] or '无'}")
