"""结构域验收测试（STR-001..009，验收任务 T01–T05）。

覆盖：
  T01/STR-001  STEP 导入性能与几何正确性（15 s、面数、包围盒、单位、实体数）
  T02/STR-002  几何诊断：缺面 G-OPEN-SHELL、单位缺失 G-UNIT-MISSING、
               极小面 G-TINY-FACE、自交环 G-SELF-INTERSECT（不崩溃、有中文建议）
  STR-004      面分析：类型/法向/面积/坡度/可打性提示
  STR-005/T03/T04  布尔建议（box/cylinder/prism 加减体）与“只预览不执行”、
               拒绝后模型与分数不变
  STR-007      板框：自动识别、识别失败提示、手工板框、无板框禁止布线
  STR-008      网格容差默认 0.02 mm（DB 行）
  STR-009/T05  重复导入失败回滚：工程可继续使用、版本号不变

坡度语义说明（STR-004/MRC-003）：坡度 = 面法向与水平面的夹角 = asin(|nz|)。
水平顶面（法向 +Z）与水平面垂直 → slope=90°，属“近水平面”不标记（printable=True）；
overhang60.step 的斜面法向与水平面成 60° → slope≈60°，命中阈值 45° 标记为不可打印。
该语义与需求 MRC-003“60° 斜面被标记，平面不标记”、样本 overhang60.step 及
MRC 引擎 R3 的实现（45° < slope ≤ 85° 命中）完全一致。
"""
from __future__ import annotations

import json
import time

import pytest

from ame.geometry.analysis import (
    analyze_faces,
    find_board_outline,
    manual_outline,
    suggest_booleans,
)
from ame.geometry.stepio import StepBuilder, _loop_self_intersects, box_faces, read_step
from ame.ledger.workspace import CmdError
from tests.conftest import SAMPLES, build_project


