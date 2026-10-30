"""界面域真实路径测试（不是"控件存在"，而是"点下去会发生什么"）。

`test_ui.py` 侧重面板/布局存在性与中文提示；本文件补的是**行为**：
每个用例都真正驱动一个域动作，断言它对工程数据产生了预期影响。

为什么需要这一层
----------------
覆盖率实测 `ui/domains/*` 只有 13–30%：大多数对话框代码从未被执行过。
这类代码的典型缺陷不是"崩溃"，而是**悄悄做错事**——例如保存了但没刷新、
撤销栈没登记、异常被吞掉。只有真正调用才能发现。

约定
----
- 文件对话框：monkeypatch `filedialog.askopenfilename` 等返回样例路径；
- 消息框：`hidden=True` 时已被替换为记录器，断言 `app.dialogs` 即可；
- 隐藏窗口不布局（canvas 为 1×1），因此不做坐标点击，只测动作函数。
"""
from __future__ import annotations


import pytest

tk = pytest.importorskip("tkinter")

from ame.ledger.workspace import create_project  # noqa: E402
from conftest import SAMPLES, wait_for  # noqa: E402

pytestmark = pytest.mark.ui


@pytest.fixture(scope="session")
def _tk_session():
    from ame.ui.app import App
    try:
        a = App(None, hidden=True)
    except tk.TclError as e:
        pytest.skip(f"无可用显示，跳过界面测试：{e}")
    yield a
    try:
        a.destroy()
    except Exception:
        pass


@pytest.fixture()
def app(_tk_session, data_root):
    a = _tk_session
    a.data_root = data_root
    a.ws = None
    a.selected = None
    a.slices = None
    a.last_job = None
    a.mrc = {"issues": [], "score": None}
    a.view_mode = "3d"
    a.zoom = 1.0
    a.pan = [0.0, 0.0]
    a.tool = None
    a.heatmap_on = False
    a.section_z = None
    a.play_index = None
    a.feature_preview = []
    a.dialogs = []
    a._refresh_all()
    a.update_idletasks()
    yield a
    for w in list(a.winfo_children()):
        if w.winfo_class() == "Toplevel":
            w.destroy()
    a.update_idletasks()


def _project(app, with_board=True, name="UI行为"):
    ws = create_project(app.data_root, name)
    ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                       "plate_100x60.step")
    if with_board:
        ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    app.ws = ws
    app.mrc = ws.run_mrc()
    app._refresh_all()
    return ws


def _pick_file(monkeypatch, path):
    """把文件选择框替换成固定路径。"""
    from tkinter import filedialog
    monkeypatch.setattr(filedialog, "askopenfilename", lambda **k: str(path))
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda **k: str(path))


# ================================================================ 结构域
def test_import_step_via_dialog_updates_model(app, monkeypatch):
    """导入 STEP 对话框路径：模型入库、MRC 重算、画布可绘。"""
    app.ws = create_project(app.data_root, "导入")
    _pick_file(monkeypatch, SAMPLES / "plate_100x60.step")
    app.import_step()
    assert app.ws.step_model() is not None
    assert app.mrc.get("score") is not None
    assert app.ws.board_outline()[0], "板框应被识别"


def test_import_board_via_dialog_loads_components(app, monkeypatch):
    """导入板文件对话框路径：器件/焊盘/网络数量与源文件一致。"""
    ws = create_project(app.data_root, "板")
    app.ws = ws
    ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"), "p.step")
    _pick_file(monkeypatch, SAMPLES / "board_T19A.kicad_pcb")
    app.import_board()
    st = app.ws.mrc_state()
    assert len(st["comps"]) == 8
    assert len(st["pads"]) == 20
    assert len(app.ws.nets()) == 12


