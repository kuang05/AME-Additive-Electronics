"""智能体（AGT-001..011）：设计/审查/裁决 + 计划 + 对比卡片 + 三级授权 + 重放。

无模型降级（AGT-010）：全部为本地确定性规则流程，断网可用；
云端仅用于解释/排序（AGT-012），本版不依赖。
"""
from __future__ import annotations

import json
import time

from .. import TOOL_VERSIONS, new_id
from ..db.store import now_iso

AUTH_LEVELS = ("suggest", "step_confirm", "auto")   # AGT-006，默认 step_confirm

# AI 排序来源的中文名（写入审计与界面，避免暴露内部代号）
_SRC_CN = {"cloud": "云端模型", "local": "本地模型", "heuristic": "规则先验"}


# ---------------------------------------------------------------- 计划
def make_plan(ws, goal: str, steps: list[dict], role="designer") -> dict:
    plan = {"id": new_id(), "project_id": ws.project_id, "goal": goal,
            "steps": [{"seq": i + 1, **s} for i, s in enumerate(steps)],
            "tool_versions": dict(TOOL_VERSIONS), "status": "draft",
            "agent_role": role, "created_at": now_iso()}
    ws.store.execute("INSERT INTO plan(id,project_id,goal,steps_json,tool_versions,"
                     "status,agent_role,created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (plan["id"], plan["project_id"], plan["goal"],
                      json.dumps(plan["steps"], ensure_ascii=False),
                      json.dumps(plan["tool_versions"]), "draft", role, now_iso()))
    return plan


# ---------------------------------------------------------------- 步骤执行（AGT-009：只走命令总线）
def apply_step(ws, step: dict) -> dict:
    a, args = step["action"], step.get("args", {})
    if a == "change_width":
        return ws.cmd_change_width(args["route_id"], args["width"])
    if a == "delete_route":
        return ws.cmd_delete_route(args["route_id"])
    if a == "add_route":
        return ws.cmd_add_route(args["net"], args["pts"], args.get("width", 0.6),
                                args.get("surface", "top"))
    if a == "add_jumper":
        return ws.cmd_add_jumper(args["net"], args["from_pad"], args["to_pad"],
                                 args.get("reason", ""))
    if a == "move_comp":
        return ws.cmd_move_comp(args["comp_id"], args["x"], args["y"])
    raise ValueError(f"未知动作 {a}（Agent 只能做界面上能做的事）")


# ---------------------------------------------------------------- 设计 Agent（AGT-001）
def _legal_width(w) -> float:
    """绕行/重布时用的线宽：不低于工艺下限。

    为什么必须这样：`cmd_add_route` 会按 CIR-010 校验线宽，
    低于 0.5 mm 直接拒绝。早期绕行时原样传原线宽，导致"细线绕行"
    这类修复必然失败——R4/R5 的样本因此恒为 0（实测：
    "线宽 0.30 mm 低于工艺下限" 让所有 R4 建议在采集阶段被丢弃）。

    R1（线宽不足）与 R4/R5（遮挡/间隙）是**两个独立问题**：
    绕行不该因为原线太细而失败，它就该顺手把线加宽到合规。
    """
    from ..circuit.connectivity import PROCESS_MIN_WIDTH
    try:
        return max(float(w), PROCESS_MIN_WIDTH)
    except (TypeError, ValueError):
        return PROCESS_MIN_WIDTH


