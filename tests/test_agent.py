"""智能体域验收测试（T19；AGT-002/004/006/007/008/009/010）。

覆盖：
- T19/AGT-001：固定验收板 T19-A（初始 62±3 分）在 auto 授权下提升 ≥15 分、
  轮数 ≤8，对比卡片含 before/after/steps；
- AGT-008：重放——同一起始版本（base_commit 锚点）+ 同一步骤 → 设计摘要与
  G-code 哈希一致，且重放可复现；
- AGT-002：审查 Agent 只读，指出非法步骤风险（线宽低于工艺下限）；
- AGT-004：计划步骤含前置条件与期望结果，计划落库可查；
- AGT-006：三级授权——suggest 不改状态，step_confirm+auto_confirm 行同 auto；
- AGT-007：轮数上限停止并说明；连续无改进按收敛保护停止（arbiter_note 含“收敛”）；
- AGT-009：apply_step 只接受白名单动作，实际走命令总线（与人工操作同源）；
- AGT-010：整个流程不访问网络（对 loop.py 的文本断言）。
"""
import json
from pathlib import Path

import pytest

from conftest import build_project, do_slice
from ame.agent.loop import (apply_step, clone_workspace, designer_propose,
                            make_plan, replay, review_plan, run_designer)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- T19 / AGT-008
def test_t19_designer_improvement_and_replay(data_root):
    """T19：T19-A 初始分 ∈ [59,65]（62±3，断言区间不断言具体值）；
    auto 授权 8 轮内提升 ≥15 分；对比卡片非空且含 before/after/steps；
    AGT-008 重放：design_match 与 gcode_match 均为 True，且重放可复现。"""
    ws = build_project(data_root)            # 板文件自带 8 条违规走线（0.3mm 细线、近距对）
    mrc0 = ws.run_mrc()
    assert 59 <= mrc0["score"] <= 65, f"T19-A 初始分应在 62±3 区间，实际 {mrc0['score']}"

    snap1 = clone_workspace(ws)              # 运行前克隆（重放用，AGT-008）
    snap2 = clone_workspace(ws)              # 第二次重放用（验证可复现）

    res = run_designer(ws, "auto", 8)
    assert res["score_final"] - res["score_start"] >= 15, "T19 验收：提升 ≥15 分"
    assert res["rounds"] <= 8, "T19 验收：轮数 ≤8"
    assert res["applied_steps"], "auto 授权下应有实际执行的步骤"
    assert res["base_commit"], "返回值应带运行前 head 版本（重放锚点）"

    # AGT-005 对比卡片：修改前后分数、改动步骤
    assert res["cards"]
    for card in res["cards"]:
        assert {"before", "after", "steps"} <= set(card)
        assert card["after"] >= card["before"]

    # AGT-008 重放：同一起始版本 + 同一步骤 → 设计摘要一致、G-code 哈希一致
    rp1 = replay(snap1, res["applied_steps"], ws, do_slice,
                 source_commit=res["base_commit"])
    assert rp1["design_match"] is True
    assert rp1["gcode_match"] is True
    # 再跑一次重放，结果一致（可复现）
    rp2 = replay(snap2, res["applied_steps"], ws, do_slice,
                 source_commit=res["base_commit"])
    assert rp2["design_match"] == rp1["design_match"]
    assert rp2["gcode_hash"] == rp1["gcode_hash"]


# ---------------------------------------------------------------- AGT-002
def test_agt002_review_plan_flags_illegal_step(data_root):
    """AGT-002：审查 Agent 对含非法步骤（线宽 0.3 的 change_width）的计划
    给出非空 risks 且 ok=False；审查为只读，不改变设计。"""
    ws = build_project(data_root)
    rid = ws.routes()[0]["id"]
    digest0 = ws.design_digest()
    plan = make_plan(ws, "含非法步骤的计划", [
        {"action": "change_width",
         "args": {"route_id": rid, "width": 0.3},
         "precondition": "走线存在",
         "expected": "线宽 0.30 mm（低于工艺下限，应被拦截）"}])
    review = review_plan(ws, plan, ws.run_mrc())
    assert review["ok"] is False
    assert review["risks"], "审查必须指出：目标线宽低于工艺下限会被拒绝"
    assert ws.design_digest() == digest0, "审查 Agent 只读复核，不得改变设计"


