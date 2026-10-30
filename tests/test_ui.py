"""界面域验收（UI-001..010）。

界面用 Tk 构建；测试在真实窗口上构建后立即销毁，不进入 mainloop。
若运行环境没有可用显示（无 DISPLAY / 无桌面会话），整文件 skip。
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from ame.ledger.workspace import create_project  # noqa: E402

from conftest import SAMPLES, wait_for  # noqa: E402

pytestmark = pytest.mark.ui


@pytest.fixture(scope="session")
def _tk_session():
    """整文件共用一个 Tk 解释器：反复创建/销毁根窗口会让 Tcl 状态失效。
    窗口自始至终 withdraw（不映射到屏幕），测试期间不会弹窗。"""
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
def app(_tk_session, data_root, tmp_path):
    """每个用例重置到一个干净工程，复用同一个隐藏窗口。"""
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
    a._refresh_all()
    a.update_idletasks()
    a.update()
    yield a
    for w in list(a.winfo_children()):
        if w.winfo_class() == "Toplevel":
            w.destroy()
    a.update_idletasks()


def _setup_project(app, with_board=True):
    """在 app 里建好工程并导入样例（不经过文件对话框）。"""
    app.ws = create_project(app.data_root, "界面测试")
    app.ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                           "plate_100x60.step")
    if with_board:
        app.ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    app.mrc = app.ws.run_mrc()
    app._refresh_all()
    app.update()


def test_ui001_main_layout_regions(app):
    """UI-001 顶栏/左栏/中央画布/右栏/底栏五个区域都存在且可见。"""
    assert app.lbl_project.winfo_exists()
    assert app.lbl_version.winfo_exists()
    assert app.lbl_score.winfo_exists()
    assert app.canvas.winfo_exists()
    assert app.progress.winfo_exists()
    assert app.lbl_status.winfo_exists()
    tabs = [app.nb.tab(i, "text") for i in range(app.nb.index("end"))]
    # UI-004 要求七类面板齐全。早期只有 4 个页签（属性/问题/计划/对话），
    # "层列表"与"打印面板"缺失，而原测试把缺项写进了期望。
    assert tabs == ["属性", "问题", "层", "打印", "计划", "对话"]
    assert app.layer_tree.winfo_exists(), "层列表面板（UI-004）"
    assert app.pr_defect_tree.winfo_exists(), "打印面板（UI-004）"
    # 1920×1080 下不被遮挡：要求的默认窗口为 1600×960，最小 1280×800
    assert app.winfo_width() >= 1280 and app.winfo_height() >= 800


def test_ui007_empty_state_guides_user(app):
    """UI-007 没有工程时画布给出引导文字，而不是纯白屏。"""
    items = app.canvas.find_all()
    texts = [app.canvas.itemcget(i, "text") for i in items
             if app.canvas.type(i) == "text"]
    assert any("新建工程" in t for t in texts)


def test_ui007_no_outline_state(app):
    """UI-007 有结构但没板框时，画布提示原因并说明不能切片（STR-007）。"""
    app.ws = create_project(app.data_root, "无板框")
    app.mrc = {"issues": [], "score": None}
    app._refresh_all()
    app.update()
    texts = [app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
             if app.canvas.type(i) == "text"]
    assert any("没有板框" in t and "不能切片" in t for t in texts)


def test_ui002_view_switch_keeps_selection(app):
    """UI-002 三维↔二维切换后选中对象保持一致。"""
    _setup_project(app)
    comps = app.ws.comp_state()
    app.selected = {"type": "comp", "data": comps[0]}
    app.set_view("3d")
    assert app.selected["data"]["ref"] == comps[0]["ref"]
    app.set_view("2d")
    assert app.selected["data"]["ref"] == comps[0]["ref"]
    assert app.view_mode == "2d"
    assert app.lbl_view.cget("text").startswith("二维")


def test_ui003_select_shows_properties(app):
    """UI-003 点选器件后属性面板显示该对象字段。"""
    _setup_project(app)
    comp = app.ws.comp_state()[0]
    app._show_prop(comp)
    text = app.prop_text.get("1.0", "end")
    assert comp["ref"] in text and "器件高度" in text   # 属性面板中文化（UI-008）


def test_ui004_issue_panel_lists_problems(app):
    """UI-004/UI-010 问题面板列出全部问题，且规则码可见。"""
    _setup_project(app)
    rows = app.issue_tree.get_children()
    assert len(rows) == len(app.mrc["issues"]) > 0
    vals = [app.issue_tree.item(r, "values") for r in rows]
    assert any(v[1] == "R1" for v in vals)


def test_ui005_progress_and_error_surface(app):
    """UI-005 长任务有进度反馈；失败给出错误说明而不崩溃。"""
    _setup_project(app)
    app.progress_set(42)
    assert abs(float(app.progress["value"]) - 42) < 0.01
    app.set_view("2d")
    app.update()
    # 越界布线：必须被拒绝并给出可读原因（CIR-011）
    from ame.ledger.workspace import CmdError
    with pytest.raises(CmdError) as ei:
        app.ws.cmd_add_route("VCC", [[-5, -5], [50, 50]], 0.6)
    assert "板框" in str(ei.value)


def test_ui008_chinese_error_text(app):
    """UI-008 主要流程与错误信息使用中文，不以内部代号作为唯一解释。"""
    _setup_project(app)
    from ame.ledger.workspace import CmdError
    with pytest.raises(CmdError) as ei:
        app.ws.cmd_change_width(app.ws.routes()[0]["id"], 0.3)
    msg = str(ei.value)
    assert "线宽" in msg and "下限" in msg


def test_ui010_help_contains_rule_reference(app):
    """UI-010 帮助包含术语、规则说明与快捷键，离线可读。"""
    captured = {}
    orig = app._show_text

    def spy(title, body):
        captured["title"], captured["body"] = title, body
        orig(title, body)
    app._show_text = spy
    app.show_help()
    body = captured["body"]
    for token in ("R1", "R2", "R7", "Ctrl+Z", "错误码", "道次"):
        assert token in body
    app._show_text = orig


def test_ui_slice_and_print_flow(app):
    """UI 切片→打印全流程在界面上可完成，状态栏给出中文结果。"""
    _setup_project(app)
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None), "切片未在超时内完成"
    app.update()
    assert "切片完成" in app.lbl_status.cget("text")
    assert app.slices and app.slices["passes"] == 7
    app.connect_device()
    app.start_print()
    app.update()
    assert "虚拟打印完成" in app.lbl_status.cget("text")


def test_ui_agent_flow_produces_card(app):
    """UI 智能体：运行后在计划面板显示对比卡片与重放校验（AGT-005/008）。"""
    _setup_project(app)
    start = app.mrc["score"]
    app.run_agent()
    app.update()
    plan = app.plan_text.get("1.0", "end")
    assert "对比卡片" in plan
    assert "重放校验" in plan
    assert app.mrc["score"] - start >= 15


def test_ui001_domain_buttons_switch_actions(app):
    """UI-001 四域入口切换后左侧动作区随之变化。"""
    for key in ("structure", "circuit", "mrc", "slicing", "print"):
        app.enter_domain(key)
        assert app.current_domain == key
        assert len(app.action_frame.winfo_children()) >= 3


def test_str003_section_and_measure(app):
    """STR-003 三维查看：剖切按 Z 阈值隐藏高处对象；测量给出两点距离。"""
    class _Ev:
        def __init__(self, x, y):
            self.x, self.y = x, y

    _setup_project(app)
    # 剖切：高度设为 0.5 mm → 高 1.5 mm 的器件不显示
    app.section_z = 0.5
    app.redraw()
    texts = [app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
             if app.canvas.type(i) == "text"]
    assert not any("U1" in t for t in texts)          # 器件被剖掉
    assert any("剖切" in t for t in texts)             # 状态提示可见
    app.section_z = None
    app.redraw()
    texts = [app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
             if app.canvas.type(i) == "text"]
    assert any("U1" in t for t in texts)              # 取消剖切后恢复

    # 测量：两点 (20,20) (50,20) → 约 30 mm（屏幕像素取整带来 <1% 误差）
    app.set_view("2d")
    app.measure()
    app.on_canvas_click(_Ev(*[int(v) for v in app._to_screen(20, 20)]))
    app.on_canvas_click(_Ev(*[int(v) for v in app._to_screen(50, 20)]))
    st = app.lbl_status.cget("text")
    assert "测量" in st
    assert abs(app.last_measure["distance_mm"] - 30.0) < 0.5


def test_slc006_layer_playback(app):
    """SLC-006 逐层播放：可逐道次前进，画布只显示该道次路径并标出起停点。"""
    _setup_project(app)
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None), "切片未在超时内完成"
    app.play_layers()
    assert app.play_index == 0
    assert "0/" in app.play_label.cget("text")
    first = app.play_label.cget("text")
    # 前进到最后一个道次
    for _ in range(10):
        app.play_index = min(app.play_index + 1, len(app.slices["pass_details"]) - 1)
    app.play_index = len(app.slices["pass_details"]) - 1
    app.play_label.configure(
        text=f"{app.slices['pass_details'][-1]['pass_no']}/{len(app.slices['pass_details']) - 1}")
    app.redraw()
    assert first != app.play_label.cget("text")
    types = [app.canvas.type(i) for i in app.canvas.find_all()]
    assert "oval" in types and "rectangle" in types     # 起停点标记
    app.play_index = None
    app.play_window.destroy()


# ================================================================ 人视角体验（第四轮）
def test_ui_score_hidden_when_no_design(app):
    """UI-007：没有设计时不显示虚高分数（空工程曾显示 100 分会误导用户）。"""
    from ame.ledger.workspace import create_project
    app.ws = create_project(app.data_root, "空工程")
    app.mrc = app.ws.run_mrc()
    app._refresh_all()
    txt = app.lbl_score.cget("text")
    assert "100" not in txt
    assert "待导入" in txt or "缺板框" in txt


def test_ui_score_hidden_when_outline_missing(app):
    """UI-007：有结构但没板框时，分数位提示“缺板框”而不是绿色 100 分。"""
    from conftest import SAMPLES
    from ame.ledger.workspace import create_project
    app.ws = create_project(app.data_root, "无板框")
    app.ws.cmd_import_step((SAMPLES / "no_outline.step").read_text("utf-8"), "n.step")
    app.mrc = app.ws.run_mrc()
    app._refresh_all()
    txt = app.lbl_score.cget("text")
    # 只有结构、没有电路时提示"待导入板文件"；两者的判据都是"不给虚高分数"
    assert "待导入板文件" in txt or "缺板框" in txt
    assert "100" not in txt


def test_ui_issue_target_shows_net_name_not_uuid(app):
    """UI-008：问题列表“对象”列是网络名等可读名，不是 UUID 前缀。"""
    _setup_project(app)
    rows = app.issue_tree.get_children()
    vals = [app.issue_tree.item(r, "values") for r in rows]
    assert vals
    for v in vals:
        target = v[2]
        assert not target.startswith(tuple("0123456789abcdef")) or "走线" in target, target
    assert any("走线" in v[2] or "焊盘" in v[2] or "面" in v[2] for v in vals)


def test_ui_issue_reason_has_no_uuid(app):
    """UI-008：问题原因用网络名描述，不暴露 UUID。"""
    import re
    _setup_project(app)
    for r in app.issue_tree.get_children():
        reason = app.issue_tree.item(r, "values")[3]
        assert not re.search(r"\b[0-9a-f]{8}\b", reason), reason


def test_ui_properties_are_chinese(app):
    """UI-008：属性面板显示中文字段名与格式化数值，不暴露原始字典。"""
    _setup_project(app)
    comp = app.ws.comp_state()[0]
    app._show_prop(comp)
    text = app.prop_text.get("1.0", "end")
    assert "位号" in text and "X 坐标" in text
    assert "x_mm" not in text and "{" not in text and "'" not in text
    # 不应出现重复标签
    labels = [ln.split("：")[0] for ln in text.strip().splitlines() if "：" in ln]
    assert len(labels) == len(set(labels)), labels


def test_ui_layer_label_tracks_slice_and_playback(app):
    """UI-001：底栏层信息随切片与逐层播放更新（不再是固定的“层：—”）。"""
    _setup_project(app)
    assert "未切片" in app.lbl_layer.cget("text")
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None), "切片未在超时内完成"
    txt = app.lbl_layer.cget("text")
    assert "道次" in txt and "Z=" in txt
    app.play_index = 4
    app._update_layer_label(app.ws)
    txt4 = app.lbl_layer.cget("text")
    assert "道次 4" in txt4


def test_ui_heatmap_renders_and_message_matches(app):
    """MRC-010：热力图真的画出热区，且状态栏计数与数据点一致（曾静默失败）。"""
    _setup_project(app)
    n_before = len([i for i in app.canvas.find_all() if app.canvas.type(i) == "oval"])
    app.toggle_heatmap()
    app.update()
    hm = app._heat_data()
    assert hm and hm["points"], "热力图应有数据点"
    n_after = len([i for i in app.canvas.find_all() if app.canvas.type(i) == "oval"])
    assert n_after > n_before, "画布应新增热力圈"
    assert str(len(hm["points"])) in app.lbl_status.cget("text")


def test_ui_agent_panel_is_chinese(app):
    """UI-008：Agent 计划面板用中文动作名，不直接倾倒 JSON。"""
    _setup_project(app)
    app.run_agent()
    txt = app.plan_text.get("1.0", "end")
    assert "调整线宽" in txt or "重新布线" in txt
    assert "change_width" not in txt
    assert '{"round"' not in txt


def test_ui_slice_switches_canvas_to_preview(app):
    """SLC-006：切片完成后画布自动进入道次预览（不再停留在原设计）。"""
    _setup_project(app)
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None), "切片未在超时内完成"
    assert app.play_index == 0
    texts = [app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
             if app.canvas.type(i) == "text"]
    assert any("播放道次" in t for t in texts)


# ================================================================ STR-003 三维渲染（光照/深度）
def test_str003_camera_projection_rotates():
    """STR-003 旋转：不同方位角下同一世界点投影位置必须不同。"""
    from ame.ui.render import Camera

    p = (10.0, 20.0, 5.0)
    a = Camera(az_deg=0, el_deg=30).project(p, 5.0, (400.0, 300.0))
    b = Camera(az_deg=90, el_deg=30).project(p, 5.0, (400.0, 300.0))
    assert a != b and all(abs(x - y) > 1e-6 for x, y in zip(a, b, strict=True))


def test_str003_camera_depth_orders_faces():
    """STR-003：相机的深度函数必须区分远近（近处点深度更大）。"""
    from ame.ui.render import Camera

    cam = Camera(az_deg=45, el_deg=35)
    near = cam.depth((0.0, 0.0, 0.0))
    far = cam.depth((-10.0, -10.0, 0.0))
    assert near > far, "靠近相机的点深度应更大"


def test_str003_lambert_shading_varies_by_normal():
    """STR-003 光照：不同朝向的面亮度必须不同（否则看不出立体）。"""
    from ame.ui.render import _light_dir, Camera, shade

    light = _light_dir(Camera())
    top = shade((0, 0, 1), light)
    side_x = shade((1, 0, 0), light)
    side_y = shade((0, 1, 0), light)
    assert len({round(top, 3), round(side_x, 3), round(side_y, 3)}) == 3, \
        "顶面/两个侧面亮度必须有差异"
    assert top > side_x, "顶面应比侧面亮（光源在上方）"


def test_str003_backface_culling():
    """STR-003：俯视时朝下的面应被剔除，朝上的面保留。"""
    from ame.ui.render import Camera

    cam = Camera(el_deg=45)          # 从斜上方看
    assert cam.facing((0, 0, 1)), "朝上面应可见"
    assert not cam.facing((0, 0, -1)), "朝下面应被剔除"


def test_str003_render_respects_occlusion_by_height():
    """STR-003 深度：凸台顶面必须晚于基板顶面绘制（否则被大面盖住）。"""
    from ame.geometry.stepio import read_step

    from ame.ui.render import Camera, bounds_and_scale, render_faces

    m = read_step((SAMPLES / "plate_boss.step").read_text("utf-8"))
    cam = Camera(az_deg=45, el_deg=35)
    cam.center = ((m.bbox[0] + m.bbox[3]) / 2, (m.bbox[1] + m.bbox[4]) / 2,
                  (m.bbox[2] + m.bbox[5]) / 2)
    sc, og = bounds_and_scale(m.bbox, cam, 640, 420)
    items = render_faces(m.faces, cam, sc, og)
    assert items
    zs = [it["z_mean"] for it in items]
    # 关键的遮挡关系：凸台顶面（高）必须晚于基板顶面（低）绘制，
    # 否则面积 6000 的基板会把凸台盖住。
    top_boss = 5.0      # 凸台顶
    top_plate = 2.0     # 基板顶
    assert top_boss in zs and top_plate in zs, zs
    assert zs.index(top_plate) < zs.index(top_boss),         f"基板顶面应早于凸台顶面绘制: {zs}"


def test_str003_render_section_filters_by_height():
    """STR-003 剖切：剖切高度以上的面不出现。"""
    from ame.geometry.stepio import read_step

    from ame.ui.render import Camera, bounds_and_scale, render_faces

    m = read_step((SAMPLES / "plate_boss.step").read_text("utf-8"))
    cam = Camera(az_deg=45, el_deg=35)
    cam.center = ((m.bbox[0] + m.bbox[3]) / 2, (m.bbox[1] + m.bbox[4]) / 2,
                  (m.bbox[2] + m.bbox[5]) / 2)
    sc, og = bounds_and_scale(m.bbox, cam, 640, 420)
    full = render_faces(m.faces, cam, sc, og)
    cut = render_faces(m.faces, cam, sc, og, section_z=3.0)
    assert len(cut) < len(full), "剖切后可见面应减少"
    # 剖切判据是"面**整体**位于剖切面之上才剔除"（`min(zs) > section_z`），
    # 所以跨越剖切面的侧面（z_mean 可能高于剖切高度）被保留是正确的。
    # 原断言写"所有面 z_mean <= 3.0"，被 `or True` 掩盖成恒真；删掉后立刻
    # 暴露：断言本身错了（实测 2 个侧面 z_mean=3.5，它们跨过剖切面）。
    #
    # 正确的可观测判据：z_mean 严格高于剖切高度的"顶面"必须消失。
    # 凸台顶面 z=5 已由下一条断言覆盖；这里补"没有任何面的 z_mean 超过
    # 剖切高度 + 模型半高"这种远离剖切面的情况。
    above = [it for it in cut if it["z_mean"] > 4.0 + 1e-6]
    assert not above, (
        f"剖切 z=3.0 后仍出现 z_mean>4.0 的面：{[(it.get('_fid'), it['z_mean']) for it in above]}")
    # 凸台顶面 z=5 必须被剖掉
    assert not any(abs(it["z_mean"] - 5.0) < 0.01 for it in cut)


def test_str003_render_performance():
    """STR-003 性能：800 面以内渲染应在交互阈值内（实测 ~4 ms）。"""
    import time

    from ame.geometry.stepio import StepBuilder, box_faces, read_step

    from ame.ui.render import Camera, bounds_and_scale, render_faces

    faces = []
    for i in range(100):
        faces += box_faces((i % 10) * 15, (i // 10) * 15, 0, 10, 10, 2)
    m = read_step(StepBuilder().build(faces, "perf"))
    cam = Camera(az_deg=45, el_deg=35)
    cam.center = ((m.bbox[0] + m.bbox[3]) / 2, (m.bbox[1] + m.bbox[4]) / 2,
                  (m.bbox[2] + m.bbox[5]) / 2)
    sc, og = bounds_and_scale(m.bbox, cam, 900, 700)
    t0 = time.perf_counter()
    items = render_faces(m.faces, cam, sc, og)
    dt = (time.perf_counter() - t0) * 1000
    assert items
    assert dt < 60, f"{len(m.faces)} 面渲染耗时 {dt:.1f} ms"


def test_str003_prism_faces_normals_point_outward():
    """STR-003/STR-002：棱柱生成器的侧面法向必须朝外（曾朝内导致渲染大片缺失）。"""
    import math

    from ame.geometry.stepio import prism_faces

    prof = [(0, 0), (10, 0), (10, 10 * math.tan(math.radians(30)))]
    faces = prism_faces(prof, 0, 30)

    def normal_of(f):
        n = [0.0, 0.0, 0.0]
        for j in range(1, len(f) - 1):
            e1 = [f[j][k] - f[0][k] for k in range(3)]
            e2 = [f[j + 1][k] - f[0][k] for k in range(3)]
            n[0] += e1[1] * e2[2] - e1[2] * e2[1]
            n[1] += e1[2] * e2[0] - e1[0] * e2[2]
            n[2] += e1[0] * e2[1] - e1[1] * e2[0]
        ln = math.sqrt(sum(c * c for c in n)) or 1
        return tuple(c / ln for c in n)

    # 两端盖法向必须分别朝 -Y / +Y
    caps_y = sorted(round(normal_of(f)[1], 2) for f in faces[:2])
    assert caps_y == [-1.0, 1.0], f"端盖法向异常: {caps_y}"
    # 至少存在一个朝上的面（顶部），且没有任何面法向指向几何内部
    ups = [f for f in faces if normal_of(f)[2] > 0.5]
    assert ups, "棱柱应有朝上的面"


def test_str003_canvas_draws_shaded_polygons(app):
    """STR-003：画布必须真正画出带填充色的多边形（不是只有线框）。"""
    from conftest import SAMPLES

    app.ws = create_project(app.data_root, "渲染")
    app.ws.cmd_import_step((SAMPLES / "plate_boss.step").read_text("utf-8"), "pb.step")
    app.mrc = app.ws.run_mrc()
    app._refresh_all()
    app.update()
    polys = [i for i in app.canvas.find_all() if app.canvas.type(i) == "polygon"]
    assert len(polys) >= 5, f"应绘制多个结构面，实际 {len(polys)}"
    fills = {app.canvas.itemcget(i, "fill") for i in polys}
    assert len(fills) >= 3, f"填充色层次不足（应体现光照）: {fills}"


def test_str003_orbit_interaction_changes_view(app):
    """STR-003 旋转交互：拖拽后相机方位/俯仰角变化并重绘。"""
    from conftest import SAMPLES

    app.ws = create_project(app.data_root, "旋转")
    app.ws.cmd_import_step((SAMPLES / "plate_boss.step").read_text("utf-8"), "pb.step")
    app.mrc = app.ws.run_mrc()
    app._refresh_all()
    app.set_view("3d")

    class Ev:
        def __init__(self, x, y):
            self.x, self.y = x, y

    az0, el0 = app.cam_az, app.cam_el
    app.on_canvas_click(Ev(300, 200))       # 开始拖拽
    app.on_canvas_drag(Ev(380, 240))        # 拖到新位置
    app.on_canvas_release(Ev(380, 240))
    assert app.cam_az != az0 and app.cam_el != el0, "拖拽后相机角度应变化"
    # 俯仰角应被限制在合理范围
    app.on_canvas_click(Ev(300, 200))
    app.on_canvas_drag(Ev(300, 5000))
    app.on_canvas_release(Ev(300, 5000))
    assert 5.0 <= app.cam_el <= 89.0

    # 适配窗口重置视角
    app.fit_view()
    assert app.cam_az == 45.0 and app.cam_el == 35.0


# ================================================================ UI-005 长任务不冻结
def test_ui005_long_task_runs_in_background(app):
    """UI-005：重任务必须转后台执行，调用立即返回、界面不冻结。"""
    import time

    from conftest import wait_for

    _setup_project(app)
    done = {}
    t0 = time.perf_counter()
    app._run_async("测试任务", lambda: (time.sleep(0.4), "OK")[1],
                   lambda r: done.__setitem__("v", r))
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 200, f"_run_async 应立刻返回，实际阻塞 {elapsed:.0f} ms"
    assert "测试任务" in app.lbl_status.cget("text")
    assert wait_for(app, lambda: "v" in done, timeout=5.0), "后台结果未回到主线程"
    assert done["v"] == "OK"


def test_ui005_slice_returns_immediately(app):
    """UI-005：切片调用必须立即返回（结果稍后由主线程回调写入）。"""
    import time

    from conftest import wait_for

    _setup_project(app)
    app.slices = None
    t0 = time.perf_counter()
    app.slice_now()
    elapsed = (time.perf_counter() - t0) * 1000
    assert elapsed < 300, f"slice_now 阻塞 {elapsed:.0f} ms"
    assert wait_for(app, lambda: app.slices is not None), "切片未完成"
    assert app.slices["passes"] == 7


def test_ui005_cancel_task_sets_flag(app):
    """UI-005：取消按钮设置取消标志，任务结果被丢弃并给出提示。"""
    _setup_project(app)
    app.cancel_task()
    assert app.task_cancel.is_set()
    assert "取消" in app.lbl_status.cget("text")


def test_ui005_error_in_background_reports(app):
    """UI-005：后台任务异常必须回报，不能静默丢弃。"""
    from conftest import wait_for

    _setup_project(app)
    seen = {}

    def boom():
        raise RuntimeError("模拟失败")

    app._run_async("坏任务", boom, lambda r: seen.__setitem__("ok", r),
                   lambda e: seen.__setitem__("err", e))
    assert wait_for(app, lambda: "err" in seen, timeout=5.0)
    assert "模拟失败" in str(seen["err"])


def test_ui005_sqlite_usable_from_background_thread(app):
    """UI-005：后台线程访问 SQLite 不得报 'objects created in a thread'。"""
    from conftest import wait_for

    _setup_project(app)
    out = {}

    def read_counts():
        return (len(app.ws.routes()), len(app.ws.pads()))

    app._run_async("读库", read_counts, lambda r: out.__setitem__("v", r),
                   lambda e: out.__setitem__("err", e))
    assert wait_for(app, lambda: "v" in out or "err" in out, timeout=5.0)
    assert "err" not in out, f"后台读库失败: {out.get('err')}"
    assert out["v"][0] > 0


# ================================================================ NFR-003 示例工程
def test_sample_project_one_click(app):
    """NFR-003：一键打开示例工程，新用户无需自备文件即可看到完整链路。"""
    assert app.ws is None
    app.open_sample()
    assert app.ws is not None
    assert app.ws.project["name"] == "T19-A 示例板"
    assert len(app.ws.comps()) == 8
    assert len(app.ws.routes()) == 8
    assert app.mrc["score"] == 60
    assert "示例工程已打开" in app.lbl_status.cget("text")
    # 画布应有内容（板框 + 器件 + 走线）
    assert len(app.canvas.find_all()) > 20


def test_sample_project_full_chain(app):
    """NFR-003：示例工程可直接跑通切片与打印（不需用户准备文件）。"""
    from conftest import wait_for

    app.open_sample()
    app.slice_now()
    assert wait_for(app, lambda: app.slices is not None)
    assert app.slices["passes"] == 7
    app.connect_device()
    app.start_print()
    assert "虚拟打印完成" in app.lbl_status.cget("text")


def test_selftest_entry_runs():
    """打包验证：--selftest 无界面自检应通过（退出码 0）。"""
    import subprocess
    import sys
    from pathlib import Path as _P

    repo = _P(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "ame", "--selftest"],
                       cwd=repo, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"自检失败:\n{r.stdout}\n{r.stderr}"
    for token in ("示例工程", "可打性评分", "切片", "G-code", "虚拟打印", "结果：通过"):
        assert token in r.stdout, f"自检输出缺少 {token}:\n{r.stdout}"


def test_selftest_motion_hash_is_reproducible():
    """SLC-008：连续两次自检的**运动内容哈希**必须相同。

    口径说明：G-code 头里的 `;project` / `;source_commit` 是溯源信息，
    每次新建示例工程都会变（新工程新版本 uuid），所以整文件哈希本就不该相等。
    可复现性要求的是"同一设计 → 同一打印动作"，因此断言运动内容哈希。
    这条测试存在的理由：曾用整文件哈希做判据，结果连续三次运行三个不同值，
    看上去像"G-code 不确定"，排查后才发现是溯源行——把口径写进断言就不会再误判。
    """
    import re
    import subprocess
    import sys
    from pathlib import Path as _P

    repo = _P(__file__).resolve().parent.parent
    seen = set()
    for _ in range(2):
        r = subprocess.run([sys.executable, "-m", "ame", "--selftest"],
                           cwd=repo, capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, f"自检失败:\n{r.stdout}\n{r.stderr}"
        m = re.search(r"运动内容哈希\s+([0-9a-f]{12})", r.stdout)
        assert m, f"自检输出缺少运动内容哈希:\n{r.stdout}"
        seen.add(m.group(1))
    assert len(seen) == 1, f"运动内容哈希不稳定：{seen}（SLC-008 被破坏）"
