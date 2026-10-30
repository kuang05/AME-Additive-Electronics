"""走线域验收测试：T06/T07/T09 与 CIR-001..012（需求 §3.3、§7.1）。

运行：cd /d/华为 && python -m pytest tests/test_circuit.py -q
公共夹具复用 tests/conftest.py（只读，不修改）。
"""
from __future__ import annotations

import json
import re

import pytest

from ame.circuit.connectivity import (PROCESS_MIN_WIDTH, check_connectivity,
                                      jumper_candidates, total_ratline_length)
from ame.circuit.kicad import import_netlist_json
from ame.ledger.workspace import CmdError, create_project
from conftest import SAMPLES, build_project, routes_of


def all_nets(state: dict) -> list[str]:
    """mrc_state 中出现的全部网络名（焊盘网络 ∪ 走线网络）。"""
    return sorted({p["net"] for p in state["pads"] if p["net"]}
                  | {r["net"] for r in state["routes"]})


def t07_routes_fixed(pads: list[dict]) -> list[dict]:
    """routes_T07.json 走线（终点按同网络焊盘修正）。

    样例数据缺陷（samples/gen_samples.py routes_t07，不在本修复范围，仅记录）：
    其 y=40 端点 (12.2/29.2/46.2, 40) 落在左邻器件焊盘 R6.1/R7.1/R8.1
    （网络分别为 N11/N1/N3）上，原样布线会产生 N1×N11、N1×N3、N3×N5
    三处短路，且 N1/N3/N5 各断路。此处保留网络名、起点与 0.6 mm 线宽，
    终点改到同网络的另一焊盘中心——T07 验收对象是软件行为
    （布线→连通→撤销），不是样例 JSON 的坐标。
    """
    by_net: dict[str, list[dict]] = {}
    for p in pads:
        if p["net"]:
            by_net.setdefault(p["net"], []).append(p)
    fixed = []
    for r in routes_of("routes_T07.json"):
        ps = by_net[r["net"]]
        assert len(ps) == 2, f"网络 {r['net']} 应有两个焊盘"
        x0, y0 = r["pts"][0][:2]
        starts = [p for p in ps if abs(p["x"] - x0) < 1e-6 and abs(p["y"] - y0) < 1e-6]
        assert starts, f"routes_T07 起点 ({x0},{y0}) 不在网络 {r['net']} 的焊盘上"
        other = next(p for p in ps if p["id"] != starts[0]["id"])
        fixed.append({"net": r["net"], "width": r["width"],
                      "pts": [[x0, y0], [other["x"], other["y"]]]})
    return fixed


# ================================================================ T06 / CIR-001
class TestT06BoardImport:
    """T06：10 器件板文件导入，器件/焊盘/飞线数量与源文件一致（CIR-001）。"""

    def test_board10_counts_match_source(self, data_root):
        ws = build_project(data_root, board="board_10.kicad_pcb")
        src = (SAMPLES / "board_10.kicad_pcb").read_text("utf-8")
        src_comps = src.count("(footprint")
        src_pads = len(re.findall(r'\(pad "', src))
        src_nets = len(re.findall(r'^\s*\(net \d+ ', src, flags=re.M))
        assert (src_comps, src_pads, src_nets) == (10, 20, 12)

        assert len(ws.comps()) == src_comps
        assert len(ws.pad_state()) == src_pads
        assert len(ws.nets()) == src_nets
        assert len(ws.routes()) == 0          # board_10 无走线

    def test_ratline_data_queryable(self, data_root):
        """飞线数据可查：未布线网络的总飞线长度 > 0（CIR-003/005 数据基础）。"""
        ws = build_project(data_root, board="board_10.kicad_pcb")
        state = ws.mrc_state()
        total = total_ratline_length(state["pads"], state["routes"])
        assert total > 0
        # 布通一个网络后，该网络不再计入飞线
        ws.cmd_add_route("N1", [[12.2, 20], [29.2, 40]], 0.6)
        state2 = ws.mrc_state()
        assert total_ratline_length(state2["pads"], state2["routes"]) < total


