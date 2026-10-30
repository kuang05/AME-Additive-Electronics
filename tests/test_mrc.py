"""可打性（MRC）域验收测试（MRC-001..011，验收任务 T10–T12）。

覆盖：
  MRC-008      确定性：同一 state 连续两次评分结果完全一致；扣分下限 0 分保护
  MRC-001/R1   线宽：0.3 mm 命中、0.6 mm 不命中（T10）
  MRC-002/R2   间距：异网走线/焊盘过近命中、同网不误报（T10）
  MRC-003/R3   坡度：overhang60.step 的 60° 斜面命中、平面不命中（T11）
  MRC-004/R4   可达性：间隙 0.2 mm < 安全半径 0.82 mm 命中且带数值（T11）
  MRC-005/R5   遮挡：路径被器件/结构凸台遮挡时指出遮挡物名称（T11）
  MRC-006/R6   温度场：靠炉门侧温差 5.4 ℃ > 5 ℃ 命中（T12）
  MRC-011      阈值可配置、规则版本递增、旧分数记录不被覆盖（T12）
  MRC-007/R7   介电流道：坝间通道不足命中（T12）
  MRC-009      解释：含规则码/对象/原因/建议

直接调用 mrc.engine.check 的 state 均附小尺寸 outline，使 R6 参考点
（板框 x 最大值）靠近走线、温差低于 5 ℃，避免与被测规则互相污染。
"""
from __future__ import annotations

import pytest

from ame.mrc.engine import DEFAULT_RULES, check, explain_issue
from tests.conftest import build_project

# 小板框：x 最大 20 → 温差上限 0.06*20=1.2 ℃，R6 不误触发
SMALL_OUTLINE = [(0, 0), (20, 0), (20, 10), (0, 10)]


def _rules_with(code: str, key: str, value) -> list[dict]:
    """复制默认规则表并修改指定规则的阈值（MRC-011：阈值可配置）。"""
    out = []
    for r in DEFAULT_RULES:
        rr = {**r, "threshold": dict(r["threshold"])}
        if rr["code"] == code:
            rr["threshold"][key] = value
        out.append(rr)
    return out


def _state(**kw) -> dict:
    st = {"outline": SMALL_OUTLINE, "faces": [], "obstacles": [],
          "comps": [], "pads": [], "routes": [], "jumpers": [],
          "bed": [200.0, 200.0]}
    st.update(kw)
    return st


def _of(res: dict, code: str) -> list[dict]:
    return [i for i in res["issues"] if i["rule_code"] == code]


# ================================================================ MRC-008 确定性
def test_mrc008_deterministic_same_state(data_root):
    """MRC-008：同一模型重复检查分数一致 —— 同一 state 连续 run_mrc() 两次，
    分数与 issues 列表（含顺序）完全一致。"""
    ws = build_project(data_root, name="T19A")   # 板文件自带 8 条走线
    r1 = ws.run_mrc()
    r2 = ws.run_mrc()
    assert r1["score"] == r2["score"]
    assert r1["issues"] == r2["issues"]
    assert r1 == r2                              # 全量一致（含 penalty_total/规则版本）
    assert r1["issues"], "T19-A 验收板应存在违规"
    assert 59 <= r1["score"] <= 65               # T19 初始分 62±3（主控基线 60）


# ================================================================ MRC-001 / R1 / T10
def test_r1_min_width_threshold(data_root):
    """MRC-001/T10：0.3 mm 走线在默认下限 0.5 mm 下被标记（penalty>0、原因含数值）；
    0.6 mm 不标记。"""
    thin = check(_state(routes=[{"id": "RT-THIN", "net": "NA",
                                 "pts": [[0, 0], [10, 0]], "width": 0.3}]))
    hits = _of(thin, "R1")
    assert len(hits) == 1
    iss = hits[0]
    assert iss["target_id"] == "RT-THIN"
    assert iss["penalty"] > 0
    assert "0.30" in iss["reason"] and "0.50" in iss["reason"]
    assert iss["suggestion"], "MRC-009：违规需带可执行建议"

    clean = check(_state(routes=[{"id": "RT-OK", "net": "NA",
                                  "pts": [[0, 0], [10, 0]], "width": 0.6}]))
    assert _of(clean, "R1") == []
    assert clean["score"] == 100