# ---------------------------------------------------------------- AGT-004
def test_agt004_plan_steps_have_precondition_expected_and_persisted(data_root):
    """AGT-004：计划步骤含前置条件与期望结果；计划落库（plan 表可查），
    记录目标、步骤、工具版本与状态。"""
    ws = build_project(data_root)
    mrc = ws.run_mrc()
    steps = designer_propose(ws, mrc, ws.mrc_state())
    assert steps, "T19-A 存在违规走线，设计 Agent 应给出可执行修改步骤"
    plan = make_plan(ws, "提升可打性分数", steps)
    for s in plan["steps"]:
        assert s.get("precondition"), f"步骤 {s['seq']} 缺前置条件"
        assert s.get("expected"), f"步骤 {s['seq']} 缺期望结果"
    assert [s["seq"] for s in plan["steps"]] == list(range(1, len(steps) + 1))
    # 计划落库
    row = ws.store.one("SELECT * FROM plan WHERE id=?", (plan["id"],))
    assert row is not None, "计划应写入 plan 表（可保存、可回放）"
    assert row["goal"] == "提升可打性分数"
    assert json.loads(row["steps_json"]) == plan["steps"]
    assert row["status"] == "draft"
    assert row["agent_role"] == "designer"
    assert json.loads(row["tool_versions"]), "工具版本应随计划入库（AGT-008 前提）"


# ---------------------------------------------------------------- AGT-006
def test_agt006_auth_suggest_changes_nothing(data_root):
    """AGT-006：只建议（suggest）授权下不执行任何步骤、分数不变、设计不变。"""
    ws = build_project(data_root)
    digest0 = ws.design_digest()
    r = run_designer(ws, auth="suggest", max_rounds=8)
    assert r["applied_steps"] == []
    assert r["score_final"] == r["score_start"]
    assert ws.design_digest() == digest0


def test_agt006_step_confirm_with_auto_confirm_behaves_like_auto(data_root):
    """AGT-006：step_confirm + auto_confirm=True（确认由 UI 驱动，测试用自动确认）
    行为同 auto：有实际执行步骤、分数提升 ≥15、轮数 ≤8。"""
    ws = build_project(data_root)
    r = run_designer(ws, auth="step_confirm", max_rounds=8, auto_confirm=True)
    assert r["applied_steps"]
    assert r["score_final"] - r["score_start"] >= 15
    assert r["rounds"] <= 8


# ---------------------------------------------------------------- AGT-007
def test_agt007_round_cap_stops_with_reason(data_root):
    """AGT-007：把 max_rounds=2 传入 → rounds ≤2，且 verdict 给出停止结论。"""
    ws = build_project(data_root)
    r = run_designer(ws, auth="auto", max_rounds=2)
    assert r["rounds"] <= 2
    assert "停止" in r["verdict"], "达到轮数上限必须停止并在裁决结论中说明"
    assert r["arbiter_note"], "AGT-011 审计：停止原因应写入裁决说明"


def test_agt007_no_improvement_convergence_stop(data_root):
    """AGT-007：score<100 时连续无改进按收敛保护停止，arbiter_note 含“收敛”。
    suggest 授权保证每轮零改进（步骤只建议不执行），用于触发该路径。"""
    ws = build_project(data_root)
    r = run_designer(ws, auth="suggest", max_rounds=8)
    assert r["score_final"] < 100
    assert r["rounds"] == 3, "no_improve_stop=3：连续 3 轮无改进即停（AGT-003）"
    assert "收敛" in r["arbiter_note"]
    assert "停止" in r["verdict"]


# ---------------------------------------------------------------- AGT-009
def test_agt009_apply_step_whitelist_and_command_bus(data_root):
    """AGT-009：apply_step 只接受白名单动作（越权动作报“只能做”）；
    实际执行走命令总线（ws.cmd_*），routes 表宽度变化且人工可撤销。"""
    ws = build_project(data_root)
    # 白名单之外的动作拒绝
    with pytest.raises(ValueError, match="只能做"):
        apply_step(ws, {"action": "hack_db", "args": {}})
    # 抽查：change_width 经命令总线生效
    route = next(r for r in ws.routes() if r["width_mm"] < 0.5)   # T19-A 的 0.3mm 细线
    apply_step(ws, {"action": "change_width",
                    "args": {"route_id": route["id"], "width": 0.6}})
    row = ws.store.one("SELECT width_mm FROM route WHERE id=?", (route["id"],))
    assert row["width_mm"] == pytest.approx(0.6), "routes 表宽度应确实变化"
    assert ws.op_log and ws.op_log[-1]["cmd"] == "change_width", \
        "操作应记入命令总线 op_log（人与 Agent 同源）"
    # 人工等效：同一修改可被人工撤销复原（AGT-009 验收要点）
    ws.undo()
    row2 = ws.store.one("SELECT width_mm FROM route WHERE id=?", (route["id"],))
    assert row2["width_mm"] == pytest.approx(0.3)


# ---------------------------------------------------------------- AGT-010
def test_agt010_designer_loop_has_no_network_access():
    """AGT-010：无模型降级——设计/审查/裁决流程为纯本地确定性规则，
    代码不包含任何网络访问（文本审查断言）。"""
    src = (ROOT / "ame" / "agent" / "loop.py").read_text("utf-8")
    banned = ("requests", "urllib", "urlopen", "socket", "httpx",
              "aiohttp", "http://", "https://")
    for token in banned:
        assert token not in src, f"AGT-010：智能体流程不得包含网络访问痕迹（{token}）"