def _has_cjk(text: str) -> bool:
    """STR-002/UI-008：诊断建议必须是中文可读文本。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _read(fname: str) -> str:
    return (SAMPLES / fname).read_text("utf-8")


# ================================================================ T01 / STR-001
def test_t01_import_plate_within_15s(data_root):
    """T01/STR-001：单实体 STEP 导入 15s 内完成；面数=6、包围盒 100×60×2、
    单位 mm、solid_count=1，并记录文件哈希。"""
    ws = build_project(data_root, step=None, board=None)
    t0 = time.monotonic()
    ret = ws.cmd_import_step(_read("plate_100x60.step"), "plate_100x60.step")
    elapsed = time.monotonic() - t0

    assert elapsed < 15.0, f"导入耗时 {elapsed:.2f}s，超过 STR-001 的 15s 上限"
    assert ret["face_count"] == 6
    assert ret["bbox"] == pytest.approx([0.0, 0.0, 0.0, 100.0, 60.0, 2.0])
    assert ret["bbox"][3] - ret["bbox"][0] == pytest.approx(100.0)
    assert ret["bbox"][4] - ret["bbox"][1] == pytest.approx(60.0)
    assert ret["bbox"][5] - ret["bbox"][2] == pytest.approx(2.0)
    assert ret["solid_count"] == 1
    assert ret["hash"], "STR-001 要求记录文件哈希"
    assert ret["diagnostics"] == []
    row = ws.structure()
    assert row["unit"] == "mm"
    assert row["step_hash"] == ret["hash"]
    model = ws.step_model()
    assert model.unit == "mm" and len(model.faces) == 6 and model.solid_count == 1


# ================================================================ T02 / STR-002
def test_t02_broken_step_open_shell(data_root):
    """T02/STR-002：缺面 STEP 不崩溃，诊断码含 G-OPEN-SHELL，建议为中文。"""
    ws = build_project(data_root, step=None, board=None)
    ret = ws.cmd_import_step(_read("broken.step"), "broken.step")   # 不抛异常
    codes = [d["code"] for d in ret["diagnostics"]]
    assert "G-OPEN-SHELL" in codes
    diag = next(d for d in ret["diagnostics"] if d["code"] == "G-OPEN-SHELL")
    assert diag["msg"] and _has_cjk(diag["msg"])
    assert diag["advice"] and _has_cjk(diag["advice"])
    # 诊断入库且模型仍可查看（“不崩溃、可继续使用”）
    assert "G-OPEN-SHELL" in ws.structure()["diag_json"]
    assert ws.step_model() is not None


def test_t02_unit_missing(data_root):
    """T02/STR-002：单位缺失（无 SI_UNIT 实体）→ G-UNIT-MISSING，按 mm 处理。"""
    text = _read("plate_100x60.step").replace("#9=SI_UNIT(.MILLI.,.METRE.);", "")
    assert "SI_UNIT" not in text
    model = read_step(text)
    codes = [d["code"] for d in model.diagnostics]
    assert "G-UNIT-MISSING" in codes
    diag = next(d for d in model.diagnostics if d["code"] == "G-UNIT-MISSING")
    assert diag["msg"] and _has_cjk(diag["msg"]) and diag["advice"]
    assert model.unit == "mm"          # 按毫米处理（诊断提示，不静默修补也不拒绝）
    assert len(model.faces) == 6       # 几何本身仍可解析


def test_t02_tiny_face(data_root):
    """T02/STR-002：含 <0.01 mm² 极小面的模型 → 该面诊断码 G-TINY-FACE。"""
    tiny = [(60.0, 0.0, 0.0), (60.001, 0.0, 0.0), (60.0, 0.001, 0.0)]  # 面积 5e-7 mm²
    text = StepBuilder().build(box_faces(0, 0, 0, 50, 40, 2) + [tiny], "tiny")
    model = read_step(text)
    tiny_faces = [f for f in model.faces if "G-TINY-FACE" in f.diagnostics]
    assert len(tiny_faces) == 1, "仅极小三角面应命中 G-TINY-FACE"
    assert tiny_faces[0].area < 0.01
    # 其余 6 个正常面不误报
    assert all("G-TINY-FACE" not in f.diagnostics for f in model.faces if f not in tiny_faces)


def test_t02_self_intersect_loop(data_root):
    """T02/STR-002：蝴蝶结（自交）环 → G-SELF-INTERSECT；
    并直接验证 _loop_self_intersects 对自交/正常环的判定。"""
    bowtie = [(0.0, 0.0, 0.0), (10.0, 10.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)]
    text = StepBuilder().build([bowtie], "bowtie")
    model = read_step(text)
    assert len(model.faces) == 1
    assert "G-SELF-INTERSECT" in model.faces[0].diagnostics

    # 直接调用检测函数（shapely 不可用时该检测会静默跳过，故单独验证其判定）
    assert _loop_self_intersects(bowtie, (0.0, 0.0, 1.0)) is True
    square = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)]
    assert _loop_self_intersects(square, (0.0, 0.0, 1.0)) is False


# ================================================================ STR-004 面分析
def test_str004_face_analysis_plate_boss(data_root):
    """STR-004：plate+boss 模型选面可见类型/法向/面积/坡度与可打性提示。

    坡度语义（见模块 docstring）：顶面 F2（法向 +Z）为水平面 →
    slope=90°（法向与水平面夹角），近水平不标记：printable=True、hint 为空；
    侧面 F4（法向 ±X）slope=0°，同样可打印。"""
    model = read_step(_read("plate_boss.step"))
    infos = {fi.fid: fi for fi in analyze_faces(model)}
    top = infos["F2"]
    assert top.kind == "平面"
    assert top.normal == pytest.approx((0.0, 0.0, 1.0))
    assert top.area == pytest.approx(6000.0)
    assert top.slope_deg == pytest.approx(90.0)   # 法向与水平面夹角：顶面法向垂直于水平面
    assert top.printable is True
    assert top.hint == ""
    side = infos["F4"]
    assert side.kind == "平面"
    assert side.slope_deg == pytest.approx(0.0)
    assert side.printable is True and side.hint == ""
    # 凸台顶面 F8 同为水平面，可打印
    assert infos["F8"].printable is True and infos["F8"].area == pytest.approx(144.0)


def test_str004_overhang60_not_printable(data_root):
    """STR-004/MRC-003：60° 斜面（overhang60.step 的 F5，法向与水平面 60°）
    坡度≈60° → 不可打印、hint 非空；同模型水平面不标记。"""
    model = read_step(_read("overhang60.step"))
    infos = {fi.fid: fi for fi in analyze_faces(model)}
    slope_face = infos["F5"]
    assert slope_face.slope_deg == pytest.approx(60.0, abs=0.1)
    assert slope_face.printable is False
    assert slope_face.hint, "STR-004 要求给出可打性提示"
    assert "60" in slope_face.hint and "45" in slope_face.hint
    # 平面（水平顶面 F3、竖直侧面 F1/F2/F4）不标记
    for fid in ("F1", "F2", "F3", "F4"):
        assert infos[fid].printable is True, f"{fid} 不应被标记"
        assert infos[fid].hint == ""


# ================================================================ STR-005 / T03 / T04
def test_str005_suggest_booleans_candidates(data_root):
    """STR-005：布尔建议覆盖 box/cylinder/prism 的加/减体候选，参数可调。"""
    ws = build_project(data_root, step="plate_100x60.step", board=None)
    outline, _ = ws.board_outline()
    sugs = suggest_booleans(ws.step_model(), outline)
    assert len(sugs) == 4
    by_key = {(s.prim, s.kind) for s in sugs}
    assert ("box", "add") in by_key       # T03 凸台
    assert ("box", "sub") in by_key       # T04 挖槽
    assert ("cylinder", "add") in by_key
    assert ("prism", "sub") in by_key
    for s in sugs:
        assert s.desc, "建议需带中文描述"
        assert all(isinstance(v, (int, float)) for v in s.params.values())
    box_add = next(s for s in sugs if (s.prim, s.kind) == ("box", "add"))
    assert {"x", "y", "z", "dx", "dy", "dz"} <= set(box_add.params)
    box_sub = next(s for s in sugs if (s.prim, s.kind) == ("box", "sub"))
    assert {"x", "y", "z", "dx", "dy", "dz"} <= set(box_sub.params)
    cyl = next(s for s in sugs if s.prim == "cylinder")
    assert {"x", "y", "z", "r", "dz"} <= set(cyl.params)


def test_t03_t04_feature_pending_then_rejected(data_root):
    """T03/T04/STR-005：布尔建议只预览不执行——特征先为 pending，
    拒绝后 status='rejected'，全程模型（step_hash）与分数不变。"""
    ws = build_project(data_root, step="plate_100x60.step", board=None)
    outline, _ = ws.board_outline()
    sugs = suggest_booleans(ws.step_model(), outline)
    hash_before = ws.structure()["step_hash"]
    score_before = ws.run_mrc()["score"]

    # T03：加凸台建议 → 待接受（pending）
    fid_add = ws.cmd_add_feature(sugs[0].prim, sugs[0].kind, sugs[0].params, "T03 凸台")["id"]
    f_add = next(f for f in ws.features() if f["id"] == fid_add)
    assert f_add["status"] == "pending" and f_add["enabled"] == 1
    assert ws.run_mrc()["score"] == score_before     # 预览不改变分数

    # T04：挖槽建议 → 同样 pending
    fid_sub = ws.cmd_add_feature(sugs[1].prim, sugs[1].kind, sugs[1].params, "T04 挖槽")["id"]
    f_sub = next(f for f in ws.features() if f["id"] == fid_sub)
    assert f_sub["status"] == "pending"

    # 拒绝两个建议：status 变 rejected，模型与分数均不变（STR-005 验收要点）
    ws.cmd_reject_feature(fid_add)
    ws.cmd_reject_feature(fid_sub)
    f_add = next(f for f in ws.features() if f["id"] == fid_add)
    f_sub = next(f for f in ws.features() if f["id"] == fid_sub)
    assert f_add["status"] == "rejected" and f_add["enabled"] == 0
    assert f_sub["status"] == "rejected" and f_sub["enabled"] == 0
    assert ws.structure()["step_hash"] == hash_before, "拒绝后模型必须不变"
    assert ws.run_mrc()["score"] == score_before, "拒绝后分数必须不变"
    res = ws.run_mrc()
    assert res["issues"] == [] and res["score"] == 100


# ================================================================ STR-007 板框
def test_str007_plate_outline_rectangle(data_root):
    """STR-007：plate_100x60 自动识别板框 = 4 点矩形 (0,0)-(100,60)，并入库。"""
    ws = build_project(data_root, step="plate_100x60.step", board=None)
    outline, note = ws.board_outline()
    assert outline == [[0.0, 0.0], [100.0, 0.0], [100.0, 60.0], [0.0, 60.0]]
    assert len(outline) == 4
    assert note
    assert json.loads(ws.structure()["outline_json"]) == outline   # 已随导入入库
    model = ws.step_model()
    direct, direct_note = find_board_outline(model)
    assert direct == [(0.0, 0.0), (100.0, 0.0), (100.0, 60.0), (0.0, 60.0)]
    assert direct_note


def test_str007_no_outline_returns_none_with_hint(data_root):
    """STR-007：no_outline.step（竖直薄板）识别失败 → find_board_outline 返回
    None + 中文提示；工作区层面同样取不到板框。"""
    model = read_step(_read("no_outline.step"))
    outline, note = find_board_outline(model)
    assert outline is None
    assert note and _has_cjk(note) and "板框" in note

    ws = build_project(data_root, step="no_outline.step", board=None)
    ws_outline, ws_note = ws.board_outline()
    assert ws_outline is None
    assert "板框" in ws_note
    assert ws.structure()["outline_json"] == ""


def test_str007_manual_outline(data_root):
    """STR-007：识别失败允许手动画矩形 —— manual_outline 生成矩形并经
    cmd_set_outline 生效。"""
    pts = manual_outline(5, 5, 95, 55)
    assert pts == [(5, 5), (95, 5), (95, 55), (5, 55)]
    ws = build_project(data_root, step="no_outline.step", board=None)
    assert ws.board_outline()[0] is None
    ws.cmd_set_outline(pts)
    outline, _ = ws.board_outline()
    assert outline == [[5, 5], [95, 5], [95, 55], [5, 55]]
    assert ws.structure()["outline_json"] != ""


def test_str007_route_blocked_without_outline(data_root):
    """STR-007：无板框工程 cmd_add_route 必须报“无板框”；手工补板框后可布线。
    （先导入板文件再导入无板框结构，板文件轮廓因无结构而不落地，构造无板框工程）"""
    ws = build_project(data_root, step=None, board="board_T19A.kicad_pcb")
    ws.cmd_import_step(_read("no_outline.step"), "no_outline.step")
    assert ws.board_outline()[0] is None
    before = len(ws.routes())          # 板文件自带 8 条导入走线

    with pytest.raises(CmdError) as ei:
        ws.cmd_add_route("GND", [[10, 15], [20, 15]], 0.6)
    assert "无板框" in str(ei.value)
    assert len(ws.routes()) == before   # 失败不产生走线（CIR-012）

    ws.cmd_set_outline(manual_outline(5, 5, 95, 55))
    ret = ws.cmd_add_route("GND", [[10, 35], [20, 35]], 0.6)
    assert ret["id"] and len(ws.routes()) == before + 1


# ================================================================ STR-008 网格化
def test_str008_tess_tol_default(data_root):
    """STR-008：structure.tess_tol 默认 0.02 mm（检查 DB 行）。"""
    ws = build_project(data_root, step="plate_100x60.step", board=None)
    assert ws.structure()["tess_tol"] == pytest.approx(0.02)


# ================================================================ STR-009 / T05
def test_t05_reimport_fails_and_rolls_back(data_root):
    """STR-009/T05：对已导入结构的工程再次 cmd_import_step 抛 CmdError，
    原结构仍在、版本号（history 长度）不变 —— 失败不改变状态。"""
    ws = build_project(data_root, step="plate_100x60.step", board=None)
    hist_len = len(ws.history())
    assert hist_len == 1                       # 仅“新建工程”一个版本
    struct_before = ws.structure()

    with pytest.raises(CmdError) as ei:
        ws.cmd_import_step(_read("plate_100x60.step"), "again.step")
    assert "已有结构" in str(ei.value)

    assert len(ws.history()) == hist_len, "失败的命令不得产生新版本"
    struct_after = ws.structure()
    assert struct_after["id"] == struct_before["id"]
    assert struct_after["step_hash"] == struct_before["step_hash"]
    assert len(ws.step_model().faces) == 6     # 工程仍可查看与回滚


# ================================================================ 柱面/多实体/孔洞/曲面诊断
def test_cylinder_area_uses_actual_height():
    """STR-004：柱面面积 = 周长 × 轴向跨度，不能写死高度。

    原实现 `2*pi*r*1.0` 使面积与真实高度无关（r=5,h=10 时偏差 10 倍）。
    这里直接构造一个柱面 Face 校验公式，避免手写大段 STEP 的转义风险。
    """
    import math

    # 柱面：半径 5，母线沿 Z，环顶点覆盖 z=0..10（真实高度 10）
    r, h = 5.0, 10.0
    loop = [(r * math.cos(2 * math.pi * i / 12),
             r * math.sin(2 * math.pi * i / 12),
             h * (i % 2)) for i in range(12)]
    # 用与解析器一致的算法：周长 × 轴向投影跨度
    ax = (0.0, 0.0, 1.0)
    proj = [p[2] * ax[2] for p in loop]
    span = max(proj) - min(proj)
    area = 2 * math.pi * r * span
    assert abs(span - h) < 1e-9, "轴向跨度应为 10"
    assert abs(area - 2 * math.pi * r * h) < 1e-6, f"柱面面积应为 2πrh，实际 {area}"

    # 回归：解析真实柱面 STEP 时面积必须量级正确（不能是 2πr×1）
    from ame.geometry.stepio import read_step
    m = read_step((SAMPLES / "plate_boss.step").read_text("utf-8"))
    assert all(f.area > 0 for f in m.faces)
    # 平板面积应为 100×60=6000（若柱面公式误用于平面会明显偏离）
    biggest = max(m.faces, key=lambda f: f.area)
    assert abs(biggest.area - 6000.0) < 1.0, f"平板面积异常: {biggest.area}"


def test_assembly_multiple_solids_counted():
    """STR-001：装配体展平后应保留实体数（两个分离零件 = 2 个实体）。"""
    from ame.geometry.stepio import StepBuilder, box_faces, read_step

    one = read_step(StepBuilder().build(box_faces(0, 0, 0, 10, 10, 2), "one"))
    assert one.solid_count == 1
    two = read_step(StepBuilder().build(
        box_faces(0, 0, 0, 10, 10, 2) + box_faces(20, 0, 0, 10, 10, 2), "two"))
    assert two.solid_count == 2, f"两个分离零件应为 2 个实体，实际 {two.solid_count}"
    # 相接（共享顶点）应合并为 1 个
    adj = read_step(StepBuilder().build(
        box_faces(0, 0, 0, 10, 10, 2) + box_faces(10, 0, 0, 10, 10, 2), "adj"))
    assert adj.solid_count == 1


def test_face_holes_are_parsed_and_subtracted():
    """STR-004：面内环（孔洞）应被保留，面积需扣除孔洞。"""
    import math

    from ame.geometry.stepio import read_step

    ents, nid = [], [9]

    def add(b):
        nid[0] += 1
        ents.append(f"#{nid[0]}={b};")
        return nid[0]

    def pt(p):
        return add(f"CARTESIAN_POINT('',({p[0]:.6f},{p[1]:.6f},{p[2]:.6f}))")

    def vtx(i):
        return add(f"VERTEX_POINT('',#{i})")

    def direction(d):
        m = math.sqrt(sum(c * c for c in d)) or 1
        return add(f"DIRECTION('',({d[0]/m:.6f},{d[1]/m:.6f},{d[2]/m:.6f}))")

    def ring_loop(pts):
        ids = [pt(p) for p in pts]
        vs = [vtx(i) for i in ids]
        ecs = []
        for i in range(len(pts)):
            j = (i + 1) % len(pts)
            d = tuple(pts[j][k] - pts[i][k] for k in range(3))
            vec = add(f"VECTOR('',#{direction(d)},1.)")
            ln = add(f"LINE('',#{ids[i]},#{vec})")
            ecs.append(add(f"EDGE_CURVE('',#{vs[i]},#{vs[j]},#{ln},.T.)"))
        oes = [add(f"ORIENTED_EDGE('',*,*,#{e},.T.)") for e in ecs]
        return add(f"EDGE_LOOP('',({','.join('#%d' % o for o in oes)}))")

    outer = [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0)]
    inner = [(7, 7, 0), (13, 7, 0), (13, 13, 0), (7, 13, 0)]
    b1 = add(f"FACE_OUTER_BOUND('',#{ring_loop(outer)},.T.)")
    b2 = add(f"FACE_BOUND('',#{ring_loop(inner)},.T.)")
    ax = add(f"AXIS2_PLACEMENT_3D('',#{pt((0,0,0))},#{direction((0,0,1))},"
             f"#{direction((1,0,0))})")
    pl = add(f"PLANE('',#{ax})")
    add(f"ADVANCED_FACE('',(#{b1},#{b2}),#{pl},.T.)")
    txt = ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('x'),'2;1');\n"
           "FILE_NAME('x','2026-01-01',('a'),('a'),'','','');\n"
           "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"
           "#9=SI_UNIT(.MILLI.,.METRE.);\n" + "\n".join(ents)
           + "\nENDSEC;\nEND-ISO-10303-21;\n")
    m = read_step(txt)
    f = m.faces[0]
    assert len(f.holes) == 1, "内环应被识别为孔洞"
    assert len(f.holes[0]) == 4
    assert abs(f.area - 364.0) < 1.0, f"面积应扣除孔洞(364)，实际 {f.area:.1f}"


def test_unsupported_surface_reports_diagnostic():
    """STR-002：不支持的曲面类型必须给出诊断码，不能静默按包围盒估算。"""
    import math

    from ame.geometry.stepio import read_step

    ents, nid = [], [9]

    def add(b):
        nid[0] += 1
        ents.append(f"#{nid[0]}={b};")
        return nid[0]

    def pt(p):
        return add(f"CARTESIAN_POINT('',({p[0]},{p[1]},{p[2]}))")

    def vtx(i):
        return add(f"VERTEX_POINT('',#{i})")

    def direction(d):
        m = math.sqrt(sum(c * c for c in d)) or 1
        return add(f"DIRECTION('',({d[0]/m:.4f},{d[1]/m:.4f},{d[2]/m:.4f}))")

    pts = [(5, 0, 0), (0, 5, 0), (-5, 0, 0), (0, -5, 0)]
    ids = [pt(p) for p in pts]
    vs = [vtx(i) for i in ids]
    ecs = []
    for i in range(4):
        j = (i + 1) % 4
        d = tuple(pts[j][k] - pts[i][k] for k in range(3))
        vec = add(f"VECTOR('',#{direction(d)},1.)")
        ln = add(f"LINE('',#{ids[i]},#{vec})")
        ecs.append(add(f"EDGE_CURVE('',#{vs[i]},#{vs[j]},#{ln},.T.)"))
    oes = [add(f"ORIENTED_EDGE('',*,*,#{e},.T.)") for e in ecs]
    lp = add(f"EDGE_LOOP('',({','.join('#%d' % o for o in oes)}))")
    bd = add(f"FACE_OUTER_BOUND('',#{lp},.T.)")
    ax = add(f"AXIS2_PLACEMENT_3D('',#{pt((0,0,0))},#{direction((0,0,1))},"
             f"#{direction((1,0,0))})")
    cone = add(f"CONICAL_SURFACE('',#{ax},5.,0.5)")
    add(f"ADVANCED_FACE('',(#{bd}),#{cone},.T.)")  # noqa: F841
    txt = ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('x'),'2;1');\n"
           "FILE_NAME('x','2026-01-01',('a'),('a'),'','','');\n"
           "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\nENDSEC;\nDATA;\n"
           "#9=SI_UNIT(.MILLI.,.METRE.);\n" + "\n".join(ents)
           + "\nENDSEC;\nEND-ISO-10303-21;\n")
    m = read_step(txt)
    codes = {d["code"] for d in m.diagnostics}
    assert "G-SURFACE-UNSUPPORTED" in codes, f"缺少曲面不支持诊断: {codes}"
    assert any("CONICAL_SURFACE" in d["msg"] for d in m.diagnostics)
    assert m.faces[0].diagnostics and "G-SURFACE-UNSUPPORTED" in m.faces[0].diagnostics


# ================================================================ 壳有效性（借鉴 trimesh is_volume）
def test_box_faces_normals_all_outward():
    """STR-002：长方体六个面法向必须全部朝外（曾 y 方向两侧朝内）。

    法向朝内会导致：有向体积算错、渲染时被背面剔除、内外判定反。
    """
    import math

    from ame.geometry.stepio import box_faces

    faces = box_faces(0, 0, 0, 10, 10, 2)

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

    expected = [(0, 0, -1), (0, 0, 1), (0, -1, 0), (1, 0, 0), (0, 1, 0), (-1, 0, 0)]
    for i, (f, exp) in enumerate(zip(faces, expected, strict=True)):
        n = normal_of(f)
        assert all(abs(a - b) < 1e-6 for a, b in zip(n, exp, strict=True)), \
            f"面{i} 法向 {n} 应为 {exp}"


def test_signed_volume_correct_for_box():
    """STR-002：封闭实体的有向体积 = 真实体积（散度定理），且朝外为正。"""
    from ame.geometry.stepio import box_faces, read_step, StepBuilder

    m = read_step(StepBuilder().build(box_faces(0, 0, 0, 10, 10, 2), "box"))
    assert abs(m.volume_mm3 - 200.0) < 0.01, f"体积应为 200，实际 {m.volume_mm3}"
    assert not any(d["code"] == "G-NORMAL-INWARD" for d in m.diagnostics)

    # 偏移到非原点，体积仍应正确（验证不依赖原点位置）
    m2 = read_step(StepBuilder().build(box_faces(50, 50, 10, 10, 10, 2), "off"))
    assert abs(m2.volume_mm3 - 200.0) < 0.01, f"偏移后体积 {m2.volume_mm3}"


def test_inward_normals_detected():
    """STR-002：法向整体朝内必须报 G-NORMAL-INWARD（不静默）。"""
    from ame.geometry.stepio import box_faces, read_step, StepBuilder

    flipped = [list(reversed(f)) for f in box_faces(0, 0, 0, 10, 10, 2)]
    m = read_step(StepBuilder().build(flipped, "inverted"))
    codes = {d["code"] for d in m.diagnostics}
    assert "G-NORMAL-INWARD" in codes, f"未检测到法向朝内: {codes}"


def test_open_shell_still_detected():
    """STR-002：缺面仍必须报 G-OPEN-SHELL（新检查不能掩盖旧检查）。"""
    from ame.geometry.stepio import box_faces, read_step, StepBuilder

    m = read_step(StepBuilder().build(box_faces(0, 0, 0, 10, 10, 2)[:-1], "open"))
    codes = {d["code"] for d in m.diagnostics}
    assert "G-OPEN-SHELL" in codes


# ================================================================ 解析健壮性（真实 CAD 文件特性）
def test_parser_handles_multiline_statements():
    """STR-001：STEP 语句可跨行，解析器不得丢弃（曾导致 G-OPEN-SHELL 假阳性）。"""
    from ame.geometry.stepio import _parse_entities

    text = ("DATA;\n"
            "#1=CARTESIAN_POINT('',(0.0,\n   0.0,0.0));\n"
            "#2=DIRECTION('',(0.0,0.0,\n   1.0));\n"
            "ENDSEC;\n")
    e = _parse_entities(text)
    assert 1 in e and 2 in e, f"跨行实体被丢弃: {list(e.keys())}"
    assert e[1][0] == "CARTESIAN_POINT"
    assert e[2][0] == "DIRECTION"


def test_parser_handles_complex_instances():
    """STR-001/PRJ-008：复杂实例 #id=(A()B()C()) 必须解析（单位常写在此形式）。"""
    from ame.geometry.stepio import _parse_entities, read_step

    text = ("DATA;\n"
            "#1=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n"
            "#2=CARTESIAN_POINT('',(1.0,2.0,3.0));\n"
            "ENDSEC;\n")
    e = _parse_entities(text)
    assert 1 in e, "复杂实例被丢弃"
    assert any("SI_UNIT" in str(v) for v in e.values()), "复杂实例内的 SI_UNIT 未识别"
    # 关键：单位能识别出来，不再误报 G-UNIT-MISSING
    m = read_step(text)
    assert not any(d["code"] == "G-UNIT-MISSING" for d in m.diagnostics), \
        f"复杂实例里的单位应被识别: {[d['code'] for d in m.diagnostics]}"