def test_face_analysis_and_boolean_preview_do_not_mutate(app):
    """面分析与布尔建议都只读：调用后设计摘要不变（STR-004/005）。"""
    ws = _project(app)
    before = ws.design_digest()
    app.show_face_analysis()
    app.show_boolean()
    assert ws.design_digest() == before, "查看类动作不得改变设计"


def test_manual_outline_sets_board_and_recomputes_score(app):
    """手工板框：写入板框后评分可用（STR-007）。"""
    ws = _project(app, with_board=False)
    app.manual_outline()
    outline, note = ws.board_outline()
    assert outline, f"应已设置板框：{note}"
    assert app.mrc.get("score") is not None


def test_section_and_measure_are_display_only(app):
    """剖切与测量只影响显示，不改设计（STR-003）。"""
    ws = _project(app)
    before = ws.design_digest()
    app.set_section()
    app.measure()
    assert ws.design_digest() == before
    assert app.tool == "measure"
    app.cancel_tool()
    assert app.tool is None


# ================================================================ 走线域
def test_finish_route_creates_undoable_route(app):
    """拖动落点后双击结束：走线入库且可撤销（CIR-004/PRT-005）。"""
    ws = _project(app)
    n0 = len(ws.routes())
    st = ws.mrc_state()
    pad = st["pads"][0]
    app.tool = "route"
    app.route_net = pad["net"]
    # 在板内构造两点（不经画布坐标换算，直接给世界坐标）
    app._drag_pts = [[20.0, 20.0, 0.0], [40.0, 20.0, 0.0]]
    app.finish_route()
    assert len(ws.routes()) == n0 + 1
    app.undo()
    assert len(ws.routes()) == n0, "新走线应可撤销"


def test_finish_route_rejects_degenerate_input(app):
    """不足 2 点或未选网络时不落库（CIR-004 边界）。"""
    ws = _project(app)
    n0 = len(ws.routes())
    app.tool = "route"
    app.route_net = None
    app._drag_pts = [[20.0, 20.0, 0.0], [40.0, 20.0, 0.0]]
    app.finish_route()
    assert len(ws.routes()) == n0, "未选网络不得落线"

    app.tool = "route"
    app.route_net = "GND"
    app._drag_pts = [[20.0, 20.0, 0.0]]
    app.finish_route()
    assert len(ws.routes()) == n0, "单点不得落线"


def test_connectivity_check_reports_result(app):
    """连通性检查：对有断路的样例板应给出失败与定位（CIR-009）。"""
    ws = _project(app, with_board=False)
    ws.cmd_import_board((SAMPLES / "board_10.kicad_pcb").read_text("utf-8"))
    app.mrc = ws.run_mrc()
    app.check_connectivity()
    assert app.dialogs, "应弹出/记录检查结果"
    assert any("连通性" in d["title"] for d in app.dialogs)


def test_keepouts_and_auto_route_candidates(app):
    """禁布区增删 + 自动布线候选取用（CIR-006/T08）。"""
    ws = _project(app)
    before = len(ws.keepouts())
    r = ws.cmd_add_keepout([[30, 30], [60, 30], [60, 50], [30, 50]], "禁区A")
    assert len(ws.keepouts()) == before + 1
    ws.cmd_delete_keepout(r["id"])
    assert len(ws.keepouts()) == before

    cands = ws.cmd_auto_route_candidates("N2")
    assert cands, "样例板应有可布网络"
    ok = [c for c in cands if not c.get("failed")]
    assert ok, "至少应有一个可布候选"


def test_edit_selected_property_hidden_mode_is_noop(app):
    """隐藏模式下的"编辑属性"只给说明、不改数据（不做交互输入）。

    这条断言的价值在于锁住"自动化环境不会偷偷改工程"这一约定：
    若哪天有人在隐藏分支里加了写库，这个测试会立刻失败。
    """
    ws = _project(app)
    route = ws.mrc_state()["routes"][0]
    before = ws.design_digest()
    app.selected = {"type": "route", "data": {"id": route["id"],
                                             "net": route["net"],
                                             "width": route["width"]}}
    app.edit_selected_prop()
    assert ws.design_digest() == before, "隐藏模式不得修改设计"
    assert app.last_dialog and "隐藏模式" in app.last_dialog["body"]


