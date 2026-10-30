"""三层双材料切片器（SLC-001..007/009..013）。

道次（需求 §3.5 表）：
  0-2 下保护 B 介电 实心铺满 3×0.08
  3   电路   B 介电 打坝留槽 0.10
  4   电路   A 银浆 填槽     0.10（两次填则拆 4a/4b）
  5   上保护 B 介电 铺满留焊盘坑 0.10
  6   上保护 A 银浆 填焊盘坑 0.10
纯函数：同一 (state, params, materials) → 同一路径与 G-code。
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..circuit.connectivity import check_connectivity
from ..geometry.primitives import pad_polygon as _pad_polygon
from ..geometry.primitives import route_polygon as _route_polygon

# 路径生成原语（光栅填充/串接/简化）已拆到 slicing/raster.py；
# re-export 保持 `from .slicer import raster_fill` 等旧调用方可用。
from .raster import (SPACING_K, SIMPLIFY_TOL, chain_paths,  # noqa: F401
                     fill_region, fill_spacing, raster_fill, simplify_pts)

# 需求 §1.7 总厚目标区间。
# v1.2 已裁决（§10.5）：采纳 0.44 mm（3×0.08 + 0.10 + 0.10 的完整推导），
# 原目标 0.35–0.40 无推导依据，改为 0.40–0.45（覆盖 0.44 并留 ±0.01 工艺余量）。
# 超区间时提示但不阻断（SLC-001/SLC-012）。
TOTAL_THICK_TARGET = (0.40, 0.45)

DAM_W = 0.6        # 介电坝宽（工艺基线）
FILL_W = 0.6       # 铺满线宽
# 焊盘坑设计：坑的**内壁开口**应 ≥ 铜焊盘 + 2×重叠，否则银层小于铜焊盘会虚焊。
# 坑轮廓是一条中心线（浆线宽 FILL_W），所以中心线要再外扩 FILL_W/2，
# 才能保证浆料内沿落在"焊盘 + 重叠"处。
PAD_PIT_OVERLAP = 0.15          # 银层超出焊盘单边的重叠量
PAD_PIT_CENTER_INFLATE = (2 * PAD_PIT_OVERLAP) + FILL_W   # 中心线需外扩的总量
SILVER_MAX_SINGLE = 0.8   # 槽宽 ≤ 此值时银浆走中心线，否则光栅




# 路径点简化的默认容差（mm）。buffer 生成的圆弧会有几十个近乎共线的点，
# 对打印没有意义，却让每条路径的 JSON 达到数 KB。0.02 mm 远低于机器定位精度。
# 填充线距修正：圆角矩形挤出的实际线距 = 线宽 - 层厚×(1-π/4)。
# 用线宽当线距会过挤（相邻线重叠），标杆切片器（CuraEngine Flow.cpp）都用这个公式。












def _route_poly(route) -> Polygon:
    return _route_polygon(route["pts"], route["width"])


def _pad_poly(pad, extra=0.0) -> Polygon:
    """焊盘几何（委托统一的 primitives；现在支持旋转与圆形）。"""
    return _pad_polygon(pad, inflate=extra)


def _route_content_key(r: dict) -> tuple:
    """走线的确定性排序键：只含几何与网络名，不含主键。

    同一份设计重新构建后 uuid 会变，若按 id 排序则 G-code 段序随主键漂移。
    取 (网名, 线宽, 点列) 可保证内容相同则顺序相同。
    """
    pts = r.get("pts") or []
    pts_key = tuple((round(float(p[0]), 3), round(float(p[1]), 3)) for p in pts
                    if len(p) >= 2)
    return (str(r.get("net", "")), round(float(r.get("width", 0) or 0), 3), pts_key)


def _bottom_passes(board, params: dict, P, mk) -> list[dict]:
    """下保护层道次（SLC-001/002）：道次数可配置（典型 3），层间填充 45°/135° 交替。

    为什么必须交替：三个下保护道次若都用 45°，相邻两层的填充丝走向完全
    平行，层界面成为"弱面"——受剪切时沿界面剥离。交替 90° 后上层丝跨越
    下层丝，界面剪切强度显著提高（这是 FDM/SLS 的通用结论，也是
    CuraEngine/PrusaSlicer 的默认行为）。
    每层的路径长度完全一致（实测 170 行 / 7721 mm），所以这不增加任何
    打印时间或材料，纯粹是方向选择。
    """
    bottom_h = params.get("bottom_layer_mm", 0.08)
    # 道次数越界时钳到合理范围
    n_bottom = max(1, min(10, int(params.get("bottom_passes", 3) or 3)))
    z_acc = 0.0
    passes = []
    for i in range(n_bottom):
        z_acc += bottom_h
        # 填充内缩：中心线内缩半线距，避免浆料溢出板框（外沿刚好落在边界）
        fill_area = fill_region(board, FILL_W, bottom_h)
        edge = board.buffer(-FILL_W / 2, join_style=2, mitre_limit=1.2)
        outline_ring = edge.exterior.coords if not edge.is_empty else None
        paths = [P("outline", list(outline_ring) if outline_ring is not None
                   else [], FILL_W)]
        fill_angle = 45 if (i % 2 == 0) else 135
        paths += [P("fill", row, FILL_W)
                  for row in raster_fill(fill_area, fill_spacing(FILL_W, bottom_h),
                                         angle_deg=fill_angle)]
        passes.append(mk(i, "bottom", "B", z_acc, bottom_h,
                         chain_paths(paths)))
    return passes


def _dam_pass(routes: list, outline: list, z_circuit: float, lh: float,
              P, mk) -> dict:
    """电路层介电坝（道次 3）：沿走线两侧 + 板框坝。

    内外环都要画：闭合环走线（天线线圈/地环/屏蔽框）的中心线 buffer 后
    会有一个内环，只取 .exterior 会静默丢掉内侧介电坝（实测缺 73.6 mm），
    导致银浆直接漏到下层。
    """
    dam_paths = [P("outline", list(outline) + [outline[0]], DAM_W)]
    for r in routes:
        dam = _route_poly(r).buffer(DAM_W / 2)
        if not dam.is_empty:
            dam_paths.append(P("dam", simplify_pts(list(dam.exterior.coords)), DAM_W))
            for inner in dam.interiors:
                dam_paths.append(P("dam", simplify_pts(list(inner.coords)), DAM_W))
    return mk(3, "circuit", "B", z_circuit, lh, dam_paths)


def _silver_fill(routes: list, P) -> list[dict]:
    """电路层银浆填槽路径：细线直写，宽槽（> 0.9 mm）转光栅填充。"""
    out = []
    for r in routes:
        if r["width"] <= SILVER_MAX_SINGLE:
            out.append(P("trench", r["pts"], r["width"]))
        else:
            poly = _route_poly(r)
            out += [P("trench", row, min(0.6, r["width"]))
                    for row in raster_fill(poly, 0.4)]
    return out


def _top_passes(board, pads: list, outline: list, lh: float, z_top: float,
                pass_top: int, P, mk) -> list[dict]:
    """上保护层两个道次：介电铺满留坑 + 银浆填坑。

    坑轮廓画在"浆料范围"外再偏移半线宽处，使浆料内沿正好压在焊盘边缘
    （坑开口比铜焊盘大 20%/30%，避免润湿不良虚焊）。
    """
    pits = unary_union([_pad_poly(p, extra=0.3) for p in pads if p.get("net")]) if pads else Polygon()
    cover_region = board.difference(pits) if not pits.is_empty else board
    cover_area = fill_region(cover_region, FILL_W, lh)
    cover_edge = board.buffer(-FILL_W / 2, join_style=2, mitre_limit=1.2)
    cover_ring = cover_edge.exterior.coords if not cover_edge.is_empty else outline
    cover_paths = [P("outline", list(cover_ring), FILL_W)]
    cover_paths += [P("fill", row, FILL_W)
                    for row in raster_fill(cover_area, fill_spacing(FILL_W, lh))]
    if not pits.is_empty:
        rings = pits.geoms if pits.geom_type == "MultiPolygon" else [pits]
        for g in rings:
            ring = g.buffer(FILL_W / 2, join_style=2, mitre_limit=1.5)
            if not ring.is_empty:
                cover_paths.append(P("pad_pit", list(ring.exterior.coords), FILL_W))
                for inner in ring.interiors:
                    cover_paths.append(P("pad_pit", list(inner.coords), FILL_W))
    top = [mk(pass_top, "top", "B", z_top, lh, chain_paths(cover_paths))]

    pit_fill = []
    for p in pads:
        if not p.get("net"):
            continue
        poly = _pad_poly(p, extra=2 * PAD_PIT_OVERLAP)
        pit_fill.append(P("pad_fill", list(poly.exterior.coords), FILL_W))
        pit_fill += [P("pad_fill", row, FILL_W)
                     for row in raster_fill(poly, fill_spacing(FILL_W, lh))]
    top.append(mk(pass_top + 1, "top", "A", z_top, lh, chain_paths(pit_fill)))
    return top


def _connectivity_view(routes: list, pads: list) -> tuple[list, list]:
    """切片后连通性检查的视图（SLC-007）。

    字段用 .get 兜底：缺 id 的焊盘照样参与几何判定，崩溃在这里没有任何
    收益——坏数据的正确语义是"尽量检出问题"。无坐标的焊盘无法参与
    几何判定，直接剔除（而不是让 KeyError 打断整个切片）。
    """
    sliced_routes = [{"id": f"SL-{str(r.get('id', ''))[:6]}",
                      "net": r.get("net", ""), "pts": r["pts"],
                      "width": r.get("width", 0.6)} for r in routes]
    valid_pads = [p for p in pads
                  if isinstance(p, dict) and "x" in p and "y" in p]
    sliced_pads = [{"id": str(p.get("id") or f"P{i}"),
                    "ref": str(p.get("ref") or ""),
                    "pad_no": str(p.get("pad_no") or i),
                    "net": p.get("net"), "x": p["x"], "y": p["y"],
                    "w": p.get("w", 1.5), "h": p.get("h", 1.0),
                    "shape": p.get("shape", "rect"), "rot": 0}
                   for i, p in enumerate(valid_pads)]
    return sliced_routes, sliced_pads


def build_slices(state: dict, params: dict) -> dict:
    """生成道次与路径。params 需含 layer_height_mm/…/cure_*（SLC-003）。

    入口先做契约校验：缺板框或缺关键参数时抛**可读的** ValueError，
    而不是让 ``Polygon(None)`` 之类在深层抛 KeyError/TypeError——
    后者会一路变成界面上看不懂的堆栈（UI-005：失败要有原因）。
    """
    outline = (state or {}).get("outline")
    if not isinstance(outline, list) or len(outline) < 3:
        raise ValueError(
            "切片缺少有效板框（至少 3 个顶点）。请先在结构域导入 STEP "
            "或手工设置板框（STR-007：无板框不能进入切片）")
    missing = [k for k in ("layer_height_mm", "speed_mm_s") if k not in (params or {})]
    if missing:
        raise ValueError(f"切片参数缺少必需项：{', '.join(missing)}（SLC-012）")
    # 板框多边形：扣除内孔（螺丝孔等），避免孔洞区域被实心填充（SLC-004）
    holes = [h for h in (state.get("holes") or []) if len(h) >= 3]
    try:
        board = Polygon(outline, holes) if holes else Polygon(outline)
        if not board.is_valid:
            board = board.buffer(0)
    except Exception:
        board = Polygon(outline)
    routes_all = state.get("routes") or []
    if not isinstance(routes_all, list):
        routes_all = []
    # 畸形走线（少于 2 点）不参与切片并记警告（NFR-002：坏数据不崩溃、有提示）
    routes = [r for r in routes_all if isinstance(r, dict) and len(r.get("pts", [])) >= 2]
    # 按**内容**排序，而不是按 id 排序。
    #
    # state["routes"] 来自 ``ORDER BY id``，而 id 是新建对象时生成的 uuid。
    # 同一份设计内容重新建一遍（Agent 重放、重新导入同一板文件）主键必然不同，
    # 路径顺序跟着变，G-code 哈希就对不上——实测内容逐字节相同、仅段顺序不同，
    # 表现为 design_match=True 但 gcode_match=False。
    # 排序键只取几何与网络名，保证"同一设计内容 → 同一 G-code"（SLC-008）。
    routes.sort(key=_route_content_key)
    skipped = len(routes_all) - len(routes)
    pads = state.get("pads", [])
    lh = params["layer_height_mm"]
    passes: list[dict] = []

    def mk(pass_no, kind, mat_role, z, h, paths):
        return {"pass_no": pass_no, "kind": kind, "mat": mat_role, "z_mm": round(z, 4),
                "h_mm": round(h, 4), "paths": paths}

    seq = [0]

    def P(role, pts, width=FILL_W, speed=None):
        seq[0] += 1
        return {"seq": seq[0], "role": role, "width_mm": width,
                "speed_mm_s": speed or params["speed_mm_s"],
                "pts": [[round(p[0], 3), round(p[1], 3)] for p in pts]}

    z_bottom_acc = 0.0
    passes += _bottom_passes(board, params, P, mk)
    z_bottom_acc = passes[-1]["z_mm"]

    # 道次 3：电路层 介电坝（沿走线两侧 + 板框坝）
    z_circuit = z_bottom_acc + lh
    passes.append(_dam_pass(routes, outline, z_circuit, lh, P, mk))

    # 道次 4：电路层 银浆填槽（两次填则拆分）
    if params.get("silver_two_fills"):
        passes.append(mk(4, "circuit", "A", z_circuit, lh / 2,
                         _silver_fill(routes, P)))
        passes.append(mk(5, "circuit", "A", z_circuit, lh / 2,
                         _silver_fill(routes, P)))
        pass_top = 6
    else:
        passes.append(mk(4, "circuit", "A", z_circuit, lh,
                         _silver_fill(routes, P)))
        pass_top = 5

    # 道次 5/6：上保护铺满留坑 + 银浆填坑
    z_top = z_circuit + lh
    passes += _top_passes(board, pads, outline, lh, z_top, pass_top, P, mk)

    # 切片后连通性（SLC-007）：导体 = 槽填 + 焊盘坑填。
    # 检查用视图由 _connectivity_view 构造（坏数据兜底语义见该函数 docstring）。
    sliced_routes, sliced_pads = _connectivity_view(routes, pads)
    nets = sorted({p.get("net") for p in sliced_pads if p.get("net")}
                  | {r.get("net") for r in sliced_routes if r.get("net")})
    conn = check_connectivity(nets, sliced_pads, sliced_routes,
                              [{"id": j["id"], "net": j["net"],
                                "from_pad": j["from_pad"], "to_pad": j["to_pad"]}
                               for j in state.get("jumpers", [])])

    # 总厚 = 各物理层之和（银浆填槽/填坑与同层介电共面，不额外增加厚度）
    total_thick = round(z_bottom_acc + lh + lh, 4)
    thick_note = check_total_thickness(total_thick)

    return {"passes": passes, "layer_count": 3, "connectivity": conn,
            "z_bottom": round(z_bottom_acc, 4), "z_circuit": round(z_circuit, 4),
            "z_top": round(z_top, 4), "params_echo": params,
            "total_thickness_mm": total_thick,
            "thickness_note": thick_note,
            "skipped_degenerate_routes": skipped}


def check_total_thickness(total: float,
                          target: tuple[float, float] = TOTAL_THICK_TARGET) -> str:
    """总厚与目标区间偏差说明（SLC-001「总厚与目标不一致时提示」）。

    返回空串表示一致，否则返回可读提示。区间取闭区间，容差 0.005 mm
    吸收浮点误差（0.44 由 3×0.08+0.10+0.10 相加而来）。
    """
    lo, hi = target
    if lo - 0.005 <= total <= hi + 0.005:
        return ""
    return (f"总厚 {total:.3f} mm 超出目标 {lo:.2f}–{hi:.2f} mm"
            f"（偏差 {total - hi:+.3f} mm）。层厚相加为 {total:.2f} mm，"
            "与目标区间不一致，请确认各层厚度或工艺口径（需求 §9 待确认 #1）")


def build_schedule(params: dict, slices: dict) -> list[dict]:
    """固化排程（SLC-005）：先介电→预凝胶→银浆→定型→层固化；终固化、冷却。
    cure_mode: layer=按物理层固化（默认，待确认#2）；pass=按道次固化。

    每个相位带 kind（bottom/circuit/top/board）与 pass_no（该物理层最后一个道次），
    供 G-code 按层交错输出与故障按层注入。
    """
    schedule: list[dict] = []
    t = 0.0

    def add(phase, temp, dur, note, kind, pass_no):
        nonlocal t
        schedule.append({"t_start_s": round(t, 1), "phase": phase,
                         "temp_c": temp, "duration_s": round(dur, 1), "note": note,
                         "kind": kind, "pass_no": pass_no})
        t += dur

    kinds = [("下保护层", "bottom"), ("电路层", "circuit"), ("上保护层", "top")]
    by_kind: dict[str, list[dict]] = {"bottom": [], "circuit": [], "top": []}
    for p in slices["passes"]:
        by_kind[p["kind"]].append(p)
    for label, kind in kinds:
        ps = by_kind[kind]
        if not ps:
            continue
        last_pass = max(p["pass_no"] for p in ps)
        b = [p for p in ps if p["mat"] == "B"]
        a = [p for p in ps if p["mat"] == "A"]
        if b:
            add(f"打印·{label}·介电", 25, 10, f"道次 {'/'.join(str(p['pass_no']) for p in b)}",
                kind, last_pass)
            add("预凝胶", params["pregel_temp_c"], 90, "80℃ 1–2 min，表层失流",
                kind, last_pass)
        if a:
            add(f"打印·{label}·银浆", params["pregel_temp_c"], 10,
                f"道次 {'/'.join(str(p['pass_no']) for p in a)}", kind, last_pass)
            add("银浆定型", params["set_temp_c"], 90, "100℃ 1–2 min", kind, last_pass)
        ramp = ((params["cure_temp_c"] - params["pregel_temp_c"])
                / max(float(params["ramp_c_min"] or 0), 1e-6) * 60)
        add("升温", params["cure_temp_c"], ramp,
            f"升温速率 ≤{params['ramp_c_min']}℃/min", kind, last_pass)
        add("层固化", params["cure_temp_c"], params["cure_min"] * 60,
            "170℃ 6–8 min（按物理层）" if params["cure_mode"] == "layer" else "按道次固化",
            kind, last_pass)
    last = max((p["pass_no"] for p in slices["passes"]), default=0)
    add("整板终固化", params["cure_temp_c"], params["final_cure_min"] * 60,
        "170℃ 15 min", "board", last)
    add("冷却", params["cool_to_c"],
        max(params["cure_temp_c"] - params["cool_to_c"], 0)
        / max(float(params["ramp_c_min"] or 0), 1e-6) * 60,
        "降至 60℃ 以下取板", "board", last)
    schedule.append({"t_start_s": round(t, 1), "phase": "完成", "temp_c": params["cool_to_c"],
                     "duration_s": 0, "note": "", "kind": "board", "pass_no": last})
    return schedule


def estimate(slices: dict, params: dict, mat_a: dict, mat_b: dict,
             travel_speed: float = 60.0) -> dict:
    """路径长度 / 时间 / 体积 / 成本估算（SLC-009，±30%，区分首次升温与稳态）。"""
    lengths = {"A": 0.0, "B": 0.0}
    # 体积必须按**每条路径自己的线宽**累加，不能用统一的 FILL_W：
    # 实测道次 4（电路层银浆）有 5 条 0.3 mm 路径与 3 条 0.6 mm 路径混排，
    # 按固定 0.6 算会把细线的体积高估一倍（0.3 → 0.6 是 ×2）。
    # 早期就是按 FILL_W 统一算的，体积/成本估算偏差随细线比例放大。
    vol_a = vol_b = 0.0
    print_time = 0.0
    lh = params["layer_height_mm"]
    flow = params.get("flow_factor", 1.0)
    for p in slices["passes"]:
        for path in p["paths"]:
            pts = path["pts"]
            ln = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            lengths[p["mat"]] += ln
            spd = path["speed_mm_s"] if path["role"] != "travel" else travel_speed
            print_time += ln / max(float(spd or 0), 1e-6)
            # 挤出截面 = 线宽 × 层厚（矩形近似，与 G-code 的 E 值口径一致）
            vol = ln * float(path.get("width_mm", FILL_W)) * lh * flow
            if p["mat"] == "A":
                vol_a += vol
            else:
                vol_b += vol
    sched = build_schedule(params, slices)
    cure_time = sum(s["duration_s"] for s in sched[:-1])
    first_ramp = sum(s["duration_s"] for s in sched if s["phase"] == "升温")
    steady = print_time + cure_time - first_ramp
    cost = vol_a / 1000 * mat_a.get("price_cny_ml", 0) + vol_b / 1000 * mat_b.get("price_cny_ml", 0)
    return {
        "length_mm": {k: round(v, 1) for k, v in lengths.items()},
        "volume_ml": {"A": round(vol_a / 1000, 4), "B": round(vol_b / 1000, 4)},
        "time_s": {"print": round(print_time, 1), "cure": round(cure_time, 1),
                   "first_ramp": round(first_ramp, 1), "steady_total": round(steady, 1)},
        "cost_cny": round(cost, 2),
        "assumptions": "估算按路径长度×线宽×层厚的理想体积，未计针头启停损耗；"
                       "时间含空移 60mm/s；与虚拟打印偏差目标 ±30%（不含首次升温）",
    }