class TestCIR001NetlistImport:
    """CIR-001：纯网表 + 坐标表（netlist+xy）导入；缺坐标拒绝。"""

    def test_netlist_xy_import(self, data_root):
        ws = create_project(data_root, "网表工程")
        ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                           "plate_100x60.step")
        res = ws.cmd_import_board((SAMPLES / "netlist.json").read_text("utf-8"),
                                  fmt="netlist+xy")
        assert (res["comps"], res["pads"], res["nets"]) == (1, 2, 2)
        assert sorted(n["name"] for n in ws.nets()) == ["NET1", "NET2"]
        pads = {p["pad_no"]: p for p in ws.pad_state()}
        assert pads["1"]["net"] == "NET1" and abs(pads["1"]["x"] - 28.1) < 1e-6
        assert pads["2"]["net"] == "NET2" and abs(pads["2"]["x"] - 31.9) < 1e-6
        fmt = ws.store.one("SELECT format FROM circuit")["format"]
        assert fmt == "netlist+xy"

    def test_netlist_missing_coords_rejected(self):
        """缺坐标的网表 → ValueError 且信息含“坐标”（CIR-001，§1.6-4）。"""
        bad = {"outline": [[5, 5], [95, 5], [95, 55], [5, 55]],
               "comps": [{"ref": "D1", "x": 30, "y": 30}],
               "pads": [{"ref": "D1", "pad": "1", "net": "NET1"}]}   # 缺 x/y
        with pytest.raises(ValueError, match="坐标"):
            import_netlist_json(json.dumps(bad))


# ================================================================ CIR-002
class TestCIR002Placement:
    """CIR-002：器件移动 / 锁定。"""

    def test_move_comp_moves_pads_with_it(self, data_root):
        """焊盘必须随器件平移（保持相对位置）。

        早期实现只改 comp 表，焊盘留在原地——器件与焊盘脱节后，连通性、
        MRC、切片全部基于错位坐标，而且不报错。原测试把这一缺陷写成了
        「焊盘绝对坐标不变」的期望，属于用测试固化 bug。
        """
        ws = build_project(data_root)                     # board_T19A
        comp = next(c for c in ws.comp_state() if c["ref"] == "R1")
        pads_before = [(p["pad_no"], round(p["x"], 4), round(p["y"], 4))
                       for p in ws.pad_state() if p["ref"] == "R1"]
        assert pads_before, "样本应带焊盘"

        dx, dy = 33.0 - comp["x"], 48.0 - comp["y"]
        ws.cmd_move_comp(comp["id"], 33.0, 48.0)

        after = {c["ref"]: c for c in ws.comp_state()}
        assert (after["R1"]["x"], after["R1"]["y"]) == (33.0, 48.0)
        pads_after = [(p["pad_no"], round(p["x"], 4), round(p["y"], 4))
                      for p in ws.pad_state() if p["ref"] == "R1"]
        expected = [(no, round(x + dx, 4), round(y + dy, 4))
                    for no, x, y in pads_before]
        assert pads_after == expected                     # 焊盘随器件平移

        # 撤销精确还原（不残留浮点误差）
        ws.undo()
        assert [(p["pad_no"], round(p["x"], 4), round(p["y"], 4))
                for p in ws.pad_state() if p["ref"] == "R1"] == pads_before
        assert next(c for c in ws.comp_state() if c["ref"] == "R1")["x"] == comp["x"]

    def test_locked_comp_rejects_move(self, data_root):
        ws = build_project(data_root)
        comp = next(c for c in ws.comp_state() if c["ref"] == "R1")
        ws.cmd_lock_comp(comp["id"], True)                # 锁定后生效
        with pytest.raises(CmdError, match="锁定"):
            ws.cmd_move_comp(comp["id"], 40.0, 50.0)
        ws.cmd_lock_comp(comp["id"], False)               # 解锁恢复
        assert ws.cmd_move_comp(comp["id"], 40.0, 50.0)["ok"]