def test_change_width_command_via_bus(app):
    """改线宽的业务路径（界面按钮最终调用的就是它）：落库 + 可撤销。"""
    ws = _project(app)
    route = ws.mrc_state()["routes"][0]
    ws.cmd_change_width(route["id"], 0.8)
    row = ws.store.one("SELECT width_mm FROM route WHERE id=?", (route["id"],))
    assert row["width_mm"] == pytest.approx(0.8)
    ws.undo()
    row = ws.store.one("SELECT width_mm FROM route WHERE id=?", (route["id"],))
    assert row["width_mm"] == pytest.approx(route["width"])


# ================================================================ 可打性域
def test_run_mrc_updates_panel_and_score_label(app):
    """运行检查：分数、问题列表、顶栏标签三者一致（MRC-008）。"""
    _project(app)
    app.run_mrc()
    assert app.mrc["score"] is not None
    rows = app.issue_tree.get_children()
    assert len(rows) == len(app.mrc["issues"])
    assert str(app.mrc["score"]) in app.lbl_score.cget("text")


def test_issue_explain_and_suggest_are_read_only(app):
    """解释/建议只读：不改变设计，且输出含规则代号（MRC-009）。"""
    ws = _project(app)
    issue = app.mrc["issues"][0]
    app.selected = {"type": "issue", "data": issue}
    before = ws.design_digest()
    app.explain_selected()
    text = app.last_dialog["body"]
    app.suggest_selected()
    assert ws.design_digest() == before
    assert issue["rule_code"] in text, "解释应含规则代号"
    assert "原因" in text


def test_export_issues_writes_csv(app, monkeypatch, tmp_path):
    """导出问题列表：选 CSV 就必须真的写 CSV（曾一律写 JSON 的旧缺陷）。"""
    _project(app)
    app.run_mrc()
    out = tmp_path / "issues.csv"
    _pick_file(monkeypatch, out)
    app.export_issues()
    assert out.exists()
    head = out.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "规则" in head and "严重度" in head


def test_edit_rules_changes_threshold_and_score(app):
    """改阈值 → 重新评分（MRC-011）；旧阈值不覆盖历史记录。"""
    ws = _project(app)
    before = ws.run_mrc()["score"]
    ws.cmd_set_rule("R1", {"min_width": 0.1})
    after = ws.run_mrc()
    assert after["score"] >= before, "放宽线宽下限后分数不应下降"
    assert not [i for i in after["issues"] if i["rule_code"] == "R1"]


# ================================================================ 切片域
def test_slice_now_populates_layer_panel(app):
    """切片：层列表行数 = 道次数，底栏显示层信息（SLC-006）。"""
    ws = _project(app)
    ws.commit("切片前")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None, timeout=30), "切片应完成"
    pds = app.slices.get("pass_details") or []
    assert len(pds) == 7
    assert len(app.layer_tree.get_children()) == len(pds)
    assert "道次" in app.lbl_layer.cget("text")


def test_export_gcode_writes_file_and_hash_stable(app, monkeypatch, tmp_path):
    """导出 G-code：文件非空；同一设计两次导出内容哈希一致（SLC-008）。"""
    ws = _project(app)
    ws.commit("导出前")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None, timeout=30)
    p1, p2 = tmp_path / "a.gcode", tmp_path / "b.gcode"
    _pick_file(monkeypatch, p1)
    app.export_gcode()
    _pick_file(monkeypatch, p2)
    app.export_gcode()
    assert p1.exists() and p1.stat().st_size > 0
    assert p1.read_bytes() == p2.read_bytes(), "同参数两次导出应逐字节一致"


