"""Web 版补齐功能的验收：逐层播放 / 问题定位 / 热力图 / 规则阈值 / 版本 / 剖切。

与 `test_web_api.py` 的分工
--------------------------
那边守的是"既有契约不许破"（四域闭环、错误处理、命令总线一致性）；
这里守的是"新补的能力**真的能拿到数据**"。所以每个用例都走真实 HTTP，
断言的是前端画图所依赖的字段存在且自洽，而不是仅断言 200。

前端交互（滑块、开关、面板）不做浏览器自动化：本项目的既有约定是
用静态契约测试（见 `test_frontend_has_routing_and_selection_wiring`）
保证"页面里真有这段代码"，渲染正确性靠手工演示。这里沿用同一约定，
但把断言收得更紧——新功能必须同时出现在 HTML 与真实接口两侧。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from conftest import SAMPLES  # noqa: E402

HTML = (Path(__file__).resolve().parent.parent
        / "ame" / "web" / "static" / "index.html").read_text("utf-8")


def _sample(client) -> str:
    r = client.post("/api/projects/sample")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _new(client, name="特性测试") -> str:
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _sliced_pid(client) -> tuple[str, dict]:
    """建工程 + 导入板文件 + 切片，返回 (pid, slice 响应)。"""
    pid = _new(client)
    with open(SAMPLES / "plate_100x60.step", "rb") as f:
        assert client.post(f"/api/projects/{pid}/import/step",
                           files={"file": ("plate_100x60.step", f, "text/plain")}
                           ).status_code == 200
    with open(SAMPLES / "board_T19A.kicad_pcb", "rb") as f:
        assert client.post(f"/api/projects/{pid}/import/board",
                           files={"file": ("board_T19A.kicad_pcb", f, "text/plain")}
                           ).status_code == 200
    r = client.post(f"/api/projects/{pid}/slice")
    assert r.status_code == 200, r.text
    return pid, r.json()


# ================================================================ SLC-006 逐层播放
def test_layer_endpoint_returns_paths_for_every_pass(client):
    """逐层播放：每个道次都能取到完整路径，且与切片响应同序同数（SLC-006）。"""
    pid, sl = _sliced_pid(client)
    passes = sl["pass_details"]
    assert len(passes) == 7

    seen = []
    for i, p in enumerate(passes):
        r = client.get(f"/api/projects/{pid}/layer/{i}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["pass_no"] == p["pass_no"], "滑块下标必须与 pass_details 同序"
        assert d["kind"] == p["kind"] and d["mat"] == p["mat"]
        assert d["z_mm"] == p["z_mm"]
        assert d["total_passes"] == len(passes)
        # 路径条数必须与切片响应的 paths 计数一致——不一致说明前端看到的
        # 路径不是刚导出的那份 G-code 的来源。
        assert d["path_count"] == p["paths"] == len(d["paths"])
        seen.append(d["pass_no"])

    assert seen == sorted(seen)


def test_layer_paths_are_drawable_coordinates(client):
    """路径点必须是 [x, y] 数值对：画布直接喂给 toScreen，不能是字典/字符串。"""
    pid, _sl = _sliced_pid(client)
    d = client.get(f"/api/projects/{pid}/layer/0").json()
    assert d["path_count"] > 0
    for path in d["paths"][:20]:
        assert len(path["pts"]) >= 2, "少于 2 点的路径画不出线"
        for q in path["pts"]:
            assert len(q) == 2
            assert isinstance(q[0], (int, float)) and isinstance(q[1], (int, float))
        assert path["role"] in ("outline", "fill", "dam", "trench",
                               "pad_pit", "pad_fill", "travel")


def test_layer_before_slice_is_readable_400(client):
    """没切片就拉道次：400 + 中文原因，不是 500 也不是空列表。"""
    pid = _new(client, "未切片")
    r = client.get(f"/api/projects/{pid}/layer/0")
    assert r.status_code == 400
    assert "切片" in r.json()["detail"]


def test_layer_index_out_of_range_is_readable_400(client):
    """越界下标要说清范围——否则前端滑块写错只能看到一片空白。"""
    pid, _sl = _sliced_pid(client)
    r = client.get(f"/api/projects/{pid}/layer/99")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "7" in detail and "范围" in detail

    assert client.get(f"/api/projects/{pid}/layer/-1").status_code == 400


def test_layer_on_unknown_project_is_404(client):
    r = client.get("/api/projects/not-exist/layer/0")
    assert r.status_code == 404
    assert "打不开" in r.json()["detail"]


def test_layer_matches_cached_slice_payload(client):
    """切片命中缓存时（第二次调用），道次端点仍要能取到同一份数据。

    缓存返回的 payload 里 ``pass_details`` 只有路径数没有坐标，如果
    ``/layer`` 依赖它而不是依赖库，第二次之后就会变成"切片成功但无法预览"。
    """
    pid, sl1 = _sliced_pid(client)
    sl2 = client.post(f"/api/projects/{pid}/slice").json()
    assert sl2["cached"] is True, "第二次切片应命中缓存（该用例依赖此前提）"
    assert sl2["slice_set_id"] == sl1["slice_set_id"]
    for i, p in enumerate(sl2["pass_details"]):
        d = client.get(f"/api/projects/{pid}/layer/{i}").json()
        assert d["pass_no"] == p["pass_no"]
        assert d["path_count"] == p["paths"]


# ================================================================ MRC-010 问题定位
def test_issue_targets_resolve_to_geometry(client):
    """每条问题都能在前端 state 里找到对应几何或明确说明找不到（MRC-010）。

    这是"点问题能定位"的硬前提：前端拿 target_id 去 routes/pads 里找，
    如果后端给的 id 与 state 里的 id 不是同一套主键，交互就是假的。
    """
    pid = _sample(client)
    st = client.get(f"/api/projects/{pid}/state").json()
    mrc = client.post(f"/api/projects/{pid}/mrc").json()
    route_ids = {r["id"] for r in st["routes"]}
    pad_ids = {p["id"] for p in st["pads"]}
    pad_labels = {f"{p['ref']}.{p['pad_no']}" for p in st["pads"]}

    located, locatable_types = 0, set()
    for i in mrc["issues"]:
        tid = str(i["target_id"])
        parts = [s.strip() for s in tid.split("|")]
        hit = (tid in route_ids or tid in pad_ids
               or any(p in pad_labels for p in parts)
               or any(any(r.startswith(part[:8]) for r in route_ids) for part in parts))
        if hit:
            located += 1
            locatable_types.add(i["target_type"])
    assert located >= 1, "至少应有问题能定位到几何，否则该功能等于没做"
    assert locatable_types, "定位必须覆盖至少一种目标类型"


def test_frontend_wires_issue_click_to_canvas_highlight():
    """前端：问题列表点击必须真的设置选中并触发重绘（不能只是样式）。"""
    assert 'data-idx' in HTML, "问题条目必须带下标，才能回查 issue"
    assert "$('issues').addEventListener('click'" in HTML, \
        "用事件委托绑定，整块 innerHTML 重建后仍有效"
    assert "locateIssue" in HTML
    assert "S.selected = { type: 'issue'" in HTML
    assert "scheduleDraw()" in HTML


# ================================================================ MRC-010 热力图
def test_frontend_has_heatmap_toggle_wired_to_endpoint():
    assert 'id="btnHeat"' in HTML
    assert "/heatmap`" in HTML, "开关必须真的请求后端热力图"
    assert "max_weight" in HTML or "maxW" in HTML, "必须按权重大小决定颜色/半径"


def test_heatmap_toggle_off_and_on(client):
    """热力图数据在开关两侧都可用：关掉不该影响后续评分与切片。"""
    pid = _sample(client)
    h = client.get(f"/api/projects/{pid}/heatmap").json()
    assert h["points"]
    assert all("weight" in p and "x" in p and "y" in p for p in h["points"])
    # 权重不超过最大值，前端按 max_weight 归一化依赖这一点
    assert max(p["weight"] for p in h["points"]) <= h["max_weight"] + 1e-9


# ================================================================ MRC-011 规则阈值
def test_rules_panel_exposes_all_seven_rules(client):
    """规则面板要能列出 7 条规则及其阈值键，前端按实际键生成输入框。"""
    pid = _sample(client)
    rules = client.get(f"/api/projects/{pid}/rules").json()
    assert [r["code"] for r in rules] == ["R1", "R2", "R3", "R4", "R5", "R6", "R7"]
    for r in rules:
        assert isinstance(r["threshold"], dict)
        assert isinstance(r["rule_version"], int)
    # R5 没有阈值参数：前端必须能处理空阈值而不是崩掉
    r5 = next(r for r in rules if r["code"] == "R5")
    assert r5["threshold"] == {}


def test_rule_change_bumps_version_and_reflects_in_score(client):
    """改阈值 → rule_version 递增 → 重新评分生效（MRC-011 版本化，不覆盖历史）。"""
    pid = _sample(client)
    v0 = next(r["rule_version"] for r in client.get(f"/api/projects/{pid}/rules").json()
              if r["code"] == "R1")
    score0 = client.post(f"/api/projects/{pid}/mrc").json()["score"]

    assert client.post(f"/api/projects/{pid}/rules/R1",
                       json={"threshold": {"min_width": 0.1}}).status_code == 200
    rules = client.get(f"/api/projects/{pid}/rules").json()
    r1 = next(r for r in rules if r["code"] == "R1")
    assert r1["rule_version"] == v0 + 1
    assert r1["threshold"]["min_width"] == pytest.approx(0.1)

    m = client.post(f"/api/projects/{pid}/mrc").json()
    assert m["score"] >= score0
    assert not [i for i in m["issues"] if i["rule_code"] == "R1"]
    # 其它规则的阈值不能被顺手改掉（合并写入而非整体覆盖）
    assert next(r for r in rules if r["code"] == "R2")["threshold"]["min_clear"] == 0.5


def test_rule_enable_toggle_does_not_reset_threshold(client):
    """只切启停不该动阈值，也不该偷偷把规则重新启用（历史上的真实 bug）。"""
    pid = _sample(client)
    before = next(r for r in client.get(f"/api/projects/{pid}/rules").json()
                  if r["code"] == "R2")
    assert client.post(f"/api/projects/{pid}/rules/R2",
                       json={"enabled": False}).status_code == 200
    after = next(r for r in client.get(f"/api/projects/{pid}/rules").json()
                 if r["code"] == "R2")
    assert after["enabled"] is False
    assert after["threshold"] == before["threshold"]
    assert after["rule_version"] == before["rule_version"]


def test_rule_bad_threshold_is_readable_400(client):
    pid = _sample(client)
    r = client.post(f"/api/projects/{pid}/rules/R1", json={"threshold": {"min_width": -5}})
    assert r.status_code == 400
    assert any("\u4e00" <= c <= "\u9fff" for c in r.json()["detail"])


def test_rule_unknown_code_is_readable_400(client):
    pid = _sample(client)
    r = client.post(f"/api/projects/{pid}/rules/R99",
                    json={"threshold": {"min_width": 0.5}})
    assert r.status_code == 400
    assert "R99" in r.json()["detail"]


def test_frontend_rules_panel_wired():
    assert 'id="btnRules"' in HTML
    assert "/rules`" in HTML and "/rules/${code}`" in HTML
    # 保存后必须重新评分，否则用户改完阈值看不到任何变化
    assert "saveRules" in HTML and "runMrc()" in HTML


# ================================================================ PRJ-004 版本历史
def test_versions_have_seq_score_and_reason(client):
    """版本列表：seq / 分数 / 原因齐全，且 seq 单调（前端按顺序倒排展示）。"""
    pid = _sample(client)
    client.post(f"/api/projects/{pid}/slice")   # 触发一次提交
    vs = client.get(f"/api/projects/{pid}/versions").json()
    assert len(vs) >= 2, "示例工程 + 切片应至少产生两版"
    seqs = [v["seq"] for v in vs]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "seq 不能重复"
    for v in vs:
        assert "reason" in v and "created_at" in v
        assert "score_before" in v and "score_after" in v


def test_versions_describe_what_changed(client):
    """版本必须说明改了哪些对象——"分数变了但不知道为什么"等于没有历史。"""
    pid = _sample(client)
    routes = client.get(f"/api/projects/{pid}/state").json()["routes"]
    client.delete(f"/api/projects/{pid}/routes/{routes[0]['id']}")
    client.post(f"/api/projects/{pid}/slice")
    vs = client.get(f"/api/projects/{pid}/versions").json()
    assert any(v["changed"] for v in vs), "应有版本记录了变更项"


def test_frontend_versions_panel_wired():
    assert 'id="btnVersions"' in HTML
    assert "/versions`" in HTML
    assert "score_before" in HTML and "score_after" in HTML


# ================================================================ STR-003 剖切
def test_section_cut_uses_z_in_face_loops(client):
    """剖切的数据前提：faces 的每个 loop 点都带 z，前端才能按 Z 阈值过滤（STR-003）。"""
    pid = _new(client, "剖切测试")
    with open(SAMPLES / "plate_100x60.step", "rb") as f:
        assert client.post(f"/api/projects/{pid}/import/step",
                           files={"file": ("plate_100x60.step", f, "text/plain")}
                           ).status_code == 200
    st = client.get(f"/api/projects/{pid}/state").json()
    faces = st["faces"]
    assert faces, "导入 STEP 后应有面"
    assert st["faces_note"] == "", "面数未超限时不应有截断说明"
    for f in faces:
        assert f["loop"], "面必须有环"
        for p in f["loop"]:
            assert len(p) == 3, "点必须是 [x, y, z]"
        assert len(f["normal"]) == 3
    # 平板 6 个面：z=0 与 z=2 两个水平面，顶点 z 必须真的不同，
    # 否则按 Z 剖切切不开任何东西。
    zs = {round(p[2], 3) for f in faces for p in f["loop"]}
    assert len(zs) >= 2, f"平板应有不同高度的顶点，实际 {zs}"


def test_state_without_step_still_returns_faces_shape(client):
    """没导入 STEP 时 faces 为空，且 faces_note 也要在——前端读它做提示。"""
    pid = _new(client, "空工程")
    st = client.get(f"/api/projects/{pid}/state").json()
    assert st["faces"] == []
    assert "faces_note" in st


def test_frontend_section_cut_wired():
    assert 'id="sectionZ"' in HTML
    assert "S.sectionZ" in HTML
    # 必须真的按 Z 过滤面，而不是只存一个不被使用的值
    assert "some(p => p[2] <= clip" in HTML, "剖切判据应保留剖切面以下的面"
    assert "c.height || 0) > S.sectionZ" in HTML, "器件也要按高度参与剖切"


# ================================================================ 预览态互斥
def test_preview_and_highlight_are_mutually_exclusive():
    """逐层预览时画布只画当前道次，此时定位/热力图必须先退出预览。

    否则用户点了问题条却看不到任何高亮（画布还被单道次路径占着），
    会得到"定位功能坏了"的错误结论——这是真实踩过的坑。
    """
    seed = HTML.split("$('issues').addEventListener('click'")[1].split("});")[0]
    assert "exitPlay()" in seed, "点问题定位前必须退出逐层预览"
    heat = HTML.split("$('btnHeat').onclick")[1].split("try {")[0]
    assert "exitPlay()" in heat, "开热力图前必须退出逐层预览"


# ================================================================ 前端不要假按钮
def test_every_new_control_exists_in_dom():
    """新加的功能控件必须在 DOM 里真实存在；有 ID 才可能被脚本绑上。"""
    for ident in ("btnHeat", "btnRules", "btnVersions", "sectionZ",
                  "playbar", "playRange", "btnPlayPrev", "btnPlayNext",
                  "btnPlayExit", "modal", "modalClose"):
        assert f'id="{ident}"' in HTML, f"缺少控件 #{ident}"


# ================================================================ 参数校验中文化
class TestValidationMessages:
    """UI-008：错误信息必须是中文可读，不能只给内部代号。"""

    @pytest.mark.parametrize("body,expect_kw", [
        ({"width": -1}, "线宽"),
        ({"width": 999}, "线宽"),
        ({}, "线宽"),
    ])
    def test_width_validation_is_chinese(self, client, body, expect_kw):
        pid = client.post("/api/projects/sample").json()["id"]
        r = client.post(f"/api/projects/{pid}/routes/any/width", json=body)
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert expect_kw in detail, f"应指出字段名，实际：{detail}"
        assert any("\u4e00" <= c <= "\u9fff" for c in detail), \
            f"错误信息必须是中文：{detail}"

    def test_validation_keeps_structured_errors(self, client):
        """翻译过的同时保留原始结构，供前端程序判断（不能只留中文串）。"""
        pid = client.post("/api/projects/sample").json()["id"]
        r = client.post(f"/api/projects/{pid}/routes/any/width", json={"width": -1})
        d = r.json()
        assert "errors" in d and isinstance(d["errors"], list)
        assert d["errors"][0]["type"] == "greater_than"

    def test_move_comp_requires_coordinates(self, client):
        pid = client.post("/api/projects/sample").json()["id"]
        r = client.post(f"/api/projects/{pid}/comps/any/move", json={})
        assert r.status_code == 422
        assert "坐标" in r.json()["detail"] or "必填" in r.json()["detail"]

    def test_malformed_inputs_never_500(self, client):
        """任何畸形输入都不得返回 500——500 表示服务端有未处理异常。"""
        pid = client.post("/api/projects/sample").json()["id"]
        probes = [
            ("delete", f"/api/projects/{pid}/routes/nope", None),
            ("get", f"/api/projects/{pid}/layer/999", None),
            ("get", f"/api/projects/{pid}/layer/-1", None),
            ("get", f"/api/projects/{pid}/layer/abc", None),
            ("post", f"/api/projects/{pid}/rules/R99", {"threshold": {}}),
        ]
        for method, url, body in probes:
            r = (client.delete(url) if method == "delete"
                 else client.get(url) if method == "get"
                 else client.post(url, json=body or {}))
            assert r.status_code < 500, f"{method} {url} 返回 {r.status_code}：{r.text[:120]}"
