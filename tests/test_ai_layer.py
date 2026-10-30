"""AI 层验收（AGT-010 无模型降级 / AGT-012 云端可选 / NFR-007 离线）.

覆盖：
- 特征层确定性：同一份设计 → 同一向量（重放/复现的前提）；
- 三级降级链：云端可用 → 云端；云端失败 → 本地；权重缺失 → 规则先验；
- 云端"只上传数值"：请求体不含器件名/网络名/坐标（DAT-007 隐私）；
- 云端异常不穿透：传输失败/响应畸形/条数不符都降级而非抛异常（NFR-007）；
- 训练产物可验证：权重维度与特征一致、指标随权重落盘；
- 排序稳定性：同分时保持原始顺序（不能让排序抖动破坏重放一致性）。
"""
from __future__ import annotations

import json

import pytest

from ame.ai import features as F
from ame.ai.cloud import Transport
from ame.ai.local import LocalModel, heuristic_score, reset_default_model
from ame.ai.service import (ai_status, configure_cloud, explain_issue_ai,
                            rank_proposals, reset_cloud, suggest_defect_cause)
from ame.ai.train import WEIGHTS_PATH
from tests.conftest import build_project


@pytest.fixture(autouse=True)
def _clean_ai_state():
    """每个用例前后复位本地/云端单例，避免用例间互相污染。"""
    reset_default_model()
    reset_cloud()
    yield
    reset_default_model()
    reset_cloud()


@pytest.fixture()
def ws_and_mrc(data_root):
    ws = build_project(data_root, name="AI")
    return ws, ws.run_mrc(), ws.mrc_state()


def _proposals(ws, mrc, state):
    from ame.agent.loop import designer_propose
    return F.group_proposals(mrc["issues"], designer_propose(ws, mrc, state))


# ================================================================ 特征层
def test_features_deterministic_and_fixed_dim(ws_and_mrc):
    """特征维度固定，且同一状态重复取值完全一致（重放可复现的前提）。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    assert props, "样例板应能生成候选修复"
    a = [F.proposal_features(p, state, mrc) for p in props]
    b = [F.proposal_features(p, state, mrc) for p in props]
    assert a == b, "同一状态两次取特征必须一致"
    assert all(len(row) == len(F.FEATURE_NAMES) for row in a)
    # 特征名与向量长度必须同步（新增特征忘了改名会在这里炸开）
    assert len(F.FEATURE_NAMES) == len(set(F.FEATURE_NAMES))


def test_features_one_hot_positions(ws_and_mrc):
    """规则/动作 one-hot 的位序是上线契约，重排会让旧权重失效。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    r1 = next((p for p in props if (p.get("issue") or {}).get("rule_code") == "R1"), None)
    if r1 is None:
        pytest.skip("样例板无 R1 问题")
    feats = F.proposal_features(r1, state, mrc)
    d = dict(zip(F.FEATURE_NAMES, feats, strict=False))
    assert d["rule_R1"] == 1.0
    assert d["act_change_width"] == 1.0
    assert sum(d[f"rule_{r}"] for r in F.RULE_ORDER) == 1.0


# ================================================================ 三级降级
def test_local_weights_load_and_dim_match():
    """随包权重能被加载，维度与特征一致（防止改了特征没重训）。"""
    model = LocalModel()
    assert model.available, f"随包权重应可用：{model.load_error}"
    payload = json.loads(model.path.read_text("utf-8"))
    assert payload["feature_version"] == F.FEATURE_VERSION
    assert len(payload["weights"]) == len(F.FEATURE_NAMES)
    assert payload["feature_names"] == list(F.FEATURE_NAMES)
    meta = payload.get("meta") or {}
    # 训练与评估记录必须随权重落盘（验收要能复核，而不是一句"训练过"）
    assert meta.get("samples", 0) > 20
    assert "loso" in meta and meta["loso"].get("blend") is not None


def test_source_is_local_by_default(ws_and_mrc):
    """未注入云端时默认走本地模型（不是启发式——权重可用就该用）。"""
    st = ai_status(cloud_enabled=False)
    assert st["source"] == "local"
    assert st["local"]["available"] is True
    assert st["cloud"]["available"] is False


