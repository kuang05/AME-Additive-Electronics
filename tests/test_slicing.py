"""切片域验收测试：T13–T16、SLC-001..012 与 NFR-001 切片性能（需求 §3.5、§7.1）。

运行：cd /d/华为 && python -m pytest tests/test_slicing.py -q
公共夹具复用 tests/conftest.py（只读，不修改）。
"""
from __future__ import annotations

import time

import pytest
from shapely.geometry import Point, Polygon

from ame.slicing.gcode import gcode_content_hash
from ame.slicing.slicer import build_schedule, build_slices, estimate, raster_fill
from ame.ledger.services import do_slice, gcode_of, latest_slice
from conftest import build_project


def params_of(ws) -> dict:
    """工艺参数视图（与 services.params_dict 同口径，剔除 id/材料外键）。"""
    p = ws.params()
    return {k: p[k] for k in p if k not in ("id", "material_a_id", "material_b_id")}


def materials_of(ws) -> tuple[dict, dict]:
    roles = {m["role"]: m for m in ws.materials()}
    return roles["conductor"], roles["dielectric"]


# ================================================================ T13 / SLC-001/002
class TestT13PassLayout:
    """T13 / SLC-001 / SLC-002：三层物理层 + 典型 7 道次，材料/层序/厚度正确。"""

    def test_seven_passes_layout(self, data_root):
        ws = build_project(data_root)          # board_T19A 样例自带连通走线
        slices = build_slices(ws.mrc_state(), params_of(ws))
        passes = slices["passes"]

        assert slices["layer_count"] == 3                       # SLC-001 三层
        assert [p["pass_no"] for p in passes] == list(range(7))
        assert [p["mat"] for p in passes] == ["B", "B", "B", "B", "A", "B", "A"]
        assert [p["kind"] for p in passes] == ["bottom"] * 3 + ["circuit"] * 2 + ["top"] * 2
        # §1.7 层厚：下保护 3×0.08；电路/上保护 0.10
        assert [p["h_mm"] for p in passes] == [0.08] * 3 + [0.1] * 4

        # z 递增：物理层之间严格递增；同层介电/银浆两道次共面（§3.5 道次表）
        zs = [p["z_mm"] for p in passes]
        assert all(a <= b for a, b in zip(zs, zs[1:], strict=False))
        assert zs[0] < zs[3] < zs[5]
        assert passes[3]["z_mm"] == passes[4]["z_mm"]
        assert passes[5]["z_mm"] == passes[6]["z_mm"]

        # 每个道次的每条路径非空且为可执行折线
        for p in passes:
            assert p["paths"], f"道次 {p['pass_no']} 无路径"
            for path in p["paths"]:
                assert len(path["pts"]) >= 2

        assert slices["connectivity"]["ok"] is True

    def test_do_slice_service_returns_seven(self, data_root):
        ws = build_project(data_root)
        r = do_slice(ws)
        assert r["passes"] == 7
        assert r["layer_count"] == 3


class TestSLC002SilverTwoFills:
    """SLC-002 变体：银浆分两次填 → 8 道次，仍 3 个物理层。"""

    def test_eight_passes_when_two_fills(self, data_root):
        ws = build_project(data_root)
        ws.cmd_set_params(silver_two_fills=1)
        slices = build_slices(ws.mrc_state(), params_of(ws))
        passes = slices["passes"]

        assert len(passes) == 8
        assert slices["layer_count"] == 3
        assert [p["mat"] for p in passes] == ["B", "B", "B", "B", "A", "A", "B", "A"]
        assert [p["kind"] for p in passes] == ["bottom"] * 3 + ["circuit"] * 3 + ["top"] * 2
        assert passes[4]["z_mm"] == passes[3]["z_mm"]           # 两次银浆填同层共面
        r = do_slice(ws)
        assert r["passes"] == 8


# ================================================================ SLC-003
class TestSLC003Params:
    """SLC-003：改速度 → 估算时间变化；改层高 → 可切片且 z 变化。"""

    def test_speed_change_alters_estimate_time(self, data_root):
        ws = build_project(data_root)
        state = ws.mrc_state()
        mat_a, mat_b = materials_of(ws)
        p1 = params_of(ws)
        est1 = estimate(build_slices(state, p1), p1, mat_a, mat_b)

        ws.cmd_set_params(speed_mm_s=40.0)
        p2 = params_of(ws)
        est2 = estimate(build_slices(state, p2), p2, mat_a, mat_b)
        assert est2["time_s"]["print"] < est1["time_s"]["print"]

    def test_layer_height_0_5_still_slices_and_z_moves(self, data_root):
        ws = build_project(data_root)
        z1 = build_slices(ws.mrc_state(), params_of(ws))["passes"][3]["z_mm"]
        ws.cmd_set_params(layer_height_mm=0.5)      # 上限边界值，仍合法
        r = do_slice(ws)
        assert r["passes"] == 7 and r["layer_count"] == 3
        slices = build_slices(ws.mrc_state(), params_of(ws))
        zs = [p["z_mm"] for p in slices["passes"]]
        assert all(a <= b for a, b in zip(zs, zs[1:], strict=False))
        assert slices["passes"][3]["z_mm"] > z1     # 层高变化 → z 变化