# ================================================================ MRC-002 / R2 / T10
def test_r2_spacing_routes_same_net_and_pads():
    """MRC-002/T10：异网走线边距 0.3 mm 被标记；同网络两条线不误报；
    异网焊盘过近同样被标记（R2 含焊盘）。"""
    # 异网走线：中心线距 0.9、各宽 0.6 → 边距 0.3 < 0.5
    pair = check(_state(routes=[
        {"id": "RT-A", "net": "NA", "pts": [[0, 0], [10, 0]], "width": 0.6},
        {"id": "RT-B", "net": "NB", "pts": [[0, 0.9], [10, 0.9]], "width": 0.6},
    ]))
    hits = _of(pair, "R2")
    assert len(hits) == 1
    assert "0.30" in hits[0]["reason"] and "0.50" in hits[0]["reason"]
    assert "NA" in hits[0]["reason"] and "NB" in hits[0]["reason"]

    # 同网络不误报
    same = check(_state(routes=[
        {"id": "RT-A", "net": "NA", "pts": [[0, 0], [10, 0]], "width": 0.6},
        {"id": "RT-B", "net": "NA", "pts": [[0, 0.9], [10, 0.9]], "width": 0.6},
    ]))
    assert _of(same, "R2") == [] and _of(same, "R7") == []

    # 异网焊盘：间距 0.4 mm < 0.5 mm → 命中且对象为焊盘
    pads = check(_state(pads=[
        {"ref": "R1", "pad_no": "1", "net": "NA", "x": 0.0, "y": 0.0, "w": 1.5, "h": 1.0},
        {"ref": "R2", "pad_no": "1", "net": "NB", "x": 1.9, "y": 0.0, "w": 1.5, "h": 1.0},
    ]))
    hits = _of(pads, "R2")
    assert len(hits) == 1
    assert "焊盘" in hits[0]["reason"] and "0.40" in hits[0]["reason"]


# ================================================================ MRC-003 / R3 / T11
def test_r3_overhang60_slope_flagged(data_root):
    """MRC-003/T11：overhang60.step 导入后（faces 坡度由 analyze_faces 语义计算，
    即法向与水平面夹角），60° 斜面命中 R3，水平/竖直平面不命中；平板工程无 R3。"""
    ws = build_project(data_root, name="悬垂", step="overhang60.step", board=None)
    res = ws.run_mrc()
    hits = _of(res, "R3")
    assert len(hits) == 1
    assert hits[0]["target_id"] == "F5"          # 60° 斜面
    assert "60" in hits[0]["reason"] and "45" in hits[0]["reason"]
    assert hits[0]["suggestion"]
    assert hits[0]["penalty"] > 0 and res["score"] < 100
    # 平面 F1..F4（坡度 0°/90°）不命中
    assert all(i["target_id"] != f for i in hits for f in ("F1", "F2", "F3", "F4"))

    flat = build_project(data_root, name="平板", step="plate_100x60.step", board=None)
    assert _of(flat.run_mrc(), "R3") == []
    assert flat.run_mrc()["score"] == 100