# ================================================================ CIR-004 / T07
class TestCIR004ManualRoute:
    """CIR-004 / T07：二维手动布线 → 连通性通过、无短路；撤销后恢复。"""

    def test_manual_route_connectivity_and_undo(self, data_root):
        ws = build_project(data_root, board="board_10.kicad_pcb")
        routes = t07_routes_fixed(ws.pad_state())
        for r in routes:
            ws.cmd_add_route(r["net"], r["pts"], r["width"])
        assert len(ws.routes()) == 3

        state = ws.mrc_state()
        # 三维 pts z=0（二维布线，z 仅作显示/校验）
        for r in state["routes"]:
            assert all(len(p) == 3 and p[2] == 0.0 for p in r["pts"])

        conn = check_connectivity(all_nets(state), state["pads"],
                                  state["routes"], state["jumpers"])
        assert conn["shorts"] == []                       # 无短路
        opened = {o["net"] for o in conn["opens"]}
        assert opened == {"N2", "N4", "N6", "N7", "N8"}   # 未布线网络仍断路
        assert not {"N1", "N3", "N5"} & opened            # 3 个网络连通（T07）

        ws.undo()                                         # 撤销最后一条布线
        assert len(ws.routes()) == 2
        state2 = ws.mrc_state()
        conn2 = check_connectivity(all_nets(state2), state2["pads"],
                                   state2["routes"], state2["jumpers"])
        assert "N5" in {o["net"] for o in conn2["opens"]}  # 撤销后恢复断路
        assert not {"N1", "N3"} & {o["net"] for o in conn2["opens"]}
        assert conn2["shorts"] == []


# ================================================================ CIR-009
class TestCIR009Connectivity:
    """CIR-009：断路可定位；短路可检出。"""

    def test_open_located_after_route_delete(self, data_root):
        """人为断开：删除 board_T19A 的一条 imported 走线 → 报断路且 at 定位。"""
        ws = build_project(data_root)                     # board_T19A 自带 8 条走线
        state = ws.mrc_state()
        vcc = next(r for r in state["routes"] if r["net"] == "VCC")
        assert vcc["id"]
        ws.cmd_delete_route(vcc["id"])

        state2 = ws.mrc_state()
        conn = check_connectivity(all_nets(state2), state2["pads"],
                                  state2["routes"], state2["jumpers"])
        assert conn["ok"] is False
        assert conn["shorts"] == []
        opens = {o["net"]: o for o in conn["opens"]}
        assert set(opens) == {"VCC"}
        assert len(opens["VCC"]["at"]) == 2               # 失败位置可定位
        assert "断开" in opens["VCC"]["reason"] or "分裂" in opens["VCC"]["reason"]

    def test_crossing_routes_report_short(self, data_root):
        """两条异网走线十字相交 → shorts 非空。"""
        ws = create_project(data_root, "短路工程")
        ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                           "plate_100x60.step")
        ws.cmd_import_board((SAMPLES / "netlist.json").read_text("utf-8"),
                            fmt="netlist+xy")
        ws.cmd_add_route("NET1", [[20, 30], [40, 30]], 0.6)
        ws.cmd_add_route("NET2", [[31.9, 20], [31.9, 40]], 0.6)   # 与 NET1 十字相交
        state = ws.mrc_state()
        conn = check_connectivity(["NET1", "NET2"], state["pads"],
                                  state["routes"], state["jumpers"])
        assert conn["ok"] is False
        assert len(conn["shorts"]) == 1
        assert set(conn["shorts"][0]["nets"]) == {"NET1", "NET2"}
        assert len(conn["shorts"][0]["at"]) == 2


# ================================================================ CIR-010
class TestCIR010WidthConstraint:
    """CIR-010：不满足工艺下限（0.5 mm）的宽度修改被拒绝并说明下限。"""

    def test_add_route_rejects_0_3mm(self, data_root):
        assert PROCESS_MIN_WIDTH == 0.5
        ws = build_project(data_root)
        with pytest.raises(CmdError, match="0.5"):
            ws.cmd_add_route("VCC", [[22.8, 15], [77.2, 15]], 0.3)
        assert len(ws.routes()) == 8

    def test_change_width_rejects_0_3mm(self, data_root):
        ws = build_project(data_root)
        vid = next(r["id"] for r in ws.mrc_state()["routes"] if r["net"] == "VCC")
        with pytest.raises(CmdError, match="0.5"):
            ws.cmd_change_width(vid, 0.3)
        assert ws.cmd_change_width(vid, 0.8)["ok"]        # 合法宽度可改
        row = ws.store.one("SELECT width_mm FROM route WHERE id=?", (vid,))
        assert row["width_mm"] == 0.8