# ================================================================ T14 / SLC-005
class TestT14Schedule:
    """T14 / SLC-005：排程相位顺序、温度/时间与 params 一致。"""

    def test_schedule_order_and_temps(self, data_root):
        ws = build_project(data_root)
        params = params_of(ws)
        slices = build_slices(ws.mrc_state(), params)
        sched = build_schedule(params, slices)
        names = [s["phase"] for s in sched]

        assert names[0] == "打印·下保护层·介电"
        assert names[1] == "预凝胶"
        # 先介电后银浆（T14）：电路层打印介电 → 银浆 → 定型
        i_diel = names.index("打印·电路层·介电")
        i_silv = names.index("打印·电路层·银浆")
        i_set = names.index("银浆定型")
        assert i_diel < i_silv < i_set
        # 升温 → 层固化，且每个物理层固化一次
        for i, n in enumerate(names):
            if n == "升温":
                assert "层固化" in names[i + 1]
        assert names.count("升温") == names.count("层固化") == 3
        # 整板终固化 → 冷却 → 完成收尾
        assert names[-3:] == ["整板终固化", "冷却", "完成"]

        # 温度数值与 params 一致（170/80/100/60）
        for s in sched:
            if s["phase"] == "预凝胶":
                assert s["temp_c"] == params["pregel_temp_c"] == 80.0
            elif s["phase"] == "银浆定型":
                assert s["temp_c"] == params["set_temp_c"] == 100.0
            elif s["phase"] in ("升温", "层固化", "整板终固化"):
                assert s["temp_c"] == params["cure_temp_c"] == 170.0
            elif s["phase"] == "冷却":
                assert s["temp_c"] == params["cool_to_c"] == 60.0

        # 升温持续时间 = (170-80)/3℃/min × 60 = 1800 s
        ramp = (params["cure_temp_c"] - params["pregel_temp_c"]) / params["ramp_c_min"] * 60
        assert ramp == 1800.0
        for s in sched:
            if s["phase"] == "升温":
                assert s["duration_s"] == ramp
        final = next(s for s in sched if s["phase"] == "整板终固化")
        assert final["duration_s"] == params["final_cure_min"] * 60 == 900.0

        # 排程相位带 kind / pass_no（供 G-code 交错与故障按层注入）
        layer_kinds = {s["kind"] for s in sched if s["kind"] != "board"}
        assert layer_kinds == {"bottom", "circuit", "top"}
        assert {s["pass_no"] for s in sched if s["kind"] != "board"} == {2, 4, 6}
        assert all(s["kind"] == "board" for s in sched[-3:])
        ts = [s["t_start_s"] for s in sched]
        assert ts == sorted(ts)                            # 时间轴单调


# ================================================================ T15 / SLC-007
class TestT15DisconnectedBlocks:
    """T15 / SLC-007：断开网络阻断切片导出并给出定位。"""

    def test_deleted_route_blocks_slice_with_location(self, data_root):
        ws = build_project(data_root)                      # board_T19A
        state = ws.mrc_state()
        vcc = next(r for r in state["routes"] if r["net"] == "VCC")
        ws.cmd_delete_route(vcc["id"])

        slices = build_slices(ws.mrc_state(), params_of(ws))
        assert slices["connectivity"]["ok"] is False
        opens = slices["connectivity"]["opens"]
        assert opens and len(opens[0]["at"]) == 2

        with pytest.raises(RuntimeError, match="连通性") as ei:
            do_slice(ws)
        msg = str(ei.value)
        assert "定位" in msg and "定位 ?" not in msg        # 定位信息非空


# ================================================================ T16 / SLC-008
class TestT16Gcode:
    """T16 / SLC-008：G-code 哈希一致、材料批次、道次/烘箱标记完整。"""

    def test_deterministic_hash_marks_and_content_hash(self, data_root):
        ws = build_project(data_root)
        r1 = do_slice(ws)
        r2 = do_slice(ws)                                  # 同一输入连续导出两次
        assert r1["gcode_hash"] == r2["gcode_hash"]        # 哈希一致

        g = gcode_of(ws, r1["slice_set_id"])
        row = ws.store.one("SELECT gcode_hash, gcode_blob FROM slice_set WHERE id=?",
                           (r1["slice_set_id"],))
        assert row["gcode_blob"]                           # 文件 blob 单独存储
        assert gcode_content_hash(g) == row["gcode_hash"]  # 内容哈希与存储一致
        assert g.rstrip().splitlines()[-1].startswith(";gcode_sha256")

        assert ";material A" in g and "AG-2026-10" in g    # 材料批次号（SLC-008）
        assert ";material B" in g and "EP-2026-10" in g
        assert ";PASS " in g and g.count(";PASS_END") == 7  # 道次标记
        assert ";CURE" in g and "G4 P" in g                 # 烘箱阶段含停留

        # 按物理层交错：底层最后一个道次打印完 → 该层预凝胶/升温/层固化
        assert g.index(";PASS 2 ") < g.index(";CURE 预凝胶") < g.index(";PASS 3 ")
        # 整板终固化与冷却在全部道次之后
        assert g.rindex(";PASS_END") < g.index(";CURE 整板终固化") < g.index(";CURE 冷却")
        assert g.count(";CURE 预凝胶") == 3                 # 每物理层一次
        assert g.count(";CURE 银浆定型") == 2               # 电路层/上保护层
        assert g.count(";CURE 层固化") == 3

    def test_width_change_changes_hash(self, data_root):
        ws = build_project(data_root)
        r1 = do_slice(ws)
        vid = next(r["id"] for r in ws.mrc_state()["routes"] if r["net"] == "VCC")
        ws.cmd_change_width(vid, 0.8)                      # 改一条走线宽度
        r3 = do_slice(ws)
        assert r3["gcode_hash"] != r1["gcode_hash"]        # 换参数后哈希变化