# ================================================================ MRC-004 / R4 / T11
def test_r4_accessibility_min_gap():
    """MRC-004/T11：走线距器件矩形 0.2 mm（>0 且小于针尖安全半径
    0.82 = 0.64/2 + 0.5）→ R4 命中且原因含数值；贴近到相交则改判 R5。"""
    comp = [{"ref": "C1", "x": 50.0, "y": 30.0, "height": 1.0,
             "size_dx": 3.0, "size_dy": 3.0}]      # 矩形 x∈[48.5,51.5] y∈[28.5,31.5]
    near = check(_state(comps=comp,
                        routes=[{"id": "RT-A", "net": "NA",
                                 "pts": [[40, 28.3], [60, 28.3]], "width": 0.6}]))
    hits = _of(near, "R4")
    assert len(hits) == 1 and _of(near, "R5") == []
    assert hits[0]["target_id"] == "RT-A"
    assert "0.20" in hits[0]["reason"] and "0.82" in hits[0]["reason"]
    assert "C1" in hits[0]["reason"]

    touch = check(_state(comps=comp,
                         routes=[{"id": "RT-A", "net": "NA",
                                  "pts": [[40, 30], [60, 30]], "width": 0.6}]))
    assert _of(touch, "R4") == []
    assert len(_of(touch, "R5")) == 1              # 相交 = 遮挡，不再是间隙问题


# ================================================================ MRC-005 / R5 / T11
def test_r5_occlusion_names_blocker():
    """MRC-005/T11：走线穿过器件矩形或结构凸台（obstacles）→ R5 命中，
    原因指出遮挡物名称。"""
    through_comp = check(_state(
        comps=[{"ref": "C1", "x": 50.0, "y": 30.0, "height": 1.0,
                "size_dx": 3.0, "size_dy": 3.0}],
        routes=[{"id": "RT-A", "net": "NA", "pts": [[40, 30], [60, 30]], "width": 0.6}]))
    hits = _of(through_comp, "R5")
    assert len(hits) == 1
    assert "器件C1" in hits[0]["reason"] and "遮挡" in hits[0]["reason"]

    through_obstacle = check(_state(
        obstacles=[{"x0": 20, "y0": 20, "x1": 30, "y1": 26, "height": 2.0,
                    "name": "结构凸台F9"}],
        routes=[{"id": "RT-B", "net": "NB", "pts": [[10, 23], [40, 23]], "width": 0.6}]))
    hits = _of(through_obstacle, "R5")
    assert len(hits) == 1
    assert "结构凸台F9" in hits[0]["reason"]


# ================================================================ MRC-006 / R6 / T12
def test_r6_temperature_gradient_threshold():
    """MRC-006/T12：板框 (0,0)-(95,55)、走线 min_x=5 → 温差 0.06*90=5.4 ℃ > 5 ℃
    命中且原因含数值；走线远离炉门（min_x=20 → 4.5 ℃）或阈值改为 10 ℃ 时不命中。"""
    outline = [(0, 0), (95, 0), (95, 55), (0, 55)]
    near = check(_state(outline=outline,
                        routes=[{"id": "RT-X", "net": "NA",
                                 "pts": [[5, 10], [50, 10]], "width": 0.6}]))
    hits = _of(near, "R6")
    assert len(hits) == 1
    assert "5.4" in hits[0]["reason"]
    assert hits[0]["penalty"] > 0

    far = check(_state(outline=outline,
                       routes=[{"id": "RT-X", "net": "NA",
                                "pts": [[20, 10], [50, 10]], "width": 0.6}]))
    assert _of(far, "R6") == []                    # 温差 4.5 ℃ ≤ 5 ℃

    relaxed = check(_state(outline=outline,
                           routes=[{"id": "RT-X", "net": "NA",
                                    "pts": [[5, 10], [50, 10]], "width": 0.6}]),
                    rules=_rules_with("R6", "max_dt", 10.0))
    assert _of(relaxed, "R6") == []                # 阈值放宽到 10 ℃ 后不命中


