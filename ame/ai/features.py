"""问题/修复建议 → 定长特征向量（纯数据，不含模型与网络）。

为什么单独一层
--------------
排序"先修哪条"是个回归问题：每个候选修复的**期望分数收益**取决于规则、
严重度、目标几何与板面规模。把这层独立出来有三个好处：

1. 训练（train.py）与推理（local.py/cloud.py）用**同一份**特征定义，
   不会出现"训练时 24 维、线上 26 维"的静默错配；
2. 特征只读 state，无副作用，可对同一工程设计重复计算得到相同向量
   （AGT-008 重放 / SLC-008 确定性要求）；
3. 云端只上传这组数值，不上传工程几何（DAT-007 隐私）。

特征维度固定为 ``len(FEATURE_NAMES)``；新增特征必须同步改权重版本号。
"""
from __future__ import annotations

import math

# 规则与动作的固定顺序：one-hot 的位序一旦上线就不能重排
RULE_ORDER = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
ACTION_ORDER = ("change_width", "reroute", "add_route", "add_jumper", "move_comp")

FEATURE_NAMES = (
    [f"rule_{r}" for r in RULE_ORDER]          # 7：命中哪条规则
    + ["severity_norm", "penalty_norm"]        # 2：严重度与扣分
    + [f"act_{a}" for a in ACTION_ORDER]       # 5：修复动作类型
    + ["target_route", "target_pair", "target_pad",
       "target_face", "target_trench"]         # 5：目标对象种类
    + ["width_cur_norm", "pts_count_norm", "path_len_norm",
       "score_norm", "n_issues_norm", "n_routes_norm", "board_area_norm"]
)

FEATURE_VERSION = 1


def _norm(v: float, scale: float) -> float:
    """有界归一化：避免量纲把线性模型直接压死，同时防止 NaN 传染。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return max(-1.0, min(1.0, v / scale))


def _polyline_len(pts) -> float:
    total = 0.0
    for i in range(1, len(pts)):
        try:
            total += math.hypot(pts[i][0] - pts[i - 1][0],
                                pts[i][1] - pts[i - 1][1])
        except (TypeError, IndexError):
            return 0.0
    return total


def _outline_area(state: dict) -> float:
    pts = state.get("outline") or []
    if len(pts) < 3:
        return 0.0
    # 鞋带公式：只用于量级归一化，不要求严格简单多边形
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % len(pts)][0], pts[(i + 1) % len(pts)][1]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def group_proposals(issues: list[dict], steps: list[dict]) -> list[dict]:
    """把设计 Agent 的扁平步骤聚成"一个可独立评价的修复".

    设计 Agent 对"绕行/平移"类问题会发两条步骤（先删旧线、再加新线）。
    单独评价 delete_route 会得到"删线后分数变高"的假信号——那是把问题
    对象删掉了，不是修好了。聚成一个复合方案后，标签才反映真实收益。
    """
    out: list[dict] = []
    i = 0
    while i < len(steps):
        s = steps[i]
        nxt = steps[i + 1] if i + 1 < len(steps) else None
        if (s.get("action") == "delete_route" and nxt
                and nxt.get("action") == "add_route"
                and nxt.get("args", {}).get("net") is not None):
            out.append({"action": "reroute", "steps": [s, nxt],
                        "reason": s.get("reason", ""),
                        "expected": nxt.get("expected", "")})
            i += 2
            continue
        out.append({"action": s.get("action", ""), "steps": [s],
                    "reason": s.get("reason", ""),
                    "expected": s.get("expected", "")})
        i += 1
    # 把问题挂到方案上：按 reason 精确匹配优先，否则取同规则的第一条
    by_reason = {}
    for it in issues:
        by_reason.setdefault(it.get("reason", ""), it)
    for p in out:
        p["issue"] = by_reason.get(p["reason"]) or {}
    return out


def proposal_features(proposal: dict, state: dict, mrc: dict) -> list[float]:
    """一个候选修复的特征向量（顺序与 FEATURE_NAMES 严格一致）。"""
    issue = proposal.get("issue") or {}
    code = issue.get("rule_code", "")
    action = proposal.get("action", "")
    tid = str(issue.get("target_id", ""))
    ttype = issue.get("target_type", "")

    routes = {r["id"]: r for r in state.get("routes", [])}
    target = routes.get(tid)

    feats: list[float] = []
    feats += [1.0 if code == r else 0.0 for r in RULE_ORDER]
    feats.append(_norm(issue.get("severity", 0), 3.0))
    feats.append(_norm(issue.get("penalty", 0), 10.0))
    feats += [1.0 if action == a else 0.0 for a in ACTION_ORDER]
    feats += [
        1.0 if ttype == "route" else 0.0,
        1.0 if "|" in tid else 0.0,
        1.0 if ttype == "pad" else 0.0,
        1.0 if ttype == "face" else 0.0,
        1.0 if ttype == "trench" else 0.0,
    ]
    feats.append(_norm(target.get("width", 0) if target else 0, 2.0))
    feats.append(_norm(len(target.get("pts", [])) if target else 0, 10.0))
    feats.append(_norm(_polyline_len(target.get("pts", [])) if target else 0, 200.0))
    feats.append(_norm(mrc.get("score") or 0, 100.0))
    feats.append(_norm(len(mrc.get("issues") or []), 50.0))
    feats.append(_norm(len(routes), 300.0))
    feats.append(_norm(_outline_area(state), 40000.0))

    assert len(feats) == len(FEATURE_NAMES), "特征维度与 FEATURE_NAMES 不一致"
    return feats


__all__ = ["RULE_ORDER", "ACTION_ORDER", "FEATURE_NAMES", "FEATURE_VERSION",
           "group_proposals", "proposal_features"]