# ================================================================ SLC-009
class TestSLC009Estimate:
    """SLC-009：估算给出长度/时间/体积/成本并标注假设。"""

    def test_estimate_fields(self, data_root):
        ws = build_project(data_root)
        r = do_slice(ws)
        est = r["estimate"]
        assert est["assumptions"]                          # 假设说明非空
        assert est["length_mm"]["A"] > 0 and est["length_mm"]["B"] > 0
        assert est["volume_ml"]["A"] > 0 and est["volume_ml"]["B"] > 0
        assert set(est["time_s"]) >= {"print", "cure", "first_ramp", "steady_total"}
        assert est["time_s"]["print"] > 0 and est["time_s"]["cure"] > 0
        assert est["cost_cny"] > 0


# ================================================================ SLC-010
class TestSLC010Stale:
    """SLC-010：设计变更后旧切片标记过期。"""

    def test_commit_marks_slice_stale(self, data_root):
        ws = build_project(data_root)
        r = do_slice(ws)
        assert latest_slice(ws) is not None
        ws.commit("改走线")
        assert latest_slice(ws) is None                    # 有效切片查询为空
        row = ws.store.one("SELECT stale FROM slice_set WHERE id=?",
                           (r["slice_set_id"],))
        assert row["stale"] == 1                           # 旧切片 stale=1


# ================================================================ SLC-011
class TestSLC011ConfigGuard:
    """SLC-011/SLC-012：材料停用与参数越界拒绝切片并说明原因。"""

    def test_disabled_material_blocks_slice(self, data_root):
        ws = build_project(data_root)
        ws.store.execute("UPDATE material_profile SET enabled=0 WHERE role='conductor'")
        with pytest.raises(RuntimeError, match="材料"):
            do_slice(ws)

    def test_layer_height_out_of_range_blocks_slice(self, data_root):
        """SLC-011/SLC-012：层高越界时命令层直接拒绝；绕过命令层写入的脏数据
        由切片层第二道防线阻断——两条路径都覆盖。"""
        from ame.ledger.workspace import CmdError
        ws = build_project(data_root)
        ws.store.execute("UPDATE material_profile SET enabled=1 WHERE role='conductor'")
        with pytest.raises(CmdError, match="越界"):
            ws.cmd_set_params(layer_height_mm=5.0)         # 命令层拦截
        ws.store.execute("UPDATE process_params SET layer_height_mm=5.0")
        with pytest.raises(RuntimeError, match="越界"):
            do_slice(ws)                                   # 切片层拦截


# ================================================================ SLC-004 / SLC-012
class TestSLC012RasterFill:
    """SLC-004/012：光栅填充路径全部落在板框多边形内。"""

    @pytest.mark.parametrize("spacing,angle", [(0.6, 0), (0.6, 45), (0.4, 45)])
    def test_fill_lines_inside_outline(self, spacing, angle):
        board = Polygon([(5, 5), (95, 5), (95, 55), (5, 55)])
        rows = raster_fill(board, spacing, angle_deg=angle)
        assert rows
        checked = 0
        for row in rows:
            assert len(row) >= 2
            for x, y in row:
                assert board.covers(Point(x, y)), f"填充点 ({x},{y}) 越出板框"
                checked += 1
        assert checked > 50


# ================================================================ NFR-001 性能
class TestNFR001SlicePerformance:
    """NFR-001：200×200 单层板切片 ≤120 s（基准机器口径，放宽断言上限）。"""

    def test_200x200_slice_within_120s(self, data_root):
        ws = build_project(data_root)                      # board_T19A 走线
        ws.cmd_set_outline([(0, 0), (200, 0), (200, 200), (0, 200)])
        t0 = time.perf_counter()
        r = do_slice(ws)
        elapsed = time.perf_counter() - t0
        assert r["passes"] == 7 and r["layer_count"] == 3
        assert elapsed < 120, f"200×200 切片耗时 {elapsed:.1f}s 超出 NFR-001 上限"


# ================================================================ SLC-004 板框孔洞
class TestBoardHoles:
    """SLC-004/STR-004：板框内孔（螺丝孔等）不应被实心填充。"""

    def test_slicing_skips_hole_region(self, data_root):
        import json

        from shapely.geometry import Point, Polygon

        from conftest import build_project
        ws = build_project(data_root, name="孔洞")
        ws.cmd_set_outline([(0, 0), (100, 0), (100, 60), (0, 60)])
        hole = [[40, 20], [60, 20], [60, 40], [40, 40]]
        ws.store.execute("UPDATE structure SET holes_json=?", (json.dumps([hole]),))
        ws.invalidate()
        assert ws.board_holes() == [hole]
        sl = do_slice(ws)
        hp = Polygon(hole)
        p0 = sl["pass_details"][0]
        inside = sum(1 for p in p0["paths"] for pt in p["pts"]
                     if hp.contains(Point(pt[0], pt[1])))
        assert inside == 0, f"孔洞区域被填充了 {inside} 个点"

    def test_holes_recorded_on_import(self, data_root):
        """STR-004：导入 STEP 时孔洞应落库（holes_json）。"""
        from conftest import build_project
        ws = build_project(data_root, name="导入孔洞", step="plate_100x60.step")
        # 无孔模型 -> 空列表
        assert ws.board_holes() == []
        row = ws.store.one("SELECT holes_json FROM structure")
        assert row["holes_json"] == "[]"

    def test_mrc_state_exposes_holes(self, data_root):
        """切片/MRC 共用的状态视图应带孔洞信息。"""
        import json

        from conftest import build_project
        ws = build_project(data_root, name="状态孔洞")
        ws.cmd_set_outline([(0, 0), (100, 0), (100, 60), (0, 60)])
        ws.store.execute("UPDATE structure SET holes_json=?",
                         (json.dumps([[[45, 25], [55, 25], [55, 35], [45, 35]]]),))
        ws.invalidate()
        st = ws.mrc_state()
        assert st.get("holes") and len(st["holes"][0]) == 4


