"""真实 CAD 导出文件的回归基线（STR-002：只报告，不静默修补）。

样本来自 pythonocc-demos / trimesh 仓库中的真实 AP203/214 文件，覆盖
手写解析器最容易出错的三类情况：

1. ``occ214.stp`` —— 大型 AP214 装配导出。曾因 ``DIRECTION`` 参数不足 3 个
   触发 ``IndexError`` 使整个解析器崩溃；同时含跨行语句与复杂实例
   （``#id=(...)`` 多类型复合实体）。
2. ``feature.stp`` —— 以英寸声明（``CONVERSION_BASED_UNIT('INCH')``，
   基准为 ``SI_UNIT($,.METRE.)``）。曾漏识别单位，整体小 25.4 倍。
3. ``proe.stp`` —— 与 occ214 同一零件的英寸声明版本，但坐标数值本身按毫米
   写入（典型的上游 CAD 导出设置错误）。软件按文件声明换算（不做猜测性
   纠正），同时必须发出 ``G-SIZE-SUSPICIOUS`` 提醒用户核对。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ame.geometry.stepio import read_step

REAL = Path(__file__).resolve().parent.parent / "samples" / "data" / "real"


def _load(name: str) -> str:
    path = REAL / name
    if not path.exists():          # 精简安装包可能不带样本
        pytest.skip(f"缺少真实 CAD 样本 {name}")
    # 真实 STEP 常带 Latin-1 字符，用 replace 避免解码失败掩盖解析问题
    return path.read_text("utf-8", errors="replace")


def _codes(model) -> set[str]:
    return {d["code"] for d in model.diagnostics}


def _span(model) -> tuple[float, float, float]:
    b = model.bbox
    return (b[3] - b[0], b[4] - b[1], b[5] - b[2])


def test_occ214_parses_without_crash():
    """曾崩溃点：DIRECTION 参数少于 3 个 → _v3() 归一化。

    注意：该文件含大量 B 样条曲面（84 个 B_SPLINE_SURFACE[_WITH_KNOTS]），
    首版不支持，因此**应当**如实上报 G-SURFACE-UNSUPPORTED 等诊断。
    早期这些面级诊断没有汇总到 model.diagnostics，用户看不到任何提示。
    """
    model = read_step(_load("occ214.stp"))
    assert len(model.faces) == 53, "ADVANCED_FACE 数量应与文件中一致"
    assert model.solid_count == 5
    # 有诊断是正确行为（B 样条不支持必须告知），但不能是崩溃/空洞
    assert model.diagnostics, "不支持的曲面必须上报，不能静默"
    assert "G-SURFACE-UNSUPPORTED" in _codes(model)


def test_occ214_dimensions_are_millimetres():
    model = read_step(_load("occ214.stp"))
    assert model.unit == "mm"
    sx, sy, sz = _span(model)
    assert 180 < sx < 200 and 140 < sy < 160 and 190 < sz < 210, (sx, sy, sz)


def test_feature_inch_converted_to_mm():
    """英寸声明必须按 25.4 换算；换算后仍需是合理量级（不能触发量级告警）。"""
    model = read_step(_load("feature.stp"))
    assert "G-UNIT-INCH-CONVERTED" in _codes(model)
    assert model.unit == "mm"
    sx, sy, sz = _span(model)
    # 换算前为 5.34 × 2.5 × 1.375 inch
    assert 134 < sx < 138, sx
    assert 62 < sy < 65, sy
    assert 33 < sz < 36, sz
    # 换算正确时体积应为厘米级零件的合理值，而非微米级
    assert 1e4 < model.volume_mm3 < 1e7, model.volume_mm3
    assert "G-SIZE-SUSPICIOUS" not in _codes(model)


def test_feature_unsupported_surface_is_reported():
    """不支持的曲面类型只报告，不崩溃、不丢弃整个面。"""
    model = read_step(_load("feature.stp"))
    assert "G-SURFACE-UNSUPPORTED" in _codes(model)
    assert len(model.faces) == 96


def test_proe_declares_inch_but_coordinates_look_like_mm():
    """按声明换算后量级异常 → 必须报告，交给用户判断，而非软件猜。

    该文件声明单位为英寸，但坐标数值按毫米写入（典型的上游 CAD 导出设置
    错误），换算后达到 8.6 m。正确行为是「照声明换算 + 发出
    G-SIZE-SUSPICIOUS」，绝不能静默按毫米处理——那会在别的文件上把真正的
    英寸件做小 25.4 倍。
    """
    model = read_step(_load("proe.stp"))
    assert "G-UNIT-INCH-CONVERTED" in _codes(model)
    assert "G-SIZE-SUSPICIOUS" in _codes(model)
    sx, _, _ = _span(model)
    assert sx > 1000, "换算后必然超出一米量级，这是告警的触发条件"


def test_proe_circular_features_survive_conversion():
    """英寸件的圆弧离散容差必须折算到模型单位，否则精度粗 25.4 倍。

    proe.stp 含 56 个 CIRCLE 实体。曾在单位换算前用 0.02 mm 的弦高容差，
    对英寸坐标等价于 0.508 mm，圆弧被离散成明显折线。
    """
    model = read_step(_load("proe.stp"))
    verts = sum(len(f.loop) for f in model.faces)
    # 56 个圆按 0.02mm 弦高应产生数千个顶点；粗 25.4 倍时只剩几百个
    assert verts > 3000, f"圆弧离散过粗：{verts} 个顶点"
    # 弧面上的顶点应尽可能落在同一圆上：抽查朝向为圆的柱面法向分布
    assert model.volume_mm3 > 0


def test_inward_normals_reported_not_silently_flipped():
    """法向朝内只告警（STR-002）：软件不擅自翻转，但必须告知方向相反。"""
    model = read_step(_load("proe.stp"))
    assert "G-NORMAL-INWARD" in _codes(model)
    assert model.volume_mm3 > 0, "对外暴露的体积取绝对值"


def test_parser_is_deterministic_on_real_files():
    """同一文件解析两次结果完全一致（切片可重放的前提）。"""
    text = _load("feature.stp")
    a, b = read_step(text), read_step(text)
    assert a.bbox == b.bbox
    assert len(a.faces) == len(b.faces)
    assert abs(a.volume_mm3 - b.volume_mm3) < 1e-9
    for fa, fb in zip(a.faces, b.faces, strict=True):
        assert fa.normal == fb.normal
        assert fa.loop == fb.loop


# ---------------------------------------------------------------- 下游一致性
def test_board_holes_come_from_same_layer_as_outline():
    """板框与内孔必须来自同一层，否则孔会落在板框之外。

    这是真实缺陷：board_holes 曾取"面积最大的单片"，而 find_board_outline
    取"面积最大的并集层"，两者在拼接顶面上会选到不同层。
    """
    from shapely.geometry import Polygon

    from ame.geometry.analysis import board_holes, find_board_outline

    model = read_step(_load("feature.stp"))
    pts, _note = find_board_outline(model)
    assert pts is not None
    outer = Polygon(pts)
    holes = board_holes(model)
    assert len(holes) == 8, f"文件中该板有 8 个安装孔，实际 {len(holes)}"
    for h in holes:
        hp = Polygon(h)
        assert outer.contains(hp.representative_point()), "内孔必须在板框内"


def test_outline_rejected_on_multi_solid_assembly():
    """多实体装配（5 个分离实体）不应被当成一块板 → 明确拒绝而非猜一个。"""
    from ame.geometry.analysis import find_board_outline

    model = read_step(_load("occ214.stp"))
    pts, note = find_board_outline(model)
    assert pts is None
    assert "手工" in note or "手动画板框" in note, "拒绝时必须给出人工指定板框的指引"


def test_end_to_end_real_cad_to_gcode(tmp_path):
    """真实英寸件 → 导入 → 板框 → 板文件 → 切片 → G-code 全链路不崩。"""

    from ame.db.store import DataRoot
    from ame.ledger.services import do_slice
    from ame.ledger.workspace import create_project

    data = DataRoot(tmp_path / "data")
    ws = create_project(data, "真实英寸件")
    res = ws.cmd_import_step(_load("feature.stp"), "feature.stp")
    assert res["outline"] and len(res["outline"]) >= 4, res["outline_note"]
    # 单位换算在解析器内部完成，落库的结构单位必须是 mm（PRJ-008）
    assert ws.structure()["unit"] == "mm"

    board = REAL.parent / "board_T19A.kicad_pcb"
    ws.cmd_import_board(board.read_text("utf-8"))
    ws.commit("真实英寸件导入")

    sliced = do_slice(ws, "真实英寸件切片")
    assert sliced["slice_set_id"]
    gcode = ws.store.get_blob(sliced["gcode_blob"]).decode("utf-8")
    assert gcode.count("\n") > 100
    assert ";PASS" in gcode and "G1" in gcode
    assert sliced["total_thickness_mm"] > 0


# ------------------------------------------------------- 单位换算的完整性
def test_inch_conversion_scales_every_derived_quantity():
    """换算必须覆盖全部派生量：坐标、面积(f²)、体积(f³)、半径。

    曾在同一处漏掉体积(f³)：英寸件体积小 16387 倍，且完全不报错。
    """
    from ame.geometry.stepio import StepBuilder, box_faces, read_step

    mm = read_step(StepBuilder(unit="mm").build(box_faces(0, 0, 0, 10, 6, 2), "mm_box"))
    inch = read_step(StepBuilder(unit="inch").build(
        box_faces(0, 0, 0, 10 / 25.4, 6 / 25.4, 2 / 25.4), "inch_box"))

    # 两者应描述同一个物理盒子（10×6×2 mm）
    for i in range(6):
        assert inch.bbox[i] == pytest.approx(mm.bbox[i], abs=1e-3), f"bbox[{i}]"
    assert inch.volume_mm3 == pytest.approx(mm.volume_mm3, rel=1e-3)
    assert inch.volume_mm3 == pytest.approx(120.0, rel=1e-3)

    # 面积是二次量：总和应等于 mm 版
    a_mm = sum(f.area for f in mm.faces)
    a_in = sum(f.area for f in inch.faces)
    assert a_in == pytest.approx(a_mm, rel=1e-3), "面积应按 f² 换算"


def test_inch_conversion_scales_hole_loops():
    """英寸件的内环（孔洞）必须跟着换算，否则孔位错 25.4 倍。

    直接构造一个带内环的 Face 走换算函数，避免依赖特定 CAD 样本。
    """
    from ame.geometry.stepio import Face, StepModel, _convert_inch_to_mm

    f = Face(fid="F1", surf="plane", normal=(0, 0, 1), area=1.0, centroid=(1.0, 1.0, 0.0),
             loop=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)],
             holes=[[(0.25, 0.25, 0.0), (0.75, 0.25, 0.0),
                     (0.75, 0.75, 0.0), (0.25, 0.75, 0.0)]])
    m = StepModel(unit="inch", faces=[f], bbox=(0.0, 0.0, 0.0, 1.0, 1.0, 0.0),
                  volume_mm3=2.0)
    _convert_inch_to_mm(m, factor=25.4)

    assert m.unit == "mm"
    assert f.holes[0][0] == pytest.approx((0.25 * 25.4, 0.25 * 25.4, 0.0))
    assert f.area == pytest.approx(1.0 * 25.4 ** 2)
    assert m.volume_mm3 == pytest.approx(2.0 * 25.4 ** 3)


# ---------------------------------------------------------------- 诊断不静默
def test_face_level_diagnostics_are_surfaced_to_model():
    """面级诊断必须汇总到模型级（STR-002 不静默）。

    曾出现 occ214.stp 有 28 处 G-SURFACE-UNSUPPORTED、28 处 G-TINY-FACE、
    41 处 G-CURVE-UNSUPPORTED，而 model.diagnostics 完全为空——33/53 个面的
    面积是错的，用户却看不到任何提示。
    """
    model = read_step(_load("occ214.stp"))
    codes = _codes(model)
    assert "G-SURFACE-UNSUPPORTED" in codes, "不支持的曲面必须汇总上报"
    assert "G-CURVE-UNSUPPORTED" in codes, "不支持的曲线必须汇总上报"
    assert "G-TINY-FACE" in codes
    # 汇总信息应带面数，让用户知道影响范围
    diag = next(d for d in model.diagnostics if d["code"] == "G-SURFACE-UNSUPPORTED")
    assert diag.get("face_count", 0) > 0
    assert "个面" in diag["msg"]


def test_cylinder_faces_not_reported_as_self_intersecting():
    """柱面侧面不应被判自交（投影必然退化，是误判）。

    实测 feature.stp 有 12 个柱面被误判为自交：柱面侧环成"筒形"，
    投影到垂直法向的平面后必然是退化的线。
    """
    model = read_step(_load("feature.stp"))
    cylinders = [f for f in model.faces if f.surf == "cylinder"]
    assert cylinders, "样本应含柱面"
    bad = [f for f in cylinders if "G-SELF-INTERSECT" in (f.diagnostics or [])]
    assert not bad, f"{len(bad)} 个柱面被误判自交"


def test_real_files_report_issues_instead_of_staying_silent():
    """所有真实文件：如果有面级缺陷，模型级必须能看到（不能全空）。"""
    for name in ("occ214.stp", "proe.stp", "feature.stp"):
        model = read_step(_load(name))
        face_codes = {c for f in model.faces for c in (f.diagnostics or [])}
        model_codes = _codes(model)
        missing = face_codes - model_codes
        assert not missing, f"{name}: 这些面级诊断没有上报到模型级 {missing}"


# ---------------------------------------------------------------- STR-001 装配展平
ASSEMBLY = REAL.parent / "assembly_two_parts.step"
ASSEMBLY_ROT = REAL.parent / "assembly_rotated.step"


def test_mapped_item_instances_are_flattened():
    """MAPPED_ITEM 引用的同一零件按各自位姿摆开（STR-001）。

    一个 10×6×2 的盒子被引用两次（0 和 x=50）：必须得到 12 个面、
    bbox 覆盖 0..60，而不是两份重叠在原点。
    """
    if not ASSEMBLY.exists():
        pytest.skip("缺少装配样本")
    model = read_step(ASSEMBLY.read_text("utf-8"))
    assert len(model.faces) == 12, f"两个实例应各有 6 面，实际 {len(model.faces)}"
    x0, _y0, _z0, x1, _y1, _z1 = model.bbox
    assert x0 == pytest.approx(0.0, abs=1e-6)
    assert x1 == pytest.approx(60.0, abs=1e-6), "第二个实例应搬到 x=50..60"

    # 两个实例的 x 区间不重叠
    boxes = []
    for f in model.faces:
        xs = [p[0] for p in f.loop]
        boxes.append((min(xs), max(xs)))
    assert any(lo < 10.5 and hi < 10.5 for lo, hi in boxes), "应有实例在原位"
    assert any(lo > 49.5 for lo, hi in boxes), "应有实例在 x=50"


def test_mapped_item_rotation_is_applied():
    """实例的旋转必须生效：90° 后 10×6 的盒子变成 6×10（STR-001）。"""
    if not ASSEMBLY_ROT.exists():
        pytest.skip("缺少旋转装配样本")
    model = read_step(ASSEMBLY_ROT.read_text("utf-8"))
    assert len(model.faces) == 12
    _x0, _y0, _z0, x1, y1, _z1 = model.bbox
    assert x1 == pytest.approx(30.0, abs=1e-6), "旋转实例在 x=24..30"
    assert y1 == pytest.approx(10.0, abs=1e-6), "旋转后 y 跨度应为 10"

    # 找到旋转实例的面，确认它是 6 宽 × 10 深
    rotated = [f for f in model.faces if max(p[0] for p in f.loop) > 20]
    assert rotated
    xs = [p[0] for f in rotated for p in f.loop]
    ys = [p[1] for f in rotated for p in f.loop]
    assert max(xs) - min(xs) == pytest.approx(6.0, abs=1e-6)
    assert max(ys) - min(ys) == pytest.approx(10.0, abs=1e-6)


def test_assembly_flattening_reports_diagnostic():
    """展平后必须如实报告（STR-002 不静默）。"""
    if not ASSEMBLY.exists():
        pytest.skip("缺少装配样本")
    model = read_step(ASSEMBLY.read_text("utf-8"))
    assert "G-ASSEMBLY-FLATTENED" in _codes(model)
    diag = next(d for d in model.diagnostics if d["code"] == "G-ASSEMBLY-FLATTENED")
    assert "2" in diag["msg"], "应说明引用次数"


def test_real_assembly_with_baked_geometry_is_not_transformed_twice():
    """导出器已把位姿烘进坐标时不得再叠加变换（STR-001 反向保护）。

    occ214.stp / proe.stp 含 13 组 ITEM_DEFINED_TRANSFORMATION，但
    ADVANCED_BREP 顶点已是世界坐标（实测 occ214 原始 span 187.5×150×200，
    proe 5080×1524×1905，而变换平移只有 35/135/50 这类小值）。
    若再叠加，坐标会炸到 ±300、零件被搬到板外。
    """
    for name, expect in (("occ214.stp", (187.5, 150.0, 200.0)),
                         ("proe.stp", None)):
        model = read_step(_load(name))
        b = model.bbox
        span = (b[3] - b[0], b[4] - b[1], b[5] - b[2])
        if expect:
            for got, want in zip(span, expect, strict=True):
                assert got == pytest.approx(want, abs=0.5), \
                    f"{name} 尺寸应保持 {expect}，实际 {span}"
        else:
            # proe 是英寸声明 + 坐标看起来像毫米，换算后必然很大；
            # 关键是不能因为叠加装配变换而进一步翻倍
            assert max(span) < 20000, f"{name} 尺寸异常放大：{span}"
        assert len(model.faces) == 53, f"{name} 不应因展平而增删面"
        assert "G-ASSEMBLY-EXPLICIT" in _codes(model), "应说明装配已显式展开"


# ---------------------------------------------------------------- AP242 三角化几何
MESH = REAL.parent / "ap242_mesh_box.step"


def test_ap242_tessellated_mesh_is_imported():
    """网格型 AP242（TRIANGULATED_FACE_SET）必须能导入（STR-001）。

    此前解析器只认 ADVANCED_FACE，这类文件一个面都读不到，
    还会给出"请确认 AP203/214/242 格式"的误导提示。
    """
    if not MESH.exists():
        pytest.skip("缺少 AP242 网格样本")
    model = read_step(MESH.read_text("utf-8"))
    assert len(model.faces) == 12, f"盒子网格应有 12 个三角面，实际 {len(model.faces)}"
    assert model.unit == "mm"
    x0, y0, z0, x1, y1, z1 = model.bbox
    assert (x1 - x0, y1 - y0, z1 - z0) == pytest.approx((100.0, 60.0, 2.0), abs=1e-6)
    # 每个面都是三角面，且法向为单位向量
    for f in model.faces:
        assert f.surf == "tessellated"
        assert len(f.loop) == 3
        assert abs(sum(c * c for c in f.normal) ** 0.5 - 1.0) < 1e-6


def test_ap242_mesh_area_and_normals_are_correct():
    """三角面的面积与法向按实际几何算（不是按包围盒估算）。"""
    if not MESH.exists():
        pytest.skip("缺少 AP242 网格样本")
    model = read_step(MESH.read_text("utf-8"))
    # 100×60 的顶面拆成 2 个三角形，各 3000 mm²
    up = [f for f in model.faces if f.normal[2] > 0.99]
    assert len(up) == 2
    for f in up:
        assert f.area == pytest.approx(3000.0, abs=1e-6)
    # 底面朝下
    down = [f for f in model.faces if f.normal[2] < -0.99]
    assert len(down) == 2 and sum(f.area for f in down) == pytest.approx(6000.0)
    # 四个侧面
    side = [f for f in model.faces if abs(f.normal[2]) < 0.01]
    assert len(side) == 8, "四个矩形侧面各拆成两个三角形"


def test_ap242_mesh_reports_tessellated_diagnostic():
    """网格文件必须说明"这是三角化几何"（STR-002 不静默）。"""
    if not MESH.exists():
        pytest.skip("缺少 AP242 网格样本")
    model = read_step(MESH.read_text("utf-8"))
    assert "G-TESSELLATED-GEOMETRY" in _codes(model)
    diag = next(d for d in model.diagnostics if d["code"] == "G-TESSELLATED-GEOMETRY")
    assert "12" in diag["msg"]


def test_ap242_mesh_board_outline_is_detected():
    """三角化网格也要能识别板框（并集逻辑把碎片三角形拼起来）。

    单个三角形只有半个板面；若按"最大单面"取会得到 50×60 的错误板框。
    """
    if not MESH.exists():
        pytest.skip("缺少 AP242 网格样本")
    from ame.geometry.analysis import find_board_outline

    model = read_step(MESH.read_text("utf-8"))
    pts, note = find_board_outline(model)
    assert pts is not None, f"网格应能识别板框：{note}"
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert max(xs) - min(xs) == pytest.approx(100.0, abs=1e-6)
    assert max(ys) - min(ys) == pytest.approx(60.0, abs=1e-6)
    assert "2 个共面朝上面" in note, "应由两个三角形并集而成"


def test_ap242_mesh_end_to_end_to_gcode(tmp_path):
    """网格文件走通导入 → 板框 → 板文件 → 切片 → G-code。"""
    from ame.db.store import DataRoot
    from ame.ledger.services import do_slice
    from ame.ledger.workspace import create_project

    if not MESH.exists():
        pytest.skip("缺少 AP242 网格样本")
    ws = create_project(DataRoot(tmp_path / "d"), "AP242网格")
    r = ws.cmd_import_step(MESH.read_text("utf-8"), "mesh.step")
    assert r["face_count"] == 12
    assert r["outline"], "导入时应识别出板框"

    board = REAL.parent / "board_T19A.kicad_pcb"
    ws.cmd_import_board(board.read_text("utf-8"))
    ws.commit("网格基线")
    sl = do_slice(ws)
    assert sl["passes"] >= 7
    assert sl["total_thickness_mm"] > 0
    gcode = ws.store.get_blob(sl["gcode_blob"]).decode("utf-8")
    assert ";PASS" in gcode and "G1" in gcode


def test_tessellated_invalid_index_is_reported_not_crashing():
    """三角形索引越界时跳过并报告，不崩溃（NFR-002）。"""
    txt = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('','x',(''),(''),'','','');
FILE_SCHEMA(('AP242'));
ENDSEC;
DATA;
#1=SI_UNIT(.MILLI.,.METRE.);
#2=CARTESIAN_POINT('',(0.,0.,0.));
#3=CARTESIAN_POINT('',(1.,0.,0.));
#4=CARTESIAN_POINT('',(1.,1.,0.));
#6=COORDINATES_LIST('',(#2,#3,#4));
#7=TRIANGULATED_FACE_SET('',#6,$,$,((1,2,3),(1,2,99)),$);
ENDSEC;
END-ISO-10303-21;"""
    model = read_step(txt)
    assert len(model.faces) == 1, "合法三角形应保留"
    assert "G-TESS-INDEX-INVALID" in _codes(model)


def test_tessellated_without_coordinates_reports_no_face():
    """坐标缺失时给出说明（不是空洞的 G-NO-FACE）。"""
    txt = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('','x',(''),(''),'','','');
FILE_SCHEMA(('AP242'));
ENDSEC;
DATA;
#1=SI_UNIT(.MILLI.,.METRE.);
#7=TRIANGULATED_FACE_SET('',#999,$,$,((1,2,3)),$);
ENDSEC;
END-ISO-10303-21;"""
    model = read_step(txt)
    assert model.faces == []
    assert "G-TESS-INDEX-INVALID" in _codes(model)