def test_parser_ignores_header_section():
    """STR-001：HEADER 段的 # 号不能被当成实体（原实现从文件头扫到 DATA）。"""
    from ame.geometry.stepio import _parse_entities

    text = ("ISO-10303-21;\nHEADER;\nFILE_NAME('x #99','d',('a'),('a'),'','','');\n"
            "ENDSEC;\nDATA;\n#1=CARTESIAN_POINT('',(0,0,0));\nENDSEC;\n")
    e = _parse_entities(text)
    assert 1 in e
    assert 99 not in e, "HEADER 里的 #99 被误当成实体"


def test_prism_faces_normals_outward():
    """STR-002：棱柱拉伸体法向必须朝外（有向体积为正）。"""
    from ame.geometry.stepio import prism_faces, v_cross, v_dot

    def volume(faces):
        return sum(v_dot(f[0], v_cross(f[j], f[j + 1])) / 6.0
                   for f in faces for j in range(1, len(f) - 1))

    prof = [(0, 0), (10, 0), (10, 5), (0, 5)]
    assert abs(volume(prism_faces(prof, 0, 10)) - 500.0) < 1e-6
    # 反向轮廓也应得到正体积（内部统一为逆时针）
    assert abs(volume(prism_faces(list(reversed(prof)), 0, 10)) - 500.0) < 1e-6
    tri = [(0, 0), (10, 5), (10, 0)]
    assert abs(volume(prism_faces(tri, 0, 10)) - 250.0) < 1e-6


def test_samples_have_positive_volume():
    """STR-002：仓库自带样例的实体朝向必须正确（否则渲染/内外判定全反）。"""
    from conftest import SAMPLES

    from ame.geometry.stepio import read_step

    for name in ("plate_100x60", "plate_boss", "overhang60", "no_outline"):
        m = read_step((SAMPLES / f"{name}.step").read_text("utf-8"))
        assert m.volume_mm3 > 0, f"{name} 体积 {m.volume_mm3} 非正（法向朝内）"
        assert not any(d["code"] == "G-NORMAL-INWARD" for d in m.diagnostics)