# ================================================================ SLC-009 估算准确性
class TestSLC009Accuracy:
    """SLC-009：估算与虚拟打印偏差需在 ±30% 内（排除首次升温）。"""

    def test_estimate_matches_virtual_print(self, data_root):
        """估算时间必须与虚拟回放一致（曾因固化时间被 cure+dwell 重复累加，偏差 +947%）。"""
        from ame.ledger.services import run_print
        from conftest import build_project

        ws = build_project(data_root, name="估算一致")
        ws.commit("导入")
        sl = do_slice(ws)
        job = run_print(ws)
        est = sl["estimate"]["time_s"]
        # 总时长口径
        expect_total = est["print"] + est["cure"]
        dev = abs(job["elapsed_s"] / expect_total - 1)
        assert dev < 0.30, (f"估算 {expect_total:.0f}s vs 回放 {job['elapsed_s']:.0f}s，"
                            f"偏差 {dev:.0%} 超出 ±30%")
        # 排除首次升温口径（SLC-009 明确规定）
        steady_actual = job["elapsed_s"] - est["first_ramp"]
        dev2 = abs(steady_actual / est["steady_total"] - 1)
        assert dev2 < 0.30, f"稳态偏差 {dev2:.0%} 超出 ±30%"

    def test_cure_time_not_double_counted(self, data_root):
        """固化时间不得被 G-code 的 ;CURE 与 G4 各算一次（重复累加会让总时长翻倍）。"""
        from ame.ledger.services import run_print
        from conftest import build_project

        ws = build_project(data_root, name="固化不重复")
        ws.commit("导入")
        sl = do_slice(ws)
        job = run_print(ws)
        est = sl["estimate"]["time_s"]
        # 回放时长不应接近"估算的 2 倍"（那是重复累加的特征）
        ratio = job["elapsed_s"] / (est["print"] + est["cure"])
        assert ratio < 1.5, f"回放 {job['elapsed_s']:.0f}s 约为估算 {est['print']+est['cure']:.0f}s 的 {ratio:.1f} 倍，疑似重复累加"

    def test_gcode_has_retract_before_travel(self, data_root):
        """SLC-008/PRT-004：空移前必须有回抽（浆料不回抽会拖尾）。"""
        from ame.ledger.services import gcode_of, latest_slice
        from conftest import build_project

        ws = build_project(data_root, name="回抽")
        ws.commit("导入")
        do_slice(ws)
        g = gcode_of(ws, latest_slice(ws)["id"])
        assert "M83" in g, "应声明相对挤出（回抽用负 E）"
        assert "回抽" in g and "恢复出料" in g
        assert g.count("回抽") > 10, "每条路径前都应回抽"
        assert "抬针" in g and "落针" in g, "跨区域移动应抬针"

    def test_gcode_has_resume_safe_points(self, data_root):
        """PRT-014：每个道次结束写安全点标记，供断电续打定位。"""
        from ame.ledger.services import gcode_of, latest_slice
        from conftest import build_project

        ws = build_project(data_root, name="安全点")
        ws.commit("导入")
        sl = do_slice(ws)
        g = gcode_of(ws, latest_slice(ws)["id"])
        n = g.count(";SAFE_POINT")
        assert n == sl["passes"], f"安全点 {n} 个应等于道次数 {sl['passes']}"
        assert ";TOTAL_LENGTH" in g and ";TOTAL_PASSES" in g


