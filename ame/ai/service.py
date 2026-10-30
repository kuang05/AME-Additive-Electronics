"""AI 门面：排序、解释、缺陷归因增强（界面与 Agent 的唯一入口）.

三级降级链（AGT-010 / AGT-012 / NFR-007）
-----------------------------------------
1. 云端可用且用户开启  → 云端打分（华为云在线推理）
2. 云端不可用/失败     → 本地模型打分（随包权重）
3. 本地权重缺失/损坏   → 启发式先验打分

三层里**任何一层失败都不抛异常给调用方**，只在结果里带 `source` 说明
到底用的是哪一层。这样"关闭云端核心功能不缺失"是可以被测试断言的，
而不是一句承诺（`tests/test_ai_layer.py` 逐层覆盖）。

为什么排序有价值
----------------
设计 Agent 一次可能给出十几条修复。原来的实现按问题列表原始顺序逐条执行，
前几轮常常先去改"扣分少且容易反弹"的项，轮数被浪费。按**期望收益**排序后，
同样的 8 轮预算能在前 2 轮命中高收益项——这正是 T19「2 轮 +23 分」的机理。
"""
from __future__ import annotations

from . import features as _features
from .cloud import CloudRanker
from .local import default_model

# 过程内云端单例（由宿主注入 transport 后启用）
_CLOUD: CloudRanker | None = None


def configure_cloud(transport=None, endpoint: str | None = None,
                    api_key: str | None = None, enabled: bool = True) -> CloudRanker:
    """注入云端 transport（宿主/验收脚本调用）。不调用则云端始终不可用。"""
    global _CLOUD
    kw = {}
    if endpoint:
        kw["endpoint"] = endpoint
    _CLOUD = CloudRanker(transport=transport, api_key=api_key,
                         enabled=enabled, **kw)
    return _CLOUD


def cloud_ranker() -> CloudRanker | None:
    return _CLOUD


def reset_cloud() -> None:
    """测试用：撤销注入，回到纯本地。"""
    global _CLOUD
    _CLOUD = None


def ai_status(cloud_enabled: bool = False) -> dict:
    """当前 AI 能力状态（设置界面与验收报告用）。"""
    model = default_model()
    ranker = _CLOUD
    cloud_on = bool(cloud_enabled and ranker is not None and ranker.available)
    if cloud_on:
        source = "cloud"
    elif model.available:
        source = "local"
    else:
        source = "heuristic"
    return {
        "source": source,
        "feature_version": _features.FEATURE_VERSION,
        "feature_dim": len(_features.FEATURE_NAMES),
        "cloud_enabled_setting": bool(cloud_enabled),
        "local": model.describe(),
        "cloud": ranker.describe() if ranker else {"available": False,
                                                   "reason": "未注入 transport"},
    }


def rank_proposals(proposals: list[dict], state: dict, mrc: dict,
                   cloud_enabled: bool = False) -> dict:
    """按期望收益给候选修复排序。

    返回 {"ordered": [...], "scores": [...], "source": "cloud|local|heuristic",
          "error": str|None}。`ordered` 里每项附加 `_score` 与 `_rank`。
    """
    if not proposals:
        return {"ordered": [], "scores": [], "source": "heuristic", "error": None}

    rows = [_features.proposal_features(p, state, mrc) for p in proposals]
    model = default_model()
    ranker = _CLOUD
    err = None

    if cloud_enabled and ranker is not None and ranker.available:
        res = ranker.rank(rows)
        if res.ok:
            scores = res.scores
            source = "cloud"
        else:
            err = res.error
            scores = [model.score(p, f) for p, f in zip(proposals, rows, strict=True)]
            source = "local" if model.available else "heuristic"
    else:
        scores = [model.score(p, f) for p, f in zip(proposals, rows, strict=True)]
        source = "local" if model.available else "heuristic"

    # 同分时按原始顺序稳定排序（不能因排序不确定性破坏重放一致性）
    idx = sorted(range(len(proposals)), key=lambda i: (-scores[i], i))
    ordered = []
    for rank, i in enumerate(idx, 1):
        p = dict(proposals[i])
        p["_score"] = round(float(scores[i]), 4)
        p["_rank"] = rank
        ordered.append(p)
    return {"ordered": ordered, "scores": [round(float(s), 4) for s in scores],
            "source": source, "error": err}