# ================================================================ CIR-011
class TestCIR011RouteValidity:
    """CIR-011：零长度 / 重复点 / 单点 / 自交 / 越板框均拒绝并给出原因。"""

    @pytest.mark.parametrize("pts,match", [
        ([[22.8, 15], [99, 30]], "板框"),                       # 越出板框
        ([[22.8, 15], [22.8, 15]], "零长度"),                   # 零长度段（重复点）
        ([[22.8, 15]], "两个点"),                               # 单点走线
        ([[10, 10], [50, 50], [50, 10], [10, 50]], "自交"),     # 自交折线
    ])
    def test_invalid_route_rejected(self, data_root, pts, match):
        ws = build_project(data_root)
        ws.cmd_set_outline([(5, 5), (95, 5), (95, 55), (5, 55)])   # 板框 (5,5)-(95,55)
        with pytest.raises(CmdError, match=match):
            ws.cmd_add_route("VCC", pts, 0.6)
        assert len(ws.routes()) == 8


# ================================================================ CIR-012
class TestCIR012FailureKeepsState:
    """CIR-012：任何失败的布线/修改都不产生新版本、不改变模型。"""

    def test_failed_commands_leave_history_and_routes_untouched(self, data_root):
        ws = build_project(data_root)
        ws.cmd_set_outline([(5, 5), (95, 5), (95, 55), (5, 55)])
        h0, r0 = len(ws.history()), len(ws.routes())
        c0, p0 = len(ws.comps()), len(ws.pad_state())
        vid = ws.mrc_state()["routes"][0]["id"]

        failures = [
            lambda: ws.cmd_add_route("VCC", [[22.8, 15], [77.2, 15]], 0.3),  # CIR-010
            lambda: ws.cmd_add_route("VCC", [[22.8, 15], [99, 30]], 0.6),    # 越板框
            lambda: ws.cmd_add_route("VCC", [[22.8, 15], [22.8, 15]], 0.6),  # 零长度
            lambda: ws.cmd_add_route("VCC", [[22.8, 15]], 0.6),              # 单点
            lambda: ws.cmd_add_route(  # 自交
                "VCC", [[10, 10], [50, 50], [50, 10], [10, 50]], 0.6),
            lambda: ws.cmd_add_route("不存在的网络", [[10, 10], [20, 20]], 0.6),
            lambda: ws.cmd_change_width(vid, 0.3),                           # CIR-010
        ]
        for fn in failures:
            with pytest.raises(CmdError):
                fn()

        assert len(ws.history()) == h0            # 版本号不变
        assert len(ws.routes()) == r0             # 走线数量不变
        assert len(ws.comps()) == c0 and len(ws.pad_state()) == p0


# ================================================================ CIR-007 / T09
class TestCIR007Jumper:
    """CIR-007 / T09：断开网络给出跳线候选；手动应用后连通；候选不自动应用。"""

    def test_candidates_suggest_only_then_apply(self, data_root):
        ws = build_project(data_root)                     # board_T19A
        state = ws.mrc_state()
        vcc = next(r for r in state["routes"] if r["net"] == "VCC")
        ws.cmd_delete_route(vcc["id"])                    # 人为断开 VCC

        assert ws.jumpers() == []                         # 调用候选前跳线表为空
        state2 = ws.mrc_state()
        cands = jumper_candidates(state2["pads"], state2["routes"], state2["jumpers"])
        cand = next(c for c in cands if c["net"] == "VCC")
        assert cand["from_pad"] != cand["to_pad"]
        assert cand["reason"]                             # 候选可预览（含解释）

        assert ws.jumpers() == []                         # 只建议，未自动应用
        ws.cmd_add_jumper("VCC", cand["from_pad"], cand["to_pad"],
                          reason=cand["reason"])          # 手动应用
        assert len(ws.jumpers()) == 1

        state3 = ws.mrc_state()
        conn = check_connectivity(all_nets(state3), state3["pads"],
                                  state3["routes"], state3["jumpers"])
        assert conn["ok"] is True                         # 应用后连通性通过
        assert conn["opens"] == [] and conn["shorts"] == []