def test_falls_back_to_heuristic_when_weights_broken(tmp_path, monkeypatch):
    """权重损坏 → 自动降级到规则先验，不抛异常（NFR-002 / AGT-010）。"""
    bad = tmp_path / "weights.json"
    bad.write_text("{ this is not json", "utf-8")
    model = LocalModel(bad)
    assert not model.available
    assert model.load_error
    # 维度不符也要被拒绝
    wrong = tmp_path / "w2.json"
    wrong.write_text(json.dumps({"feature_version": F.FEATURE_VERSION,
                                 "weights": [1.0, 2.0]}), "utf-8")
    m2 = LocalModel(wrong)
    assert not m2.available
    assert "维度" in (m2.load_error or "")


def test_rank_proposals_degrades_gracefully(ws_and_mrc):
    """云端开启但未注入 transport → 仍返回完整排序，来源标为本地。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    res = rank_proposals(props, state, mrc, cloud_enabled=True)
    assert res["ordered"] and len(res["ordered"]) == len(props)
    assert res["source"] in ("local", "heuristic")
    assert [p["_rank"] for p in res["ordered"]] == list(range(1, len(props) + 1))


# ================================================================ 云端注入与隐私
class _Recorder:
    """录制型 transport：模拟一次在线推理，并记下请求体供隐私断言。"""

    def __init__(self, scores_fn=None, fail=False, garbage=False, short=False):
        self.calls: list[dict] = []
        self.scores_fn = scores_fn
        self.fail = fail
        self.garbage = garbage
        self.short = short

    def __call__(self, url, payload, headers):
        self.calls.append({"url": url, "payload": payload, "headers": dict(headers)})
        if self.fail:
            raise OSError("网络不可达（模拟断网）")
        if self.garbage:
            return b"<<not-json>>"
        n = len(payload["instances"])
        scores = [0.5] * n if self.scores_fn is None else self.scores_fn(n)
        if self.short:
            scores = scores[:-1]
        return json.dumps({"scores": scores}).encode("utf-8")


def test_cloud_used_when_transport_injected(ws_and_mrc):
    """注入 transport 且开启云端 → 走云端打分，且请求体只含数值。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    rec = _Recorder()
    configure_cloud(transport=Transport(send=rec, name="recorder"))
    res = rank_proposals(props, state, mrc, cloud_enabled=True)

    assert res["source"] == "cloud"
    assert len(rec.calls) == 1
    payload = rec.calls[0]["payload"]
    assert len(payload["instances"]) == len(props)
    assert payload["feature_version"] == F.FEATURE_VERSION
    # DAT-007：请求体不得携带器件名、网络名或几何坐标
    blob = json.dumps(payload, ensure_ascii=False)
    for leak in ("U1", "GND", "走线", "焊盘"):
        assert leak not in blob, f"云端请求体泄漏了设计信息：{leak}"
    assert ai_status(cloud_enabled=True)["source"] == "cloud"


def test_cloud_failure_falls_back_to_local(ws_and_mrc):
    """断网/服务报错 → 降级到本地，排序仍完整（NFR-007）。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    configure_cloud(transport=Transport(send=_Recorder(fail=True)))
    res = rank_proposals(props, state, mrc, cloud_enabled=True)
    assert res["source"] == "local"
    assert res["error"] and "OSError" in res["error"]
    assert len(res["ordered"]) == len(props)


@pytest.mark.parametrize("mode", ["garbage", "short"])
def test_cloud_malformed_response_falls_back(ws_and_mrc, mode):
    """响应非 JSON / 条数不符 → 降级，不把坏数据当结果用。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    rec = _Recorder(garbage=(mode == "garbage"), short=(mode == "short"))
    configure_cloud(transport=Transport(send=rec))
    res = rank_proposals(props, state, mrc, cloud_enabled=True)
    assert res["source"] == "local"
    assert res["error"]