# ================================================================ 切片质量（对标标杆切片器）
class TestSliceQuality:
    """对标 CuraEngine/PrusaSlicer 的质量约束。"""

    def test_serpentine_fill_has_low_travel(self, data_root):
        """SLC-004：蛇形填充的段顺序必须随行反向（否则每行末尾跳回左侧）。

        原 bug：flip 只反转段内点序、不反转段顺序，有孔洞的行被切成多段后
        产生整行宽空移（实测空移占填充 20.5%）。修复后应 < 5%。
        """
        import math

        from conftest import build_project
        ws = build_project(data_root, name="蛇形")
        ws.cmd_set_outline([(0, 0), (100, 0), (100, 60), (0, 60)])
        ws.commit("导入")
        sl = do_slice(ws)
        total_print = total_travel = 0.0
        for p in sl["pass_details"]:
            prev = None
            for path in p["paths"]:
                pts = path["pts"]
                total_print += sum(math.dist(pts[i], pts[i + 1])
                                   for i in range(len(pts) - 1))
                if prev is not None:
                    total_travel += math.dist(prev, pts[0])
                prev = pts[-1]
        ratio = total_travel / max(total_print, 1e-6)
        assert ratio < 0.05, f"空移占打印 {ratio:.1%} 过高（蛇形段序可能未修复）"

    def test_fill_does_not_touch_board_edge(self, data_root):
        """SLC-004：填充中心线必须内缩，否则半线宽浆料溢出板框。"""
        from shapely.geometry import Point, Polygon

        from conftest import build_project
        ws = build_project(data_root, name="内缩")
        ws.cmd_set_outline([(0, 0), (100, 0), (100, 60), (0, 60)])
        ws.commit("导入")
        sl = do_slice(ws)
        board = Polygon([(0, 0), (100, 0), (100, 60), (0, 60)])
        p0 = sl["pass_details"][0]
        fills = [x for x in p0["paths"] if x["role"] == "fill"]
        assert fills
        outside = [pt for f in fills for pt in f["pts"]
                   if not board.covers(Point(pt[0], pt[1]))]
        assert not outside, f"{len(outside)} 个填充点越出板框"
        # 不应有大量点正好贴在边界上（内缩生效）
        on_edge = [pt for f in fills for pt in f["pts"]
                   if pt[0] <= 0.01 or pt[0] >= 99.99
                   or pt[1] <= 0.01 or pt[1] >= 59.99]
        assert len(on_edge) == 0, f"{len(on_edge)} 个填充点贴板框边界（未内缩）"

    def test_fill_spacing_uses_rounded_rect_formula(self):
        """SLC-003：线距按圆角矩形挤出公式 = 线宽 - 层厚×(1-π/4)。"""
        import math

        from ame.slicing.slicer import fill_spacing

        assert abs(fill_spacing(0.6, 0.1) - (0.6 - 0.1 * (1 - math.pi / 4))) < 1e-9
        # 层厚趋 0 时趋近线宽
        assert abs(fill_spacing(0.6, 0.0) - 0.6) < 1e-9
        # 不因层厚过大而低于线宽一半（下限保护）
        assert fill_spacing(0.6, 10.0) >= 0.3


# ================================================================ NFR-002 输入健壮性
_BASE_PARAMS = {
    "layer_height_mm": 0.1, "speed_mm_s": 20, "extrusion_factor": 1.0,
    "pregel_temp_c": 80, "set_temp_c": 120, "cure_temp_c": 160,
    "pregel_s": 60, "set_s": 60, "cure_s": 300, "ramp_c_per_min": 5,
    "cool_temp_c": 40, "cure_mode": "layer", "bottom_layer_mm": 0.08,
    "bottom_passes": 3,
}


@pytest.mark.parametrize("name,state", [
    ("空 state", {}),
    ("outline 单点", {"outline": [[0, 0]]}),
    ("outline 为 None", {"outline": None}),
    ("routes 是字符串", {"outline": [[0, 0], [10, 0], [10, 10], [0, 10]],
                        "routes": "x"}),
    ("pads 缺坐标", {"outline": [[0, 0], [10, 0], [10, 10], [0, 10]],
                    "pads": [{}]}),
])
def test_nfr002_build_slices_rejects_or_tolerates_bad_input(name, state):
    """NFR-002：切片的坏输入要么给出**可读**错误，要么容错继续。

    反例（修复前）：空 state 抛 KeyError('outline')、outline 单点抛
    "A linearring requires at least 4 coordinates"——这两种消息传到界面
    上等于没解释。缺板框时必须说"缺板框、去哪设"，这是 UI-005 的要求。
    """
    from ame.slicing.slicer import build_slices

    try:
        res = build_slices(state, _BASE_PARAMS)
    except ValueError as e:
        msg = str(e)
        assert any("\u4e00" <= c <= "\u9fff" for c in msg), \
            f"{name}: 错误信息必须是中文可读：{msg}"
        assert "板框" in msg or "参数" in msg, \
            f"{name}: 错误信息应指明缺什么：{msg}"
    else:
        assert res["passes"], f"{name}: 容错路径也应产出道次"


def test_nfr002_missing_params_reports_which_one():
    """缺关键参数时明确指出缺哪一项，而不是 KeyError。"""
    from ame.slicing.slicer import build_slices

    state = {"outline": [[0, 0], [10, 0], [10, 10], [0, 10]]}
    with pytest.raises(ValueError) as ei:
        build_slices(state, {})
    assert "layer_height_mm" in str(ei.value)


# ================================================================ 层间填充方向交替
def test_fill_direction_alternates_between_bottom_passes():
    """下保护层的相邻道次必须交替 45°/135°（增材制造标准做法）。

    为什么重要：三个道次若同向填充，层界面上的丝走向平行，形成"弱面"——
    受剪切时沿界面剥离。交替 90° 后上层丝跨越下层丝，界面强度显著提高。
    这是 FDM/SLS/SLA 的通用工艺常识，也是 CuraEngine/PrusaSlicer 的默认行为。

    路径总长完全相同（实测 170 行 / 7721 mm），所以交替不增加任何时间或材料。
    """
    import math

    from ame.slicing.slicer import build_slices

    state = {
        "outline": [[5, 5], [95, 5], [95, 55], [5, 55]],
        "routes": [], "pads": [], "comps": [], "obstacles": [],
        "faces": [], "holes": [], "jumpers": [],
    }
    slices = build_slices(state, _BASE_PARAMS)
    bottoms = [p for p in slices["passes"] if p["kind"] == "bottom"]
    assert len(bottoms) >= 2, "默认应有 3 个下保护道次"

    # 相邻道次角度必须不同（45 vs 135 互为镜像，模 180 后都是 45，
    # 所以按原始符号判断：atan2 给 -45 与 -135，取绝对值区分）
    raws = []
    for p in bottoms:
        fills = [x for x in p["paths"] if x["role"] == "fill"]
        pts = fills[0]["pts"]
        dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
        raws.append(round(math.degrees(math.atan2(dy, dx))))
    for a, b in zip(raws, raws[1:], strict=False):
        assert abs(a - b) == 90, f"相邻道次填充方向应相差 90°，实际 {a}° vs {b}°"