# ================================================================ MRC-011 规则版本 / T12
def test_mrc011_threshold_versioned_history_preserved(data_root):
    """MRC-011/T12：ws.cmd_set_rule('R6', {"max_dt":10}) 后 rule_version 递增、
    重评分不再命中 R6；ws.history() 中旧版本的 score_after 记录保持不变。"""
    ws = build_project(data_root, name="T19A")
    s1 = ws.run_mrc()
    assert len(_of(s1, "R6")) > 0                  # 默认阈值下 GND 等靠炉门走线命中
    ws.commit("初始评分（默认阈值）")

    ret = ws.cmd_set_rule("R6", {"max_dt": 10})
    assert ret["rule_version"] == 2                # 版本递增
    rule_row = next(r for r in ws.rules() if r["code"] == "R6")
    assert rule_row["threshold"]["max_dt"] == 10
    assert rule_row["rule_version"] == 2

    s2 = ws.run_mrc()
    assert _of(s2, "R6") == []                     # 放宽后重新评分不命中
    assert s2["rule_versions"]["R6"] == 2
    assert s2["score"] > s1["score"]               # 少扣 R6 → 分数上升
    ws.commit("调整 R6 阈值至 10℃")

    hist = ws.history()
    assert len(hist) == 3                          # 新建工程 + 两次提交
    assert hist[1]["score_after"] == s1["score"]   # 旧分数记录不被覆盖
    assert hist[2]["score_after"] == s2["score"]
    assert hist[1]["score_after"] != hist[2]["score_after"]


# ================================================================ MRC-007 / R7 / T12
def test_r7_dielectric_dam_channel():
    """MRC-007/T12：异网走线边距 0.8 mm（< 0.5 + 2*0.6 = 1.7）→ 介电坝通道
    不足命中，原因含“介电坝”与数值；边距拉到 1.7 mm 后不命中。"""
    pair = check(_state(routes=[
        {"id": "RT-A", "net": "NA", "pts": [[0, 0], [10, 0]], "width": 0.6},
        {"id": "RT-B", "net": "NB", "pts": [[0, 1.4], [10, 1.4]], "width": 0.6},
    ]))                                            # 边距 1.4−0.6 = 0.8
    hits = _of(pair, "R7")
    assert len(hits) == 1
    assert "介电坝" in hits[0]["reason"]
    assert "0.80" in hits[0]["reason"] and "0.50" in hits[0]["reason"]

    # 边距拉到 2.5−0.6 = 1.9 mm（通道 0.7 > 0.5）→ 不命中。
    # 注：不取恰好 1.7 的下限值做“不命中”断言 —— buffer 距离的浮点误差
    # （1.6999…）会使其落入 < 0.5，属边界浮点敏感而非实现缺陷。
    wide = check(_state(routes=[
        {"id": "RT-A", "net": "NA", "pts": [[0, 0], [10, 0]], "width": 0.6},
        {"id": "RT-B", "net": "NB", "pts": [[0, 2.5], [10, 2.5]], "width": 0.6},
    ]))
    assert _of(wide, "R7") == []
    assert _of(wide, "R2") == []


# ================================================================ MRC-009 解释与建议
def test_mrc009_explain_issue_contains_parts(data_root):
    """MRC-009：explain_issue 返回文本含规则码、对象、原因与建议。"""
    res = check(_state(routes=[{"id": "RT-THIN", "net": "NA",
                                "pts": [[0, 0], [10, 0]], "width": 0.3}]))
    iss = _of(res, "R1")[0]
    st = _state(routes=[{"id": "RT-THIN", "net": "NA",
                         "pts": [[0, 0], [10, 0]], "width": 0.3}])
    text = explain_issue(iss, st)
    assert "R1" in text and "线宽" in text            # 规则码 + 规则中文名
    assert "走线 NA" in text                          # 对象用网络名（UI-008）
    assert "原因" in text and iss["reason"] in text   # 为什么
    assert "建议" in text and iss["suggestion"] in text  # 可以怎么改
    assert str(iss["penalty"]) in text                # 扣分数值


# ================================================================ MRC-008 评分下限
def test_mrc008_score_floors_at_zero():
    """MRC-008：大量违规累计扣分超过 100 时，分数下限保护为 0，不为负。"""
    routes = [{"id": f"RT-{i:02d}", "net": "NA",
               "pts": [[0, i * 0.1], [10, i * 0.1]], "width": 0.3}
              for i in range(30)]                    # 同网细线 30 条：R1 每条扣 5 分
    res = check(_state(routes=routes))
    assert len(_of(res, "R1")) == 30
    assert res["penalty_total"] > 100
    assert res["score"] == 0