def explain_issue_ai(issue: dict, state: dict, cloud_enabled: bool = False) -> str:
    """规则解释 + AI 补充的优先级判断（MRC-009 的解释增强）。

    本地规则给出"为什么违规"，AI 层补充"先修哪条、为什么"。
    云端不可用时只用本地/启发式，输出仍然完整。
    """
    from ..mrc.engine import explain_issue

    base = explain_issue(issue, state)
    single = [{"action": _suggest_action(issue), "issue": issue,
               "reason": issue.get("reason", ""), "steps": []}]
    ranked = rank_proposals(single, state,
                            {"score": None, "issues": [issue]}, cloud_enabled)
    p = ranked["ordered"][0]
    src_cn = {"cloud": "云端模型", "local": "本地模型", "heuristic": "规则先验"}
    lines = [base, "",
             f"AI 优先级（{src_cn.get(ranked['source'], ranked['source'])}）："
             f"收益评分 {p['_score']}",
             f"建议动作：{_ACTION_CN.get(p['action'], p['action'])}"]
    if ranked.get("error"):
        lines.append(f"（云端调用失败已降级：{ranked['error']}）")
    return "\n".join(lines)


_ACTION_CN = {
    "change_width": "加宽走线到工艺基线",
    "reroute": "绕开障碍重新布线",
    "add_route": "补一条新走线",
    "add_jumper": "加 0Ω 跳线跨接",
    "move_comp": "移动器件腾出空间",
}


def _suggest_action(issue: dict) -> str:
    """按规则与目标类型推断最可能的修复动作（与设计 Agent 的动作集一致）。"""
    code = issue.get("rule_code", "")
    if code == "R1":
        return "change_width"
    if code in ("R2", "R7"):
        return "reroute"
    if code in ("R4", "R5"):
        return "reroute"
    if code == "R3":
        return "move_comp"
    if code == "R6":
        return "reroute"
    return "change_width"


def suggest_defect_cause(defect: dict, context: dict | None = None,
                         cloud_enabled: bool = False) -> dict:
    """缺陷归因增强（PRT-009）：在规则归因之上补充可疑设计因素的排序。

    输入是缺陷 dict（含 type / pass_no / layer_z_mm / at）。输出不改写
    既有归因结论，只给出"设计侧最可能的三个可疑点"供人工复核。
    """
    ctx = context or {}
    dtype = defect.get("type", "")
    at = defect.get("at") or [0.0, 0.0]
    candidates: list[dict] = []

    routes = ctx.get("routes") or []
    for r in routes:
        pts = r.get("pts") or []
        if len(pts) < 2:
            continue
        d = _dist_to_polyline(at[0], at[1], pts)
        if d is not None and d <= 3.0:
            candidates.append({
                "kind": "route", "id": r.get("id"), "net": r.get("net"),
                "distance_mm": round(d, 2),
                "hint": f"缺陷落点距走线 {r.get('net')} 中心线 {d:.2f} mm",
            })
    candidates.sort(key=lambda c: c["distance_mm"])

    base = {"defect_type": dtype, "pass_no": defect.get("pass_no"),
            "suspects": candidates[:3]}
    if not candidates:
        base["note"] = "落点附近 3 mm 内无走线，优先怀疑工艺/设备因素"
    return base


def _dist_to_polyline(x: float, y: float, pts) -> float | None:
    """点到折线的最近距离（只用标准库，避免本层引入几何依赖）。"""
    import math
    best = None
    for i in range(1, len(pts)):
        try:
            x1, y1 = float(pts[i - 1][0]), float(pts[i - 1][1])
            x2, y2 = float(pts[i][0]), float(pts[i][1])
        except (TypeError, IndexError, ValueError):
            return None
        dx, dy = x2 - x1, y2 - y1
        seg2 = dx * dx + dy * dy
        if seg2 <= 1e-12:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / seg2))
        px, py = x1 + t * dx, y1 + t * dy
        d = math.hypot(x - px, y - py)
        best = d if best is None else min(best, d)
    return best


__all__ = ["ai_status", "configure_cloud", "cloud_ranker", "reset_cloud",
           "rank_proposals", "explain_issue_ai", "suggest_defect_cause"]