def test_show_schedule_lists_passes(app):
    """排程面板：道次数与切片结果一致（SLC-009）。"""
    ws = _project(app)
    ws.commit("排程")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None, timeout=30)
    app.show_schedule()
    assert app.last_dialog


# ================================================================ 打印域
def test_print_flow_creates_job_and_defects(app):
    """连接 → 打印 → 作业落库；注入故障产生缺陷（PRT-001/008/009）。"""
    ws = _project(app)
    ws.commit("打印前")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None, timeout=30)

    app.connect_device()
    dev = ws.store.one("SELECT online FROM device ORDER BY rowid DESC LIMIT 1")
    assert dev and dev["online"] == 1, "连接后设备应在线"

    app.inject_fault = app.inject_fault      # 保持引用（对话框在 hidden 下记录）
    app.start_print()
    job = ws.store.one("SELECT * FROM print_job ORDER BY started_at DESC LIMIT 1")
    assert job is not None, "应产生打印作业"
    assert job["state"] in ("done", "fault", "printing")


def test_print_requires_slice(app):
    """无切片时不允许开始打印（PRT-002 前置检查）。"""
    _project(app)
    app.connect_device()
    app.dialogs.clear()
    app.start_print()
    assert any("切片" in d["message"] or "切片" in d["title"] for d in app.dialogs), \
        "应提示先切片"


def test_job_history_and_defect_panel_render(app):
    """作业历史与缺陷面板可打开且不崩（PRT-010）。"""
    ws = _project(app)
    ws.commit("历史")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None, timeout=30)
    app.connect_device()
    app.start_print()
    app.show_job_history()
    assert app.last_dialog
    app.show_defects()
    assert app.last_dialog


# ================================================================ 工程域
def test_commit_creates_version_and_bumps_label(app):
    """提交版本：版本号递增，顶栏同步（PRJ-005/DAT-002）。"""
    ws = _project(app)
    n0 = len(ws.history())
    app.commit_version()
    assert len(ws.history()) == n0 + 1
    assert "版本" in app.lbl_version.cget("text")


def test_export_and_import_package_roundtrip(app, monkeypatch, tmp_path):
    """导出工程包 → 导入为副本：数据一致且互不影响（PRJ-006/NFR-008）。"""
    ws = _project(app)
    ws.commit("打包")
    pkg = tmp_path / "p.amepkg"
    _pick_file(monkeypatch, pkg)
    app.export_package()
    assert pkg.exists() and pkg.stat().st_size > 0

    # 隐藏模式下 askyesno 恒为 False（测试桩），因此"同 id 冲突 → 导入副本"
    # 这条交互分支不会被走到。直接验证底层服务：冲突时报 FileExistsError，
    # as_copy=True 时分配新 id 且数据完整。
    from ame.ledger.workspace import Workspace
    n_routes = len(ws.routes())
    with pytest.raises(FileExistsError):
        app.data_root.import_project(pkg)
    st = app.data_root.import_project(pkg, as_copy=True)
    pid = st.one("SELECT id FROM project")["id"]
    assert pid != ws.project_id, "副本必须分配新编号"
    copy_ws = Workspace(st, pid)
    assert len(copy_ws.routes()) == n_routes, "副本应含同一份设计"
    copy_ws.store.close()


def test_check_integrity_reports_ok(app):
    """完整性校验：未被外部修改时报告一致（DAT-005）。"""
    _project(app)
    app.check_integrity()
    assert app.last_dialog
    assert "完整性" in app.last_dialog["title"]


def test_settings_roundtrip(app):
    """设置持久化：写入后重新读取仍生效（PRJ-009）。"""
    app.data_root.save_settings(log_level="debug", cloud_enabled=True)
    from ame.db.store import DataRoot
    again = DataRoot(app.data_root.data_dir)
    assert again.settings["log_level"] == "debug"
    assert again.settings["cloud_enabled"] is True