def test_fill_alternation_keeps_total_path_length():
    """交替方向不改变路径总长（这是"不增加成本"的依据）。"""
    from ame.slicing.slicer import build_slices

    state = {"outline": [[5, 5], [95, 5], [95, 55], [5, 55]],
             "routes": [], "pads": [], "comps": [], "obstacles": [],
             "faces": [], "holes": [], "jumpers": []}
    slices = build_slices(state, _BASE_PARAMS)
    lengths = []
    for p in slices["passes"]:
        if p["kind"] != "bottom":
            continue
        total = 0.0
        for path in p["paths"]:
            pts = path["pts"]
            total += sum(
                ((pts[i][0] - pts[i - 1][0]) ** 2
                 + (pts[i][1] - pts[i - 1][1]) ** 2) ** 0.5
                for i in range(1, len(pts)))
        lengths.append(total)
    assert len(lengths) >= 2
    # 各道次路径总长应几乎相等（同一区域、同一线距，只差方向）
    assert max(lengths) - min(lengths) < 1e-6, f"总长不一致：{lengths}"


# ================================================================ 自适应行距（消除边缘 underfill）
class TestAdaptiveRowSpacing:
    """填充扫描线按行数均分，边缘不留空隙。

    背景：固定步长时若区域高度不是线距整数倍，最后一行与边界会留
    0～一个线距的空隙（实测 20×3 mm 留 0.107 mm、20×7.1 留 0.158 mm），
    成品边缘出现未填充带。增材文献称之为 underfill
    （Steuben et al., arXiv:2004.13497 指出等宽路径在边缘与中心都有 over/underfill）。
    """

    @pytest.mark.parametrize("height", [3.0, 7.1, 10.35, 5.7, 60.0])
    def test_edge_has_no_gap(self, height):
        from shapely.geometry import LineString, box
        from shapely.ops import unary_union

        from ame.slicing.raster import fill_spacing, raster_fill

        sp = fill_spacing(0.6, 0.08)
        region = box(0, 0, 20, height)
        rows = raster_fill(region, sp, angle_deg=0)
        assert rows, "非空区域必须有填充"

        tubes = [LineString([(p[0], p[1]) for p in r]).buffer(0.3)
                 for r in rows if len(r) >= 2]
        cov = unary_union(tubes)
        inside = cov.intersection(region).area
        ratio = inside / region.area
        assert ratio > 0.97, f"高度 {height} 时覆盖率仅 {ratio:.1%}（边缘有未填充带）"

    def test_row_count_matches_height_over_spacing(self):
        """行数应约等于 高度/线距（自适应只微调行距，不改变行数数量级）。"""
        from shapely.geometry import box

        from ame.slicing.raster import fill_spacing, raster_fill

        sp = fill_spacing(0.6, 0.08)
        for h in (10.0, 20.0, 60.0):
            rows = raster_fill(box(0, 0, 50, h), sp, angle_deg=0)
            expect = round(h / sp)
            assert abs(len(rows) - expect) <= 2, \
                f"高度 {h}：行数 {len(rows)}，预期约 {expect}"

    def test_spacing_deviation_is_bounded(self):
        """自适应行距相对标称线距的偏差必须 ≤20%（否则挤出量对不上）。"""
        from shapely.geometry import box

        from ame.slicing.raster import fill_spacing, raster_fill

        sp = fill_spacing(0.6, 0.08)
        for h in (3.0, 7.1, 10.35, 13.7):
            rows = raster_fill(box(0, 0, 20, h), sp, angle_deg=0)
            ys = sorted({round(r[0][1], 6) for r in rows})
            if len(ys) < 2:
                continue
            actual = (ys[-1] - ys[0]) / (len(ys) - 1)
            dev = abs(actual - sp) / sp
            assert dev <= 0.20, f"高度 {h}：实际行距 {actual:.4f} 偏离标称 {sp:.4f} 达 {dev:.0%}"


class TestPerPathVolume:
    """体积必须按每条路径自己的线宽累加（不能用统一线宽）。"""

    def test_volume_uses_actual_path_width(self, data_root):
        """电路层混有 0.3 与 0.6 mm 路径时，按 0.6 统一算会高估一倍。"""
        from conftest import build_project

        from ame.ledger.services import do_slice

        ws = build_project(data_root, name="体积口径")
        ws.commit("导入")
        sl = do_slice(ws)

        # 确认确实存在细路径（否则这个用例没有区分力）
        widths = {x["width_mm"] for p in sl["pass_details"] for x in p["paths"]}
        assert any(w < 0.5 for w in widths), f"样本应含细路径，实际线宽 {widths}"

        # 独立复算：按每条路径自己的宽度
        lh = ws.params()["layer_height_mm"]
        flow = ws.params().get("flow_factor", 1.0)
        vol_a_mm3 = 0.0
        for p in sl["pass_details"]:
            if p["mat"] != "A":
                continue
            for x in p["paths"]:
                pts = x["pts"]
                ln = sum(
                    ((pts[i][0] - pts[i - 1][0]) ** 2
                     + (pts[i][1] - pts[i - 1][1]) ** 2) ** 0.5
                    for i in range(1, len(pts)))
                vol_a_mm3 += ln * x["width_mm"] * lh * flow
        # estimate 返回值保留 4 位小数，容差按舍入误差取
        assert sl["estimate"]["volume_ml"]["A"] == pytest.approx(
            vol_a_mm3 / 1000, abs=1e-4)