def _detour_is_improvement(r: dict, new_pts: list, code: str,
                           state: dict) -> bool:
    """绕行方案是否真的能消除该违规、且不制造更差的问题。

    为什么必须校验：实测"扩张矩形角点绕行"在凸台场景下会把走线绕得更远，
    与其它走线产生新的 R2/R7 违规，而 R4/R5 本身没消除（凸台还在原处，
    绕行路径仍穿过它的投影区）——分数从 50 掉到 42。

    这正是项目一贯的原则：**宁可放弃一条建议，也不制造新问题**
    （同 `_best_shift` 的处理）。校验用精确几何，不用启发式。

    判据（全部满足才采纳）：
    1. 新路径与障碍物的最小距离 > 原路径的（真的离远了）；
    2. 新路径不进入任何异网走线的线宽范围（不制造 R2/R7）。
    """
    from ..geometry.primitives import route_line

    old_line = route_line(r.get("pts") or [])
    new_line = route_line(new_pts)
    if old_line is None or new_line is None:
        return False

    # 1) 障碍物：R4/R5 的 target 是走线，障碍在 state["obstacles"] 里
    obstacles = state.get("obstacles") or []
    if obstacles:
        def min_clear(line):
            best = float("inf")
            for ob in obstacles:
                from shapely.geometry import box as _box
                d = line.distance(_box(ob["x0"], ob["y0"], ob["x1"], ob["y1"]))
                best = min(best, d)
            return best
        # 新路径必须比原路径离障碍更远（否则等于没绕）
        if min_clear(new_line) <= min_clear(old_line) + 1e-6:
            return False

    # 2) 不制造新的 R2/R7：与异网走线的净距不能变小
    half_new = max(float(r.get("width", 0.6)), 0.5) / 2.0
    half_old = float(r.get("width", 0.6)) / 2.0
    for other in state.get("routes") or []:
        if other.get("id") == r.get("id") or other.get("net") == r.get("net"):
            continue
        oline = route_line(other.get("pts") or [])
        if oline is None:
            continue
        half_o = float(other.get("width", 0.6)) / 2.0
        clear_old = old_line.distance(oline) - half_old - half_o
        clear_new = new_line.distance(oline) - half_new - half_o
        if clear_new < clear_old - 1e-6:
            return False      # 更挤了，不要
    return True