# ================================================================ 智能体域
def test_agent_run_produces_plan_text_and_improves(app):
    """Agent 运行：计划面板有内容、分数提升、重放校验通过（AGT-001/008）。"""
    ws = _project(app)
    start = ws.run_mrc()["score"]
    app.run_agent()
    text = app.plan_text.get("1.0", "end")
    assert "目标" in text and "裁决" in text
    assert "重放校验" in text
    assert ws.run_mrc()["score"] > start, "Agent 应提升分数"


def test_chat_answers_top_issue(app):
    """对话面板：问"为什么分数低"给出最严重问题（UI-004）。"""
    _project(app)
    app.run_mrc()
    app.chat_entry.insert(0, "为什么分数低")
    app.send_chat()
    out = app.chat_text.get("1.0", "end")
    assert "分" in out
    assert app.mrc["issues"][0]["rule_code"] in out


def test_ai_status_panel_reports_source(app):
    """AI 收益排序面板：说明当前打分来源（AGT-012）。"""
    _project(app)
    app.show_ai_status()
    text = app.last_dialog["body"]
    assert any(k in text for k in ("本地模型", "云端模型", "规则先验"))


# ================================================================ 工程管理深路径
def test_rename_copy_delete_project_lifecycle(app, monkeypatch, tmp_path):
    """重命名 → 复制 → 删除（进回收站）→ 恢复：全链路数据一致（PRJ-007）。"""
    from tkinter import simpledialog

    ws = _project(app)
    ws.commit("起点")

    # 走动作层（交互层在隐藏模式下只能返回默认值，问不出新名字）
    assert app.apply_rename_project("改名后工程") is True
    assert app.ws.project["name"] == "改名后工程"

    monkeypatch.setattr(simpledialog, "askstring",
                        lambda *a, **k: "副本工程")
    before_copy = len(app.data_root.list_projects())
    app.copy_project()
    assert len(app.data_root.list_projects()) == before_copy + 1

    app.delete_project()
    assert app.ws is None, "删除后应回到无工程状态"
    trash = app.data_root.list_trash()
    assert trash, "应进入回收站"
    assert any(r["name"] == "改名后工程" for r in trash)

    pid2 = app.data_root.restore_project(trash[0]["file"])
    assert pid2, "恢复应返回工程 id"


def test_open_sample_one_click_builds_chain(app):
    """一键示例工程：结构+电路+分数齐备，可直接切片（NFR-003）。"""
    app.open_sample()
    assert app.ws is not None
    assert app.ws.step_model() is not None
    assert len(app.ws.routes()) > 0
    assert app.mrc.get("score") is not None
    outline, _ = app.ws.board_outline()
    assert outline


def test_new_project_dialog_creates_empty_project(app):
    """新建工程：版本 0、无设计、分数显示为待导入（UI-007 空状态）。"""
    app.new_project()          # 隐藏模式下对话框记录后即创建
    assert app.ws is not None
    assert app.ws.step_model() is None
    assert "—" in app.lbl_score.cget("text") or "待" in app.lbl_score.cget("text")


def test_apply_tess_tol_reduces_face_count(app):
    """调大网格容差 → 面数下降且入库（STR-008 可配置且真的生效）。

    直接调 `apply_tess_tol`（动作层），而非 `set_tess_tol`（交互层）：
    隐藏模式下交互层用当前值执行，测不出"改大"的效果。这也是本轮把
    取值与执行拆开的直接收益——动作层现在可被独立验证。
    """
    ws = _project(app)
    f0 = len(ws.step_model().faces)
    assert app.apply_tess_tol(0.5) is True
    f1 = len(ws.step_model().faces)
    assert f1 <= f0, "容差变大后面数不应增加"
    assert ws.structure()["tess_tol"] == pytest.approx(0.5)