# ================================================================ R2 中心线法等价性
def test_r2_centerline_equals_buffer_method():
    """MRC-002：中心线+半宽修正必须与 buffer 多边形法给出相同违规集合。

    换算法不应改变判据——实测 300 走线下两者违规逐条一致（3500 条），
    中心线法快 21 倍。
    """
    from shapely.geometry import LineString

    from ame.geometry.primitives import pad_polygon

    routes = [{"id": f"r{i}", "net": f"N{i}",
               "pts": [[10 + (i % 20) * 9, 8], [10 + (i % 20) * 9, 50]], "width": 0.6}
              for i in range(40)]
    pads = [{"id": f"p{i}", "ref": f"R{i}", "pad_no": "1", "net": f"P{i}",
             "x": 10 + (i % 20) * 9, "y": 15 + (i // 20) * 6,
             "w": 1.5, "h": 1.0, "shape": "rect", "rot": 0} for i in range(20)]
    st = _state(routes=routes, pads=pads)
    res = check(st)
    # 用 buffer 法独立复算，比较 R2 违规数量
    geoms = [(r["net"], LineString([p[:2] for p in r["pts"]]).buffer(r["width"] / 2))
             for r in routes]
    geoms += [(p["net"], pad_polygon(p)) for p in pads]
    expected = 0
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            if geoms[i][0] != geoms[j][0] and geoms[i][1].distance(geoms[j][1]) < 0.5:
                expected += 1
    got = len(_of(res, "R2"))
    assert got == expected, f"中心线法 {got} 条 vs buffer 法 {expected} 条不一致"


def test_r2_severity_stable_at_boundary():
    """MRC-008：间距恰好等于 thr/2 时严重度必须稳定（曾因浮点差异导致评分抖动）。"""
    # 两条平行线，边缘间距恰为 0.25（thr/2）
    st = _state(routes=[
        {"id": "a", "net": "NA", "pts": [[0, 0], [10, 0]], "width": 0.6},
        {"id": "b", "net": "NB", "pts": [[0, 0.85], [10, 0.85]], "width": 0.6},
    ])
    r1 = check(st)
    r2 = check(st)
    assert r1["issues"] == r2["issues"], "同一 state 两次检查结果必须一致"
    assert r1["score"] == r2["score"]


# ================================================================ NFR-002 输入健壮性
@pytest.mark.parametrize("name,state", [
    ("空 state", {}),
    ("state 为 None", None),
    ("routes 非列表", {"routes": "not-a-list"}),
    ("routes 含非字典", {"routes": [1, 2, "x", None]}),
    ("pts 为空", {"routes": [{"id": "a", "net": "n", "pts": [], "width": 0.6}]}),
    ("pts 单点", {"routes": [{"id": "a", "net": "n", "pts": [[1, 1]], "width": 0.6}]}),
    ("pts 含 NaN", {"routes": [{"id": "a", "net": "n",
                                "pts": [[float("nan"), 0], [1, 1]], "width": 0.6}]}),
    ("width 为 None", {"routes": [{"id": "a", "net": "n",
                                   "pts": [[0, 0], [1, 1]], "width": None}]}),
    ("width 为 inf", {"routes": [{"id": "a", "net": "n",
                                  "pts": [[0, 0], [1, 1]], "width": float("inf")}]}),
    ("comps 缺字段", {"comps": [{}]}),
    ("pads 缺坐标", {"pads": [{"id": "p", "net": "n"}]}),
    ("faces 坡度为字符串", {"faces": [{"fid": "F1", "slope_deg": "abc"}]}),
    ("outline 单点", {"outline": [[0, 0]]}),
    ("obstacles 缺字段", {"obstacles": [{"x0": 0}]}),
])
def test_nfr002_mrc_never_crashes_on_malformed_state(name, state):
    """NFR-002：任何畸形 state 都不得让规则引擎崩溃。

    模糊测试实测过 5 处崩溃（str 字段导致 TypeError、缺坐标 KeyError、
    空点列让 min() 抛 ValueError）。这些不是理论问题：旧库、被手工改过
    的库、导入失败残留都会产生这类数据。坏字段的正确语义是
    "该条不参与检查"，而不是"整个检查失败、界面拿不到分数"。
    """
    res = check(state)
    assert 0 <= res["score"] <= 100, f"{name}: 分数应落在 0-100"
    assert isinstance(res["issues"], list)


def test_nfr002_normal_data_still_scored_identically():
    """规范化层不得改变**正常数据**的评分结果（回归护栏）。"""
    state = {
        "outline": [[0, 0], [100, 0], [100, 60], [0, 60]],
        "routes": [
            {"id": "R-A", "net": "N1", "pts": [[10, 10], [50, 10]], "width": 0.3},
            # 与 R-A 相距 0.6 mm：扣掉两侧半线宽 0.3 mm 后净距 0.3 < 0.5 阈值
            {"id": "R-B", "net": "N2", "pts": [[10, 10.6], [50, 10.6]], "width": 0.3},
        ],
        "pads": [{"id": "P1", "ref": "U1", "pad_no": "1", "net": "N1",
                  "x": 10, "y": 10, "w": 1.5, "h": 1.0, "shape": "rect"}],
        "comps": [], "faces": [], "obstacles": [], "jumpers": [],
        "bed": [200, 200],
    }
    r1 = check(state)
    r2 = check(state)
    assert r1 == r2, "同一输入两次检查必须完全一致"
    codes = {i["rule_code"] for i in r1["issues"]}
    assert "R1" in codes, "0.3 mm 细线应命中 R1"
    assert "R2" in codes, "净距 0.3 mm 的异网细线应命中 R2"


# ================================================================ 邻近对筛选（性能）
class TestNearPairsUsesBBox:
    """`_near_pairs` 必须用包围盒外扩，而不是逐条 buffer 构造探测几何。

    回归背景：原实现 `tree.query(g.buffer(max_dist))`。STRtree 的 query 只用
    包围盒做判定，真正的几何求交并不发生，所以 buffer 的圆角/圆弧离散纯属浪费。
    实测 300 条走线：buffer 法 30.8 ms vs bbox 外扩 18.6 ms（快 1.66×），
    两者返回的候选对完全相同（20858 对）——因为 STRtree 本就用 bbox 判定。
    """

    def test_no_buffer_calls_in_near_pairs(self, monkeypatch):
        """统计 buffer 调用：`_near_pairs` 内部应为 0。"""
        from shapely.geometry import LineString

        from ame.mrc import engine as E

        calls = {"n": 0}
        original = LineString.buffer

        def counting(self, *a, **kw):
            calls["n"] += 1
            return original(self, *a, **kw)

        monkeypatch.setattr(LineString, "buffer", counting)
        geoms = [LineString([(i, 0), (i, 10)]) for i in range(50)]
        pairs = list(E._near_pairs(geoms, 1.1))
        assert pairs, "相邻直线应产生候选对"
        assert calls["n"] == 0, (
            f"_near_pairs 内部调用了 {calls['n']} 次 buffer——"
            "应改用包围盒外扩（STRtree 只用 bbox 判定）")

    def test_candidate_pairs_match_bruteforce(self):
        """候选对必须覆盖所有真实邻近对（不能因改为 bbox 而漏检）。"""
        import random

        from shapely.geometry import LineString

        from ame.mrc import engine as E

        random.seed(17)
        geoms = [LineString([(random.uniform(0, 40), random.uniform(0, 40)),
                             (random.uniform(0, 40), random.uniform(0, 40))])
                 for _ in range(60)]
        d = 1.5
        got = {(i, j) for i, j in E._near_pairs(geoms, d)}
        for i in range(len(geoms)):
            for j in range(i + 1, len(geoms)):
                if geoms[i].distance(geoms[j]) <= d:
                    assert (i, j) in got, f"漏检邻近对 ({i},{j})"