def _detour(pts, obstacle, margin=1.2):
    """两点走线绕开障碍矩形：从扩张矩形角点选两条边绕行。"""
    (x0, y0), (x1, y1) = pts[0][:2], pts[-1][:2]
    ox0, oy0 = obstacle["x0"] - margin, obstacle["y0"] - margin
    ox1, oy1 = obstacle["x1"] + margin, obstacle["y1"] + margin
    corners = [(ox0, oy0), (ox1, oy0), (ox1, oy1), (ox0, oy1)]
    c = ((x0 + x1) / 2, (y0 + y1) / 2)
    near = sorted(corners, key=lambda p: (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2)
    mid1 = near[0]
    if mid1[0] == ox0 or mid1[0] == ox1:
        mid2 = (mid1[0], oy1 if mid1[1] == oy0 else oy0)
    else:
        mid2 = (ox1 if mid1[0] == ox0 else ox0, mid1[1])
    return [[x0, y0, 0.0], [mid1[0], mid1[1], 0.0],
            [mid2[0], mid2[1], 0.0], [x1, y1, 0.0]]


def _detour_polyline(pts, obstacle, margin: float = 1.2):
    """多点走线绕开障碍矩形：只改动**落在障碍内或过近**的那一段。

    早期 R4/R5 分支要求 `len(pts) == 2`，导致样例板上 4–7 点的走线
    一条建议也生成不了（R4/R5 在训练样本里恒为 0）。多点走线的正确做法
    不是"整条重画"（会丢掉原有拐点、引入新违规），而是：
    找出穿越障碍的相邻两点，用绕行角点替换这一段，其余点原样保留。
    """
    if len(pts) < 2:
        return None
    ox0, oy0 = obstacle["x0"] - margin, obstacle["y0"] - margin
    ox1, oy1 = obstacle["x1"] + margin, obstacle["y1"] + margin

    def inside(p) -> bool:
        return ox0 <= p[0] <= ox1 and oy0 <= p[1] <= oy1

    # 找第一段（相邻两点，至少一端在障碍内）需要绕行的位置
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if not (inside(a) or inside(b)):
            continue
        # 用"离两点中点最近的角点开始，沿障碍边界走两段"绕开
        cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        corners = [(ox0, oy0), (ox1, oy0), (ox1, oy1), (ox0, oy1)]
        near = sorted(corners,
                      key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        mid1 = near[0]
        # 第二个角点：与 mid1 共享一条障碍边，且离另一点更近
        if mid1[0] in (ox0, ox1):
            mid2 = (mid1[0], oy1 if mid1[1] == oy0 else oy0)
        else:
            mid2 = (ox1 if mid1[0] == ox0 else ox0, mid1[1])
        detour = [[a[0], a[1], 0.0],
                  [mid1[0], mid1[1], 0.0],
                  [mid2[0], mid2[1], 0.0],
                  [b[0], b[1], 0.0]]
        return pts[:i] + detour + pts[i + 2:]
    return None


def _resolve_route_ref(ref: str, routes: dict, by_prefix: dict,
                       by_net: dict) -> str | None:
    """把问题里的走线引用解析成完整 id。

    R2/R7 的 target_id 有两种形态，早期只处理了第一种，导致样例板上
    R2/R7 全部无法生成修复步骤（症状：问题面板有 4 条建议，Agent 一条也不动）：
      1. 完整/短 id：``"<uuid>"`` 或 ``"<uuid8>|..."``；
      2. **人类可读标签**：``"走线 N6|焊盘C3.3"``（引擎为了可读性直接写标签）。
    第二种必须按网名回查，否则永远匹配不到。
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref in routes:
        return ref
    head = ref.split("|")[0].strip()
    if head in routes:
        return head
    if head[:8] in by_prefix:
        return by_prefix[head[:8]]
    # 标签形态："走线 <网名>" / 裸网名
    name = head[len("走线"):].strip() if head.startswith("走线") else head
    if name in by_net:
        return by_net[name]
    return None


def _shift_polyline(pts, off: float):
    """错开走线但**保持两端锚定**（多点与两点都支持）。

    整条平移会把端点从焊盘上挪开，直接造成断路（实测：分数虽从 60→63，
    但连通性检查报 1 处断路——这种"分数变好却不可制造"的修正是错误的）。
    端点必须钉在原地，只让中间段侧移：

    - 3 点及以上：保留首末点，中间顶点沿整条线的主法向平移；
    - 2 点（直连两焊盘）：在中间插入一个侧移顶点，形成绕行折角。
    """
    if len(pts) < 2:
        return None
    (x0, y0), (x1, y1) = pts[0][:2], pts[-1][:2]
    dx, dy = x1 - x0, y1 - y0
    ln = (dx * dx + dy * dy) ** 0.5
    if ln < 1e-9:
        return None
    nx, ny = -dy / ln * off, dx / ln * off
    if len(pts) == 2:
        mx, my = (x0 + x1) / 2 + nx, (y0 + y1) / 2 + ny
        return [[float(x0), float(y0), 0.0], [mx, my, 0.0],
                [float(x1), float(y1), 0.0]]
    out = [[float(pts[0][0]), float(pts[0][1]), 0.0]]
    for p in pts[1:-1]:
        out.append([float(p[0]) + nx, float(p[1]) + ny, 0.0])
    out.append([float(pts[-1][0]), float(pts[-1][1]), 0.0])
    return out


def _min_clearance(line, others) -> float:
    """候选走线到其它异网几何的最小间距；None 几何跳过。"""
    best = float("inf")
    for g in others:
        if g is None or g.is_empty:
            continue
        d = line.distance(g)
        if d < best:
            best = d
    return best


def _best_shift(r: dict, partner: str, state: dict):
    """为 R2/R7 选一个**就近但更宽敞**的平移。

    早期实现固定向同一侧平移 1.5 mm，实测会把走线推进另一组走线里，
    分数反而从 60 掉到 0。这里改为：两个方向各试几档距离，只保留
    "留在板框内且与其它异网几何的最小间距最大"的那个；一个都不安全时
    返回 None，让 Agent 放弃这条建议而不是制造新问题（CIR-012）。
    """
    pts = r.get("pts") or []
    if len(pts) < 2:
        return None
    outline = state.get("outline") or []
    from ..geometry.primitives import board_polygon, pad_polygon, route_line

    board = board_polygon(outline)
    if board is None:
        return None
    half = float(r.get("width", 0.6)) / 2.0
    # 障碍集合：异网走线 + 全部焊盘（同网不参与，同网靠得近是允许的）
    obstacles = []
    for other in state.get("routes", []):
        if other.get("id") == r.get("id") or other.get("net") == r.get("net"):
            continue
        obstacles.append(route_line(other.get("pts") or []))
    for p in state.get("pads", []):
        if p.get("net") == r.get("net"):
            continue
        obstacles.append(pad_polygon(p))

    best_pts, best_clear = None, -1.0
    for off in (1.5, -1.5, 2.5, -2.5, 3.5, -3.5):
        cand = _shift_polyline(pts, off)
        if not cand:
            continue
        line = route_line(cand)
        if line is None:
            continue
        # 必须整条留在板框内（留出半线宽的余量）
        if not board.buffer(-half - 1e-6).contains(line):
            continue
        clear = _min_clearance(line, obstacles)
        if clear > best_clear:
            best_pts, best_clear = cand, clear
    return best_pts


def _signature(step: dict) -> str:
    """步骤的稳定指纹（用于记住"这个方案已经试过且被否决"）。"""
    a = step.get("action", "")
    args = step.get("args", {}) or {}
    key = args.get("route_id") or args.get("net") or args.get("comp_id") or ""
    return f"{a}:{key}"


def designer_propose(ws, mrc: dict, state: dict,
                     rejected: set[str] | None = None) -> list[dict]:
    """按问题清单生成可执行修改步骤（本地规则，AGT-010）。

    ``rejected`` 是本轮之前**已被裁决回退**的方案指纹集合：状态没变时
    同样的建议会原样重现，不过滤就会无限重试同一个坏方案。
    """
    rejected = rejected or set()
    steps: list[dict] = []
    routes = {r["id"]: r for r in state["routes"]}
    # R2/R7 的 target_id 是“短 id|短 id”，需按前缀回查完整 id
    by_prefix: dict[str, str] = {}
    by_net: dict[str, str] = {}
    for rid, r in routes.items():
        by_prefix.setdefault(rid[:8], rid)
        # 同网多条时保留第一条：批量平移同网所有走线会互相打架
        by_net.setdefault(r["net"], rid)
    seen_routes: set[str] = set()
    # 去重键是 (规则, 走线) 而不是单纯的走线：
    # R4（间隙不足）与 R5（被遮挡）可能指向同一条走线，但它们要改的几何
    # 方向不同。早期按走线去重会让先出现的那条规则吃掉后者，导致 R4 或
    # R5 的样本恒为 0（实测 R4 就是这样拿不到训练数据）。
    seen_rule_route: set[tuple[str, str]] = set()
    for issue in mrc["issues"]:
        code = issue["rule_code"]
        tid = issue["target_id"]
        if code == "R1" and tid in routes and tid not in seen_routes:
            r = routes[tid]
            steps.append({"action": "change_width",
                          "args": {"route_id": tid, "width": 0.6},
                          "precondition": f"{tid} 存在且未锁定",
                          "expected": f"线宽 {r['width']:.2f}→0.60 mm，R1 消除",
                          "reason": issue["reason"]})
            seen_routes.add(tid)
        elif code in ("R4", "R5") and tid in routes and (code, tid) not in seen_rule_route:
            r = routes[tid]
            ob = next((o for o in state["obstacles"]
                       if o["name"].split("F")[-1] in issue["reason"] or issue["target_id"]), None)
            ob = ob or (state["obstacles"][0] if state["obstacles"] else None)
            if ob:
                # 两点用边框绕行，多点用"只改穿越段"的绕行
                new_pts = (_detour(r["pts"], ob) if len(r["pts"]) == 2
                           else _detour_polyline(r["pts"], ob))
            # 只采纳"真的能改善"的绕行，否则放弃（不制造新问题）
            if ob and new_pts and _detour_is_improvement(r, new_pts, code, state):
                steps.append({"action": "delete_route", "args": {"route_id": tid},
                              "precondition": f"{tid} 存在",
                              "expected": "移除被遮挡走线", "reason": issue["reason"]})
                steps.append({"action": "add_route",
                              "args": {"net": r["net"], "pts": new_pts,
                                       "width": _legal_width(r["width"]),
                                       "surface": r["surface"]},
                              "precondition": "新路径在板框内且不越界",
                              "expected": f"绕行 {ob['name']}，{code} 消除",
                              "reason": issue["reason"]})
                seen_rule_route.add((code, tid))
        elif code in ("R2", "R7"):
            rid = _resolve_route_ref(tid, routes, by_prefix, by_net)
            if rid and rid not in seen_routes and \
                    _signature({"action": "change_width", "args": {"route_id": rid}}) \
                    not in rejected and f"delete_route:{rid}" not in rejected:
                r = routes[rid]
                # 只接受"更宽敞"的平移；两方向都不安全时放弃这条建议，
                # 否则会把走线推进别的走线里（实测分数 60 → 0）
                new_pts = _best_shift(r, tid, state)
                if new_pts:
                    steps.append({"action": "delete_route", "args": {"route_id": rid},
                                  "precondition": f"{rid} 存在", "expected": "移除旧走线",
                                  "reason": issue["reason"]})
                    steps.append({"action": "add_route",
                                  "args": {"net": r["net"], "pts": new_pts,
                                           "width": _legal_width(r["width"]),
                                           "surface": r["surface"]},
                                  "precondition": "平移后仍在板框内且间距更大",
                                  "expected": f"错开走线，{code} 缓解",
                                  "reason": issue["reason"]})
                    seen_routes.add(rid)
    # 断路 → 跳线（CIR-007/AGT-001）
    from ..circuit.connectivity import jumper_candidates
    for jc in jumper_candidates(state["pads"], state["routes"], state["jumpers"])[:3]:
        steps.append({"action": "add_jumper",
                      "args": {"net": jc["net"], "from_pad": jc["from_pad"],
                               "to_pad": jc["to_pad"], "reason": jc["reason"]},
                      "precondition": "两焊盘属于同一网络",
                      "expected": "连通性恢复", "reason": jc["reason"]})
    return steps


# ---------------------------------------------------------------- 审查 Agent（AGT-002，只读）
def review_plan(ws, plan: dict, mrc_before: dict) -> dict:
    state = ws.mrc_state()
    routes = {r["id"]: r for r in state["routes"]}
    risks, ok_steps = [], []
    for s in plan["steps"]:
        a, args = s["action"], s.get("args", {})
        if a == "change_width":
            if args.get("width", 0) < 0.5:
                risks.append(f"步骤{s['seq']}：目标线宽低于工艺下限，会被拒绝")
            elif args["route_id"] not in routes:
                risks.append(f"步骤{s['seq']}：走线 {args['route_id'][:8]} 不存在"
                             "（可能已被前序步骤删除）")
            else:
                ok_steps.append(s["seq"])
        elif a == "add_route":
            from ..circuit.connectivity import validate_route
            outline = state["outline"]
            valid, why = validate_route([p[:2] for p in args["pts"]],
                                        args.get("width", 0.6), outline)
            if not valid:
                risks.append(f"步骤{s['seq']}：新走线非法——{why}")
            else:
                ok_steps.append(s["seq"])
        elif a == "add_jumper":
            ok_steps.append(s["seq"])
        else:
            ok_steps.append(s["seq"])
    return {"ok": not risks, "risks": risks, "approved_steps": ok_steps,
            "summary": (f"计划 {len(plan['steps'])} 步：{len(ok_steps)} 步通过审查，"
                        f"{len(risks)} 项风险；只读复核，不发起修改")}


def _rank_steps(ws, steps: list[dict], state: dict, mrc: dict,
                cloud_enabled: bool = False) -> tuple[list[dict], dict]:
    """用 AI 层给候选修复排序（AGT-012）；失败时原样返回，不影响主流程。

    AI 层只决定**顺序**，不增删步骤：这样"关闭云端/无模型"时执行内容完全
    一致，只是优先级退化为规则先验（AGT-010 无模型降级）。
    """
    try:
        from ..ai.features import group_proposals
        from ..ai.service import rank_proposals

        props = group_proposals(mrc.get("issues", []), steps)
        if len(props) <= 1:
            return steps, {}
        ranked = rank_proposals(props, state, mrc, cloud_enabled=cloud_enabled)
        if not ranked.get("ordered"):
            return steps, {}
        # 复合方案（reroute = 删 + 加）按排序展开回扁平步骤序列
        out: list[dict] = []
        for prop in ranked["ordered"]:
            out.extend(prop["steps"])
        # 排序后可能丢步骤（分组异常兜底）：长度不符就退回原顺序
        if len(out) != len(steps):
            return steps, {"source": "heuristic",
                           "error": "分组后步骤数不符，已退回原顺序"}
        return out, {"source": ranked.get("source"), "error": ranked.get("error")}
    except Exception as e:      # AI 层任何异常都不得影响设计流程
        return steps, {"source": None, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 运行循环（AGT-005/006/007）
def _resolve_cloud_flag(ws) -> bool:
    """cloud_enabled 未显式传入时读工程设置（比赛版默认开，产品版默认关）。"""
    try:
        return bool(ws.store.data_dir.joinpath("settings.json")
                    .exists() and json.loads(
                        ws.store.data_dir.joinpath("settings.json")
                        .read_text("utf-8")).get("cloud_enabled", False))
    except Exception:
        return False


def _apply_approved_steps(ws, plan: dict, review: dict, rounds: int,
                          audit: list, applied_steps: list) -> tuple[list, int]:
    """应用本轮通过审查的步骤。返回 (round_steps, applied 数)。

    失败的步骤不改变状态（CIR-012），只记审计。成功步骤记下实际主键：
    重放时新建对象 id 必然不同，需要一一映射。
    """
    round_steps: list[dict] = []
    applied = 0
    for s in plan["steps"]:
        if s["seq"] not in review["approved_steps"]:
            continue
        try:
            res = apply_step(ws, s)
            applied += 1
            rec = dict(s)
            if isinstance(res, dict) and res.get("id"):
                rec["_result_id"] = res["id"]
            round_steps.append(rec)
            applied_steps.append(rec)
            audit.append({"round": rounds, "seq": s["seq"], "action": s["action"],
                          "expected": s.get("expected", ""), "ok": True})
        except Exception as e:   # 失败不改变状态（CIR-012）
            audit.append({"round": rounds, "seq": s["seq"], "action": s["action"],
                          "error": str(e)})
    return round_steps, applied


def _arbiter_rollback(ws, trial: int, cur: int, undo_depth: int, step_mark: int,
                      round_steps: list, rounds: int, audit: list,
                      applied_steps: list, rejected: set) -> bool:
    """裁决 Agent（AGT-003/007）：本轮让分数变差就整轮回退。返回是否回退。

    早期没有回退，R2/R7 的错开与新违规会互相触发，分数在 94↔91 之间
    来回震荡——"对比卡片 after ≥ before"这类验收要求也就无从满足。
    回退后记住被否决的方案指纹：状态没变时同样的建议会原样重现，
    不过滤就会无限重试同一个坏方案。
    """
    if trial >= cur:
        return False
    for _ in range(len(ws.undo_stack) - undo_depth):
        if ws.undo_stack:
            ws.undo()
    applied = 0
    # 按下标截断：不能按 dict 相等过滤——不同轮的步骤可能内容完全相同
    # （同一走线反复被建议错开），相等比较会把之前成功的轮次一起删掉。
    del applied_steps[step_mark:]
    for s in round_steps:
        audit.append({"round": rounds, "seq": s["seq"], "action": s["action"],
                      "note": f"已回退：本轮使分数 {cur}→{trial}，"
                              "按裁决规则整体撤销（AGT-003）"})
        rejected.add(_signature(s))
    round_steps.clear()
    return applied == 0


def _stop_verdict(cur: int, no_improve: int, no_improve_stop: int,
                  rounds: int, max_rounds: int) -> str:
    """裁决（AGT-003/007）：循环结束后说明触发原因。

    结论必须是"停止"并说明触发原因，不得在已停止的运行里报告"继续"
    （轮数上限/无改进/满分均属收敛保护停止）。
    """
    if cur >= 100:
        return "已达满分 100，可打性验收通过，停止（AGT-007）"
    if no_improve >= no_improve_stop:
        return f"连续 {no_improve} 轮无改进，按收敛保护停止（AGT-007/003）"
    if rounds >= max_rounds:
        return f"已达轮数上限 {max_rounds}，按收敛保护停止（AGT-007）"
    return "无更多可执行建议，提前收敛停止（AGT-007）"


def run_designer(ws, auth="step_confirm", max_rounds=8, no_improve_stop=3,
                 auto_confirm=True, verbose=False, cloud_enabled: bool | None = None):
    """检查→计划→审查→应用→复检 循环（AGT-001/002/005/006/007）。

    返回 applied_steps：实际执行成功的步骤（含 args），供重放。

    ``cloud_enabled`` 为 None 时读取工程设置（比赛版默认开启，产品版默认关）。
    """
    if cloud_enabled is None:
        cloud_enabled = _resolve_cloud_flag(ws)
    t0 = time.time()
    base_commit = ws.project["head_commit_id"]   # 重放锚点（AGT-008）
    mrc = ws.run_mrc()
    score_start = mrc["score"]
    cur = score_start
    rounds, cards, audit, applied_steps = 0, [], [], []
    ranking: dict = {}        # 循环可能零次执行（满分/0 轮），return 也要能引用
    best = score_start
    no_improve = 0
    _rejected: set[str] = set()          # 已被裁决回退的方案指纹（避免无限重试）
    while rounds < max_rounds and no_improve < no_improve_stop and cur < 100:
        rounds += 1
        state = ws.mrc_state()
        steps = designer_propose(ws, mrc, state, _rejected)
        if not steps:
            audit.append({"round": rounds, "note": "无更多可执行建议，停止"})
            break
        # AI 收益排序（AGT-012）：同样 8 轮预算，先修高收益项。
        # AI 不可用时 rank_proposals 自动降级到规则先验，步骤集合不变，
        # 只影响顺序——因此关闭 AI 不会让流程失效（AGT-010）。
        steps, ranking = _rank_steps(ws, steps, state, mrc, cloud_enabled)
        if ranking.get("source"):
            audit.append({"round": rounds,
                          "note": f"AI 排序（{_SRC_CN.get(ranking['source'], ranking['source'])}）："
                                  f"{len(steps)} 条候选按期望收益排序"})
        plan = make_plan(ws, f"提升可打性分数（当前 {cur}）", steps, "designer")
        review = review_plan(ws, plan, mrc)
        applied = 0
        if auth == "step_confirm" and not auto_confirm:
            raise PermissionError("step_confirm 模式需要逐步确认（由 UI 层驱动）")
        undo_depth = len(ws.undo_stack)      # 本轮回退锚点
        step_mark = len(applied_steps)       # applied_steps 的回退锚点（按下标）
        if auth != "suggest":
            round_steps, applied = _apply_approved_steps(
                ws, plan, review, rounds, audit, applied_steps)
        else:
            round_steps = []
        # 裁决 Agent（AGT-003/007）：本轮若让分数变差就整轮回退。
        # 早期没有回退，R2/R7 的错开与新违规会互相触发，分数在 94↔91 之间
        # 来回震荡——"对比卡片 after ≥ before"这类验收要求也就无从满足。
        # 只算一次 MRC：本轮结果既用于裁决（是否变差要回退），
        # 也用于轮末卡片。早期这里调两次（trial + 轮末重算），
        # 5 轮下来多算 5 次完整规则检查（实测单次约 5 ms，共 25 ms，
        # 占 Agent 总耗时的 40%）。
        trial_mrc = ws.run_mrc() if applied else None
        trial = trial_mrc["score"] if trial_mrc else cur
        rolled_back = bool(applied) and _arbiter_rollback(
            ws, trial, cur, undo_depth, step_mark, round_steps, rounds,
            audit, applied_steps, _rejected)
        if rolled_back:
            applied = 0
        if applied:
            ws.commit(f"Agent 第 {rounds} 轮修改（设计 Agent，授权 {auth}）",
                      author="designer")
            # commit 不改设计内容（只写版本账本），所以 trial_mrc 仍然有效。
            # 只有回退分支需要重算——回退撤销了本轮的修改。
            mrc = trial_mrc if trial_mrc is not None else ws.run_mrc()
        else:
            mrc = ws.run_mrc()
        if rolled_back:
            mrc = ws.run_mrc()      # 回退后设计已变，必须重算
        cards.append({"round": rounds, "before": cur, "after": mrc["score"],
                      "steps": applied, "rolled_back": rolled_back,
                      "actions": [s["action"] for s in plan["steps"]
                                  if s["seq"] in review["approved_steps"]][:8],
                      "top_issues": [i["reason"] for i in mrc["issues"]][:3]})
        if mrc["score"] > best:
            best = mrc["score"]
            no_improve = 0
        else:
            no_improve += 1
        cur = mrc["score"]
    stop_note = _stop_verdict(cur, no_improve, no_improve_stop, rounds, max_rounds)
    return {"rounds": rounds, "score_start": score_start, "score_final": cur,
            "cards": cards, "audit": audit, "applied_steps": applied_steps,
            "verdict": "停止", "elapsed_s": round(time.time() - t0, 2),
            "base_commit": base_commit,
            "ai_ranking": ranking.get("source") or "heuristic",
            "ai_error": ranking.get("error"),
            "arbiter_note": stop_note}


def replay(snapshot_ws, applied_steps: list[dict], live_ws, slice_fn,
           source_commit: str | None = None) -> dict:
    """重放（AGT-008）：同一起始版本 + 同一步骤 + 同一工具版本 →
    设计摘要一致，重切 G-code 哈希一致。snapshot_ws 为运行前克隆。

    G-code 头部含 source_commit，两边必须锚定同一版本，否则哈希天然不同。

    **id 映射**：新建走线/跳线的主键是每次生成的 uuid，重放时必然不同。
    若后续步骤引用"上一轮新建的对象"，直接用原 id 会因"对象不存在"而失败，
    重放结果与原始运行不可比。因此按 `_result_id`（原始运行的实际主键）
    建立 原 id → 重放 id 的映射，逐步骤翻译后再执行。
    """
    idmap: dict[str, str] = {}

    def _tr(val):
        return idmap.get(val, val) if isinstance(val, str) else val

    for s in applied_steps:
        step = {**s, "args": dict(s.get("args") or {})}
        a = step["action"]
        if a == "change_width":
            step["args"]["route_id"] = _tr(step["args"].get("route_id", ""))
        elif a == "delete_route":
            step["args"]["route_id"] = _tr(step["args"].get("route_id", ""))
        elif a == "move_comp":
            step["args"]["comp_id"] = _tr(step["args"].get("comp_id", ""))
        try:
            res = apply_step(snapshot_ws, step)
        except Exception as e:
            return {"design_match": False, "gcode_match": False,
                    "digest": snapshot_ws.design_digest()[:12], "gcode_hash": None,
                    "anchor_commit": source_commit,
                    "replay_error": f"步骤 {a} 重放失败：{type(e).__name__}: {e}"}
        # 记录本轮新建对象：原始运行的实际 id → 重放产生的 id
        src_id = s.get("_result_id")
        if src_id and isinstance(res, dict) and res.get("id"):
            idmap[src_id] = res["id"]

    design_match = live_ws.design_digest() == snapshot_ws.design_digest()
    anchor = source_commit
    if anchor is None:
        hist = snapshot_ws.history()
        anchor = hist[-1]["id"] if hist else None
    # 切片可能因连通性/材料/参数被阻断——那是重放的合法结果，不能抛给界面
    try:
        g1 = slice_fn(live_ws, source_commit=anchor)["gcode_hash"]
        g2 = slice_fn(snapshot_ws, source_commit=anchor)["gcode_hash"]
    except Exception as e:
        return {"design_match": design_match, "gcode_match": False,
                "digest": snapshot_ws.design_digest()[:12], "gcode_hash": None,
                "anchor_commit": anchor,
                "slice_error": f"{type(e).__name__}: {e}"}
    return {"design_match": design_match, "gcode_match": g1 == g2,
            "digest": snapshot_ws.design_digest()[:12], "gcode_hash": g2,
            "anchor_commit": anchor}


# ---------------------------------------------------------------- 重放（AGT-008）
def clone_workspace(ws, tag="replay"):
    """克隆当前工程库（blob 共享只读），用于计划重放与断言对比。

    两种后端走不同的克隆方式：

    - **SQLite**：WAL checkpoint 后整文件拷贝（原有路径，速度最快）。
    - **PostgreSQL**：没有文件可拷，改用 `storage.clone_to_new_schema()`。
      早期这里直接读 `ws.store.db_path` 并执行 `PRAGMA wal_checkpoint`——
      PG 的 `PgStore` 没有 `db_path`、也不认 PRAGMA，**云上重放（AGT-008）
      会直接抛 AttributeError**。而 AGT-008 是 P0 验收项（"抽 10 个计划重放，
      100% 一致"），云上跑不了等于该验收项在 RDS 形态下不成立。
    """
    from ..db.store import Store
    from ..ledger.workspace import Workspace

    store = ws.store
    # SQLite 实现有 clone_db_file；PG 实现走 clone_to_new_schema
    if hasattr(store, "clone_db_file"):
        dst = store.clone_db_file(f"{tag}-{new_id()[:8]}")
        return Workspace(Store(dst), ws.project_id)

    # PostgreSQL：交给实现层复制 schema（同结构、同数据、新 schema 名）
    clone = store.clone_to_new_schema(tag)
    return Workspace(clone, ws.project_id)