def test_cloud_disabled_setting_never_calls_transport(ws_and_mrc):
    """设置里关闭云端时，即使 transport 可用也不发请求（DAT-007）。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    rec = _Recorder()
    configure_cloud(transport=Transport(send=rec))
    res = rank_proposals(props, state, mrc, cloud_enabled=False)
    assert res["source"] == "local"
    assert rec.calls == [], "关闭云端后不得发起任何请求"


# ================================================================ 排序稳定性
def test_ranking_is_stable_on_ties(ws_and_mrc):
    """同分时必须按原始顺序（稳定排序），否则重放结果会随排序抖动。"""
    ws, mrc, state = ws_and_mrc
    props = _proposals(ws, mrc, state)
    rec = _Recorder(scores_fn=lambda n: [1.0] * n)   # 全部同分
    configure_cloud(transport=Transport(send=rec))
    res = rank_proposals(props, state, mrc, cloud_enabled=True)
    # 同分 → 保持输入顺序
    assert [p["reason"] for p in res["ordered"]] == [p["reason"] for p in props]


def test_heuristic_score_prefers_cheap_safe_actions():
    """启发式先验：改线宽优先于动器件（单参数、可撤销、不引入新几何）。"""
    i = {"rule_code": "R1", "severity": 3}
    change = {"action": "change_width", "issue": i}
    move = {"action": "move_comp", "issue": i}
    assert heuristic_score(change) > heuristic_score(move)


# ================================================================ 解释增强 & 缺陷归因
def test_explain_issue_ai_contains_rule_and_priority(ws_and_mrc):
    ws, mrc, state = ws_and_mrc
    issue = mrc["issues"][0]
    text = explain_issue_ai(issue, state, cloud_enabled=False)
    assert issue["rule_code"] in text
    assert "原因" in text
    assert "AI 优先级" in text
    assert "建议动作" in text


def test_suggest_defect_cause_finds_nearby_route(ws_and_mrc):
    """缺陷落点附近有走线时，应把该走线列为可疑点（PRT-009 设计侧归因）。"""
    ws, mrc, state = ws_and_mrc
    route = state["routes"][0]
    at = route["pts"][0][:2]
    out = suggest_defect_cause({"type": "open_circuit", "pass_no": 4, "at": at},
                               {"routes": state["routes"]})
    assert out["suspects"], "落点在走线上应给出可疑走线"
    assert out["suspects"][0]["distance_mm"] <= 3.0


def test_suggest_defect_cause_without_nearby_route(ws_and_mrc):
    """远离所有走线时明确说明'优先怀疑工艺/设备'，不硬凑结论。"""
    ws, mrc, state = ws_and_mrc
    out = suggest_defect_cause({"type": "clog", "pass_no": 2, "at": [-500.0, -500.0]},
                               {"routes": state["routes"]})
    assert out["suspects"] == []
    assert "工艺" in out["note"]


# ================================================================ 与 Agent 集成
def test_agent_reports_ai_ranking_source(data_root):
    """设计 Agent 结果带上排序来源，便于验收时说明"用了哪一层"。"""
    from ame.agent.loop import run_designer
    ws = build_project(data_root, name="AI-Agent")
    res = run_designer(ws, "auto", 8, cloud_enabled=False)
    assert res["ai_ranking"] in ("local", "heuristic", "cloud")
    assert res["score_final"] - res["score_start"] >= 15


def test_agent_works_without_ai_package(data_root, monkeypatch):
    """删除/屏蔽 AI 层后 Agent 仍能完成检查-修改-复检（AGT-010 硬约束）。"""
    from ame.agent import loop as agent_loop

    def boom(*a, **k):
        raise ImportError("模拟 AI 层缺失")

    monkeypatch.setattr(agent_loop, "_rank_steps",
                        lambda *a, **k: (a[1], {"source": None,
                                                "error": "AI 层不可用"}))
    ws = build_project(data_root, name="无AI")
    res = agent_loop.run_designer(ws, "auto", 8, cloud_enabled=False)
    assert res["score_final"] - res["score_start"] >= 15
    assert res["applied_steps"]


# ================================================================ 训练脚本（可验证性）
def test_fit_ridge_recovers_known_linear_relation():
    """岭回归在小正则下应复原已知线性关系（训练代码本身的正确性）。

    这条测试防的是"训练脚本能跑但学错了"——权重全部为 0 或符号翻转时，
    排序准确率仍可能是 0.5，单看指标发现不了。
    """
    import numpy as np

    from ame.ai.train import fit_ridge

    rng = np.random.default_rng(7)
    X = rng.normal(size=(200, 3))
    true_w = np.array([2.0, -1.5, 0.5])
    y = X @ true_w + 3.0
    w, b = fit_ridge(X, y, alpha=1e-6)
    assert np.allclose(w, true_w, atol=1e-3), f"权重未复原：{w}"
    assert abs(b - 3.0) < 1e-3


def test_evaluate_metrics_ranges():
    """评估指标口径：完美预测 → MAE 0 / R² 1 / 排序准确率 1。"""
    import numpy as np

    from ame.ai.train import evaluate

    X = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    w = np.array([1.0, 2.0])
    b = 0.5
    y = X @ w + b
    m = evaluate(w, b, X, y)
    assert m["mae"] < 1e-9
    assert abs(m["r2"] - 1.0) < 1e-9
    assert m["pairwise_rank_accuracy"] == 1.0


def test_loso_report_flags_model_vs_heuristic():
    """留一场景交叉验证：在"模型肯定更好"的构造数据上应报出 model > heuristic。

    构造方式：让标签严格按某个特征的线性组合生成，模型能学到，
    而规则先验（只看规则代号与动作）学不到——若 LOSO 报不出这个差距，
    说明交叉验证的切分或统计写错了。
    """
    import numpy as np

    from ame.ai.train import loso_report

    rows, ys, meta = [], [], []
    for s in ("A", "B", "C", "D"):
        for k in range(6):
            feat = np.zeros(len(F.FEATURE_NAMES))
            # 关键信号放在"走线长度"这一维，与规则/动作无关
            idx = F.FEATURE_NAMES.index("path_len_norm")
            feat[idx] = (k + 1) / 6.0
            feat[F.FEATURE_NAMES.index("rule_R1")] = 1.0 if k % 2 else 0.0
            rows.append(feat)
            ys.append(float(k))            # 标签只由长度决定
            meta.append({"scenario": s, "action": "change_width",
                         "rule": "R1", "delta": float(k)})
    X, y = np.asarray(rows), np.asarray(ys)
    rep = loso_report(X, y, meta, alpha=1e-6)
    assert rep["per_scenario"], "应产出逐场景结果"
    assert rep["model"] == 1.0, f"模型应完美排序，实际 {rep['model']}"
    assert rep["model"] >= rep["heuristic"]


def test_weights_file_has_verifiable_provenance():
    """随包权重必须带可核查的训练记录（不能只有一堆数字）。

    验收时要能回答"这些权重哪来的"：样本数、场景、标签定义、LOSO 指标。
    """
    payload = json.loads(WEIGHTS_PATH.read_text("utf-8"))
    meta = payload["meta"]
    for key in ("samples", "scenarios", "labels", "loso", "by_rule", "by_action",
                "trained_at"):
        assert key in meta, f"权重缺少训练记录字段：{key}"
    assert meta["samples"] > 0
    assert "score_after" in meta["labels"], "标签定义必须写明是实测分数增量"
    assert meta["loso"]["blend"] is not None


def test_weights_are_deterministic_given_same_data():
    """同一输入两次拟合得到同一权重（训练可复现）。"""
    import numpy as np

    from ame.ai.train import fit_ridge

    rng = np.random.default_rng(11)
    X = rng.normal(size=(50, 4))
    y = X @ np.array([1.0, 2.0, 3.0, 4.0])
    w1, b1 = fit_ridge(X, y, alpha=0.5)
    w2, b2 = fit_ridge(X, y, alpha=0.5)
    assert np.array_equal(w1, w2) and b1 == b2


# ================================================================ 候选生成的可复现性
def test_candidate_count_and_order_independent_of_uuid(tmp_path):
    """同一设计在两个工程里必须得到**同样条数、同样顺序**的候选修复。

    回归背景：`mrc_state()` 的 routes 曾直接沿用 `ORDER BY r.id`，
    而 id 是每次导入重新生成的 uuid。同一份设计重建一次，行序就变，
    于是 Agent 的 `by_net`/`seen_routes` 去重会选中不同的走线，
    候选条数在 6 与 7 之间抖动（selftest 连跑两次就出现过）。
    现在 routes 按内容排序，这个测试锁住它。
    """
    from ame.agent.loop import designer_propose
    from ame.db.store import DataRoot
    from ame.ledger.workspace import create_project
    from tests.conftest import SAMPLES

    seen = []
    for i in range(2):
        root = DataRoot(tmp_path / f"c{i}")
        ws = create_project(root, f"候选可复现-{i}")
        ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                           "plate_100x60.step")
        ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
        ws.commit("导入")
        mrc = ws.run_mrc()
        state = ws.mrc_state()
        steps = designer_propose(ws, mrc, state)
        # 只比较"做了什么"，不比较 uuid
        seen.append([(s["action"], s.get("args", {}).get("net"),
                      s.get("args", {}).get("width")) for s in steps])
        ws.store.close()

    assert seen[0] == seen[1], (
        f"同一设计的候选修复不一致：\n{seen[0]}\n{seen[1]}")