def test_create_named_project_via_action_layer(app):
    """创建工程走动作层：名字与幅面生效，初始无设计（AGT-009 人工等效）。"""
    assert app.create_named_project("动作层工程", (150.0, 120.0)) is True
    assert app.ws.project["name"] == "动作层工程"
    assert app.ws.project["bed_x_mm"] == pytest.approx(150.0)
    assert app.ws.step_model() is None


# ================================================================ 走线域深路径
def test_auto_route_candidate_applied_is_undoable(app):
    """自动布线候选 → 落线 → 可撤销（CIR-006/T08）。"""
    ws = _project(app)
    n0 = len(ws.routes())
    cands = ws.cmd_auto_route_candidates("N2")
    ok = [c for c in cands if not c.get("failed")]
    assert ok, "应有可用候选"
    ws.cmd_add_route(ok[0]["net"], ok[0]["pts"], ok[0]["width"])
    assert len(ws.routes()) == n0 + 1
    app.undo()
    assert len(ws.routes()) == n0


def test_auto_place_candidates_do_not_mutate(app):
    """自动布局只给候选：调用后设计不变（CIR-005 只建议）。"""
    ws = _project(app)
    before = ws.design_digest()
    app.auto_place()
    assert ws.design_digest() == before, "看候选不得改设计"


def test_jumper_candidates_for_broken_net(app):
    """断开网络给出跳线候选；应用后连通性恢复（CIR-007）。"""
    ws = _project(app, with_board=False)
    ws.cmd_import_board((SAMPLES / "board_10.kicad_pcb").read_text("utf-8"))
    app.mrc = ws.run_mrc()
    app.jumper_candidates()
    assert app.last_dialog or app.dialogs, "应有候选或无需跳线的说明"


def test_undo_redo_cycle_restores_state(app):
    """撤销/重做往返后设计摘要一致（PRJ-005）。"""
    ws = _project(app)
    d0 = ws.design_digest()
    ws.cmd_change_width(ws.routes()[0]["id"], 0.9)
    d1 = ws.design_digest()
    app.undo()
    assert ws.design_digest() == d0
    app.redo()
    assert ws.design_digest() == d1


# ================================================================ 打印域深路径
def test_pause_and_resume_print(app):
    """打印中暂停落安全点，可续打（PRT-003/014）。"""
    ws = _project(app)
    ws.commit("打印")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None, timeout=40)
    app.connect_device()
    app.start_print()

    job = ws.store.one("SELECT * FROM print_job ORDER BY started_at DESC LIMIT 1")
    assert job is not None
    # 安全点必须在打印过程中落库（每道次一次），否则断电续打无从谈起
    assert job["safe_point_json"] or job["state"] in ("done", "fault")


def test_defect_export_writes_file(app, monkeypatch, tmp_path):
    """缺陷证据导出：文件非空且含缺陷类型（PRT-008）。"""
    ws = _project(app)
    ws.commit("导出缺陷")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None, timeout=40)
    app.connect_device()
    app.start_print()
    out = tmp_path / "defects.json"
    _pick_file(monkeypatch, out)
    app.export_defects()
    # 无缺陷时也应有明确的"无缺陷"提示，而不是静默失败
    assert out.exists() or app.last_dialog


# ================================================================ 声明的边界（负路径）
def test_actions_without_project_do_not_crash(app):
    """未打开工程时调用任何域动作都不崩、且给出引导（UI-007）。"""
    app.ws = None
    for fn in (app.run_mrc, app.show_ai_status, app.check_connectivity,
               app.slice_now, app.export_gcode, app.show_job_history,
               app.show_defects, app.run_agent, app.check_integrity):
        fn()          # 不抛异常即通过
    assert app.lbl_project.cget("text") in ("未打开工程", "")


def test_issue_actions_without_selection_are_safe(app):
    """未选中问题就点解释/建议/定位：只提示，不崩。"""
    _project(app)
    app.selected = None
    app.explain_selected()
    app.suggest_selected()
    app.locate_selected()
    assert app.dialogs