# ================================================================ 路径串接（空移重排）
class TestChainPaths:
    """`chain_paths` 的空移改善必须真实生效。

    这条测试存在的理由：该函数曾长期形同虚设——shapely 2.1 的
    `STRtree.query_nearest` 只返回单个索引，原实现假设"等距时返回多个"，
    遇到"最近端点属于已用路径"就退化到顺序选择，等价于没优化。
    而当时唯一的断言是"空移 <5%"，单板蛇形本来就低，所以失效未被发现。
    这里用空间分离的岛（蛇形行之间必然远跳）断言改善幅度。
    """

    @staticmethod
    def _islands(n: int):
        from itertools import product

        from shapely.geometry import box
        from shapely.ops import unary_union
        cells = list(product(range(3), range(2)))[:n]
        return unary_union([box(x * 30, y * 30, x * 30 + 10, y * 30 + 10)
                            for x, y in cells])

    @staticmethod
    def _travel(paths) -> float:
        import math
        return sum(math.dist(paths[i - 1]["pts"][-1][:2], paths[i]["pts"][0][:2])
                   for i in range(1, len(paths)))

    def _rows(self, n: int):
        from ame.slicing.raster import fill_region, fill_spacing, raster_fill
        area = fill_region(self._islands(n), 0.6, 0.08)
        rows = raster_fill(area, fill_spacing(0.6, 0.08), angle_deg=0)
        return [{"seq": i + 1, "role": "fill", "width_mm": 0.6,
                 "speed_mm_s": 20, "pts": r} for i, r in enumerate(rows)]

    @pytest.mark.parametrize("islands,min_gain", [(3, 0.5), (6, 0.7)])
    def test_reduces_travel_on_separated_islands(self, islands, min_gain):
        """多岛板件的空移必须显著下降（不是 0%）。"""
        from ame.slicing.raster import chain_paths
        paths = self._rows(islands)
        before = self._travel(paths)
        after = self._travel(chain_paths(paths))
        assert before > 100, "构造的岛间距不足以产生空移，测试失效"
        gain = 1 - after / before
        assert gain >= min_gain, (
            f"{islands} 岛空移 {before:.0f}→{after:.0f} mm，仅改善 {gain:.0%}"
            f"（<{min_gain:.0%}），chain_paths 可能又退化成顺序选择")

    def test_keeps_every_path_exactly_once(self):
        """重排不得丢失、重复或改写路径内容。"""
        from ame.slicing.raster import chain_paths
        paths = self._rows(6)
        out = chain_paths(paths)
        assert len(out) == len(paths)
        assert sorted(p["seq"] for p in out) == sorted(p["seq"] for p in paths)
        by_seq = {p["seq"]: p["pts"] for p in paths}
        for p in out:
            forward = p["pts"] == by_seq[p["seq"]]
            backward = p["pts"] == list(reversed(by_seq[p["seq"]]))
            assert forward or backward, f"路径 {p['seq']} 被改写（应只允许反向）"

    def test_single_board_serpentine_not_degraded(self):
        """单板蛇形本已最优，重排不得让它变差。"""
        from shapely.geometry import box

        from ame.slicing.raster import chain_paths, fill_region, fill_spacing, raster_fill
        area = fill_region(box(0, 0, 100, 100), 0.6, 0.08)
        rows = raster_fill(area, fill_spacing(0.6, 0.08), angle_deg=45)
        paths = [{"seq": i + 1, "role": "fill", "width_mm": 0.6,
                  "speed_mm_s": 20, "pts": r} for i, r in enumerate(rows)]
        before = self._travel(paths)
        after = self._travel(chain_paths(paths))
        assert after <= before * 1.01 + 1e-9, (
            f"单板蛇形空移从 {before:.0f} 涨到 {after:.0f} mm")

    def test_degenerate_paths_are_preserved(self):
        """少于 2 点的退化路径不参与排程，但必须原样保留在输出里。"""
        from ame.slicing.raster import chain_paths
        paths = self._rows(2)
        paths.append({"seq": 999, "role": "fill", "width_mm": 0.6,
                      "speed_mm_s": 20, "pts": []})
        out = chain_paths(paths)
        assert any(p["seq"] == 999 for p in out)


# ================================================================ 填充兜底判据的容差
class TestSerpentineFallbackTolerance:
    """批量求交已裁好的路径，旋回原坐标系后不应再跑一次精确求交。

    回归背景：`_serpentine_rows` 用 `target.covers(line)` 判断路径是否已在
    边界内，但旋转回原坐标会引入 1e-14 级浮点偏移，端点恰好落在边界上时
    `covers` 返回 False。实测 **任何 45°/135° 旋转都会命中 72–97% 的路径**
    （`difference(target)` 长度只有 1e-13 mm，纯浮点噪声），每次白跑一遍
    完整 intersection——占切片总耗时约 15%。
    """

    @staticmethod
    def _rate(angle: int, size=(200, 200)) -> tuple[int, int, float]:
        """复刻 raster_fill 的调用链，统计 covers 失败率与最大超出量。"""
        import math

        from shapely.affinity import rotate
        from shapely.geometry import LineString, Polygon

        from ame.slicing.raster import (fill_region, fill_spacing,
                                        _rows_by_scanline, _scanline_positions)
        w, h = size
        target = fill_region(Polygon([(0, 0), (w, 0), (w, h), (0, h)]), 0.6, 0.08)
        spacing = fill_spacing(0.6, 0.08)
        work = rotate(target, angle, origin=(0, 0), use_radians=False) if angle else target
        scanlines, ys = _scanline_positions(work, spacing)
        by_row = _rows_by_scanline(work, scanlines, ys)

        def rp(x, y, sign=1.0):
            a = math.radians(angle)
            ca, sa = math.cos(sign * a), math.sin(sign * a)
            return (x * ca - y * sa, x * sa + y * ca)

        tot = bad = 0
        worst = 0.0
        for idx in range(len(ys)):
            for col in sorted(by_row.get(idx, []), key=lambda c: c[0][0]):
                c2 = [rp(x, yy, -1.0) for x, yy in col] if angle else col
                line = LineString(c2)
                if line.length <= 1e-9:
                    continue
                tot += 1
                if not target.covers(line):
                    bad += 1
                    worst = max(worst, getattr(line.difference(target), "length", 0.0))
        return bad, tot, worst

    @pytest.mark.parametrize("angle", [0, 45, 135])
    def test_rotated_paths_survive_tolerance(self, angle):
        """容差判据下，旋转引入的浮点偏移不再被当成越界。"""
        bad, tot, worst = self._rate(angle)
        assert tot > 0, "构造无路径，测试失效"
        # 旧实现下 45°/135° 的失败率是 72–97%；容差判据应为 0 次真实越界
        assert worst < 1e-6, f"存在超过容差的真实越界：{worst:.2e} mm"

    def test_old_criterion_would_fail_many_paths(self):
        """证明旧判据（无容差 covers）确实会大面积误判——防止有人把容差删掉。

        这条测试断言的是"旧行为有多糟"，因此它读的是原始 target，不走修复路径。
        """
        bad45, tot45, worst45 = self._rate(45)
        assert bad45 / tot45 > 0.5, (
            f"45° 下无容差 covers 只失败 {bad45}/{tot45}，与实测的 72%+ 不符——"
            "可能切片几何已变化，请复核兜底容差是否仍有必要")
        assert worst45 < 1e-6, "这些失败本应只是浮点噪声"

    @pytest.mark.parametrize("angle", [45, 135])
    def test_no_reclipping_after_fix(self, angle):
        """最终验收：走真实 raster_fill，兜底 intersection 次数必须为 0。

        与上面两条的参数化测试不同，这条断言的是**实现内部**的行为
        （是否真的省掉了 intersection），因此能抓住"容差被删掉"的回退。
        """
        from shapely.geometry import Polygon

        import ame.slicing.raster as R

        calls = {"n": 0}
        original_intersection = R.LineString.intersection

        def counting(self, other, *a, **kw):
            calls["n"] += 1
            return original_intersection(self, other, *a, **kw)

        target = R.fill_region(Polygon([(0, 0), (200, 0), (200, 200), (0, 200)]),
                               0.6, 0.08)
        R.LineString.intersection = counting
        try:
            rows = R.raster_fill(target, R.fill_spacing(0.6, 0.08), angle_deg=angle)
        finally:
            R.LineString.intersection = original_intersection
        assert rows, "填充不应为空"
        assert calls["n"] == 0, (
            f"{angle}° 下仍有 {calls['n']} 次兜底 intersection"
            "（已裁剪路径只因浮点噪声被重复求交）")

    def test_real_overflow_still_clipped(self):
        """真实越界（远超容差）仍必须被裁掉，不能因为放宽判据而漏裁。"""
        from shapely.geometry import box

        from ame.slicing.raster import _serpentine_rows

        target = box(0, 0, 10, 10)
        by_row = {0: [[(-5.0, 5.0), (15.0, 5.0)]]}
        rows = _serpentine_rows(by_row, [5.0], lambda x, y, sign=1.0: (x, y), 0.0, target)
        assert rows, "越界路径应被裁剪后保留中段"
        for row in rows:
            for x, y in row:
                assert -1e-6 <= x <= 10 + 1e-6, f"裁剪后仍有越界点 x={x}"
                assert -1e-6 <= y <= 10 + 1e-6, f"裁剪后仍有越界点 y={y}"

    def test_fix_removes_reclipping_in_full_slice(self, data_root):
        """端到端：完整切片里不应再有"只因浮点噪声"而触发精确求交的路径。"""
        from conftest import build_project

        from ame.ledger.services import do_slice

        ws = build_project(data_root, name="兜底端到端")
        ws.cmd_set_outline([(0, 0), (100, 0), (100, 60), (0, 60)])
        ws.commit("导入")
        sl = do_slice(ws)
        # 切片成功且有填充路径（修复不能把路径裁没）
        fills = [p for pd in sl["pass_details"] for p in pd["paths"]
                 if p["role"] == "fill"]
        assert len(fills) > 50, f"填充路径过少（{len(fills)}），裁剪可能过严"
