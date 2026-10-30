"""打印域验收测试（T17/T18；PRT-002/003/004/005/006/007/008/009/011/014）。

覆盖：
- T17/PRT-006：虚拟打印按 G-code 回放，进度、层号、时间一致，无故障时缺陷为空；
- PRT-002：打印前检查（连接/归零/温度/材料/哈希/标定），不满足禁止开始并给中文原因；
- PRT-003/004：两种暂停（打印中=抬针 / 固化中=恒温），恢复后回到对应状态；
- PRT-005/007/T18：六种故障按道次+位置注入，生成可复现缺陷（层/位置正确）；
- PRT-008/009：缺陷记录（类型/严重度/证据/状态）与归因（设计/工艺/设备 + 回滚建议）；
- PRT-011：急停后状态 cancelled、事件保留、加热切断；
- PRT-014：断电续打——安全点续打、哈希不符拒绝、无安全点放弃。

全部本地确定性运行（tmp_path），无网络。
"""
import json

import pytest

from conftest import build_project, do_slice, run_print
from ame.ledger.services import defect_report, gcode_of, latest_slice, skip_log
from ame.printing.virtual import (FAULT_CN, FAULT_TYPES, JobState,
                                  VirtualPrinter, parse_gcode)

# PRT-009 归因分类：设计 / 工艺 / 设备
ATTRIBUTION = {"open_circuit": "design", "clog": "process", "overtemp": "device",
               "oven_undertemp": "device", "misalign": "process",
               "material_out": "process"}


@pytest.fixture()
def sliced_ws(data_root):
    """已切片的 T19-A 工程：导入 STEP + 板文件（自带违规走线）后 do_slice。"""
    ws = build_project(data_root)
    do_slice(ws)
    return ws


def _ready_printer() -> VirtualPrinter:
    """除标定外全部就绪的虚拟打印机（用于逐项验证 precheck 拦截项）。"""
    p = VirtualPrinter("DV-TEST")
    p.connect()
    p.homed = True
    p.temp_ok = True
    p.material_ready = True
    return p


# ---------------------------------------------------------------- T17 / PRT-006
def test_t17_virtual_print_and_gcode_event_replay(sliced_ws):
    """T17：T19-A 工程 do_slice 后 run_print → done、耗时>0、无缺陷；
    G-code 事件流含 pass/cure/move，道次序列 0..6；回放进度/层号/时间一致。"""
    ws = sliced_ws
    sl = latest_slice(ws)
    assert sl is not None, "切片应已入库"
    gcode = gcode_of(ws, sl["id"])

    pr = run_print(ws)
    assert pr["state"] == "done"
    assert pr["elapsed_s"] > 0
    assert pr["defects"] == []

    # 进度回放：G-code 解析为事件流（PRT-006 回放与 G-code 一致）
    events = parse_gcode(gcode)
    kinds = {e["ev"] for e in events}
    assert {"pass", "cure", "move"} <= kinds
    pass_nos = [e["pass_no"] for e in events if e["ev"] == "pass"]
    assert pass_nos == list(range(7)), "道次序列应为 0..6（§3.5 典型 7 道次）"

    # 独立重放一遍：终态进度 100%、层号停在最后一道次、时间与 run_print 一致
    job = JobState("chk", sl["id"], "DV-VIRTUAL-1", sl["gcode_hash"])
    VirtualPrinter().run(job, gcode)
    assert job.progress_pct == 100.0
    assert job.layer_cursor == 6
    assert round(job.elapsed_s, 1) == pr["elapsed_s"]

    # 作业落库（数据规则 §4.2-3：作业绑定切片）
    row = ws.store.one("SELECT * FROM print_job WHERE id=?", (pr["job_id"],))
    assert row is not None and row["state"] == "done" and row["finished_at"]


# ---------------------------------------------------------------- PRT-002
def test_prt002_precheck_rejects_with_chinese_reasons():
    """PRT-002：未连接/未归零/温度未达/材料未就绪/哈希不符/对位超差均拒绝，
    原因为中文且可读；全部满足时放行。"""
    # 全新打印机：未连接
    ok, why = VirtualPrinter().precheck("h", "h")
    assert ok is False and "未连接" in why
    # 未归零
    p = VirtualPrinter()
    p.connect()
    ok, why = p.precheck("h", "h")
    assert ok is False and "归零" in why
    # 温度未达标
    p = _ready_printer()
    p.temp_ok = False
    ok, why = p.precheck("h", "h")
    assert ok is False and "温度" in why
    # 材料未就绪
    p = _ready_printer()
    p.material_ready = False
    ok, why = p.precheck("h", "h")
    assert ok is False and "材料" in why
    # G-code 哈希不符
    p = _ready_printer()
    ok, why = p.precheck("actual-hash", "expect-hash")
    assert ok is False and "哈希" in why
    # 对位超差（PRT-013：超差禁止开始）→ 提示热态标定
    p = _ready_printer()
    p.calib["align_err_mm"] = 0.2
    ok, why = p.precheck("h", "h")
    assert ok is False and "标定" in why
    # 全部就绪 → 放行
    ok, why = _ready_printer().precheck("h", "h")
    assert (ok, why) == (True, "就绪")


# ---------------------------------------------------------------- PRT-003/004
def test_prt003_prt004_pause_resume_two_modes():
    """PRT-003/004：打印中暂停文案含“抬针”，固化中暂停文案含“恒温”；
    恢复后状态分别回到 printing / curing（位置与层号由安全点保留）。"""
    p = VirtualPrinter()
    # 打印中暂停（停挤出、回吸、抬针）
    job = JobState("j1", "s", "d", "h", state="printing", safe_point=(4, 83.5))
    msg = p.pause(job, "printing")
    assert "抬针" in msg
    assert job.state == "paused"
    assert p.resume(job)  # 恢复动作本身可执行
    assert job.state == "printing", "打印中暂停恢复后应回到打印态"
    # 固化中暂停（烘箱恒温保持，禁止直接续时）
    job2 = JobState("j2", "s", "d", "h", state="curing", safe_point=(4, 83.5))
    msg2 = p.pause(job2, "curing")
    assert "恒温" in msg2
    assert job2.state == "paused"
    assert p.resume(job2)
    assert job2.state == "curing", "固化中暂停恢复后应回到固化态（剩余保温重算）"
    # 非可暂停状态拒绝
    done = JobState("j3", "s", "d", "h", state="done")
    assert "不可暂停" in p.pause(done, "printing")
    assert "未处于暂停状态" in p.resume(done)


# ---------------------------------------------------------------- PRT-011
def test_prt011_estop_cancels_job_and_keeps_log():
    """PRT-011：急停后作业 cancelled、events_log 含 estop 记录、加热被切断。"""
    p = VirtualPrinter()
    p.temp_ok = True
    job = JobState("j-estop", "s", "d", "h", state="printing")
    msg = p.estop(job)
    assert job.state == "cancelled"
    estop_events = [e for e in job.events_log if e.get("ev") == "estop"]
    assert estop_events, "急停事件必须记录在案"
    assert p.temp_ok is False, "急停后加热应被切断"
    assert "急停" in msg


# ---------------------------------------------------------------- PRT-014
def test_prt014_resume_after_power_loss_safe_point_and_hash():
    """PRT-014：有安全点且哈希相符 → 从安全点（道次+已打印长度）继续；
    哈希不符 → 拒绝续打；无安全点 → 放弃并标记作废。"""
    p = VirtualPrinter()
    # 打印中途构造安全点：(道次 3, 已打印 50.0 mm)
    job = JobState("j1", "s", "d", gcode_hash="HASH-OK",
                   state="printing", safe_point=(3, 50.0))
    msg = p.resume_after_power_loss(job, "HASH-OK")
    assert "安全点" in msg
    assert job.state == "printing"
    assert job.layer_cursor == 3, "续打应定位到安全点道次"
    assert job.events_log[-1]["ev"] == "power_resume"
    # 哈希不符（坐标可信度校验不过）→ 拒绝续打并标记作废
    job2 = JobState("j2", "s", "d", gcode_hash="HASH-OK",
                    state="paused", safe_point=(3, 50.0))
    msg2 = p.resume_after_power_loss(job2, "HASH-OTHER")
    assert "哈希" in msg2
    assert job2.state == "cancelled", "拒绝续打的作业应标记作废，不能停在打印态"
    assert job2.layer_cursor == 0, "拒绝续打时不得推进层号"
    assert job2.events_log[-1]["ev"] == "power_resume_refused"
    # 无安全点 → 放弃并标记作废
    job3 = JobState("j3", "s", "d", gcode_hash="HASH-OK", safe_point=None)
    msg3 = p.resume_after_power_loss(job3, "HASH-OK")
    assert "无安全点" in msg3
    assert job3.state == "cancelled"


# ---------------------------------------------------------------- PRT-005/007/T18
def test_prt005_prt007_open_circuit_fault_reproducible(sliced_ws):
    """PRT-005/007/T18：注入断线（道次 4、已打印 120mm 处）生成 1 条缺陷；
    同参数重复运行缺陷位置（at）与层（layer_z_mm）一致；层/位置与 G-code 对照正确。"""
    ws = sliced_ws
    plan = [{"type": "open_circuit", "pass_no": 4, "at_len_mm": 120}]
    r1 = run_print(ws, fault_plan=plan)
    r2 = run_print(ws, fault_plan=plan)
    assert r1["state"] == "fault" and r2["state"] == "fault"
    assert len(r1["defects"]) == 1 and len(r2["defects"]) == 1
    d1, d2 = r1["defects"][0], r2["defects"][0]
    assert d1["type"] == d2["type"] == "open_circuit"
    assert d1["pass_no"] == d2["pass_no"] == 4
    assert d1["at"] == d2["at"], "同参数重复运行缺陷位置必须一致（PRT-007）"
    assert d1["layer_z_mm"] == d2["layer_z_mm"], "同参数重复运行缺陷层必须一致"
    # T18：层正确——缺陷层 z 与 G-code 道次 4 的 z 一致
    events = parse_gcode(gcode_of(ws, latest_slice(ws)["id"]))
    z4 = next(e["z"] for e in events if e["ev"] == "pass" and e["pass_no"] == 4)
    assert d1["layer_z_mm"] == z4
    # 位置在板框内（板框 0..100 × 0..60）
    x, y = d1["at"]
    assert 0.0 <= x <= 100.0 and 0.0 <= y <= 60.0


@pytest.mark.parametrize("ftype", FAULT_TYPES)
def test_prt007_each_of_six_fault_types_injects_one_defect(sliced_ws, ftype):
    """PRT-005/007：六种故障各注入一次均生成缺陷，状态 fault，类型与归因正确。"""
    ws = sliced_ws
    r = run_print(ws, fault_plan=[{"type": ftype, "pass_no": 4, "at_len_mm": 50}])
    assert r["state"] == "fault", f"{ftype} 注入后作业应进入故障态"
    assert len(r["defects"]) == 1, f"{ftype} 应恰好产生 1 条缺陷（每层只触发一次）"
    d = r["defects"][0]
    assert d["type"] == ftype
    assert d["type_cn"] == FAULT_CN[ftype]
    assert d["attribution"] == ATTRIBUTION[ftype], "归因分类应符合 PRT-009 映射"
    assert d["severity"] in (1, 2, 3)


# ---------------------------------------------------------------- PRT-008/009
def test_prt008_prt009_defect_record_and_attribution(sliced_ws):
    """PRT-008：缺陷含类型/严重度/证据/状态 open，证据按内容寻址入库可回读；
    PRT-009：open_circuit 归因为设计类且建议回滚，clog 归因为工艺类。"""
    ws = sliced_ws
    r = run_print(ws, fault_plan=[{"type": "open_circuit", "pass_no": 4,
                                   "at_len_mm": 120}])
    report = defect_report(ws, r["job_id"])
    assert len(report) == 1
    d = report[0]
    assert d["type"] == "open_circuit"
    assert d["severity"] == 3
    assert d["status"] == "open"
    assert d["evidence"], "缺陷必须带证据（DAT-004 内容寻址哈希）"
    evidence = json.loads(ws.store.get_blob(d["evidence"]))
    assert evidence["fault"]["type"] == "open_circuit" and "event" in evidence
    assert d["attribution_commit_id"], "缺陷必须关联到设计版本（PRT-009）"
    # PRT-009：设计类归因 → 给出回滚/重切建议
    att = d["attribution"]
    assert att["class"] == "design"
    assert "回滚" in att["diff_hint"]
    # 工艺类故障归因：堵头 → process
    r2 = run_print(ws, fault_plan=[{"type": "clog", "pass_no": 4, "at_len_mm": 50}])
    d2 = defect_report(ws, r2["job_id"])[0]
    assert d2["attribution"]["class"] == "process"
    assert d2["status"] == "open"


# ---------------------------------------------------------------- PRT-012 跳层
class TestPRT012SkipLayer:
    """PRT-012：允许跳过当前层的一段路径并记录原因；跳过后后续层继续、记录可查。

    这是需求覆盖度核查发现的缺口：规格书里有 PRT-012（P2），
    代码与测试里却完全没有对应实现——此前 125 条需求里唯一一条
    "规格有、实现无"的。
    """

    def test_dynamic_skip_via_request_skip(self):
        """运行中请求跳层：当前层剩余路径放弃，后续层继续，原因入事件流。"""
        gcode = "\n".join(
            f";PASS {n} kind=fill mat=A z={n * 0.1:.1f}\n"
            + "\n".join(f"G1 X{i} Y0" for i in range(1, 6))
            for n in range(3))
        p = _ready_printer()
        job = JobState(job_id="j-skip", slice_set_id="s", device_id="DV-TEST",
                       gcode_hash="h")

        def on_event(ev):
            # 模拟操作员在第 0 层开始打印时按下"跳过本层"
            if ev.get("ev") == "pass" and ev["pass_no"] == 0:
                ok, _ = p.request_skip(job, "怀疑断料，跳过本层观察")
                assert ok

        p.run(job, gcode, on_event=on_event)
        assert job.state == "done", "跳层不是故障，作业应正常完成"
        skips = [e for e in job.events_log if e["ev"] == "skip"]
        assert len(skips) == 1
        assert skips[0]["pass_no"] == 0
        assert skips[0]["reason"] == "怀疑断料，跳过本层观察"
        assert skips[0]["at_len_mm"] >= 0
        # 后续层继续：第 1、2 层的 pass 事件与打印移动都在
        passes = [e["pass_no"] for e in job.events_log if e["ev"] == "pass"]
        assert passes == [0, 1, 2], f"跳层后后续层必须继续，实际 {passes}"
        printed_after = [e for e in job.events_log
                         if e["ev"] == "pass" and e["pass_no"] > 0]
        assert printed_after, "后续层没有打印事件"

    def test_planned_skip_via_fault_plan_end_to_end(self, sliced_ws):
        """声明式跳层走 run_print 全链路：结果、落库、查询三处一致。"""
        ws = sliced_ws
        plan = [{"type": "skip_pass", "pass_no": 3, "at_len_mm": 0.0,
                 "reason": "该层路径越界，跳过后人工检查"}]
        pr = run_print(ws, fault_plan=plan)
        assert pr["state"] == "done"
        assert len(pr["skips"]) == 1
        assert pr["skips"][0]["pass_no"] == 3
        assert "越界" in pr["skips"][0]["reason"]
        # 记录可查：从库里读回
        logged = skip_log(ws, pr["job_id"])
        assert logged == pr["skips"]
        # 跳层后后续层继续：完成时 layer_cursor 应到最后一个道次
        row = ws.store.one("SELECT layer_cursor, skip_log_json FROM print_job "
                           "WHERE id=?", (pr["job_id"],))
        assert row["layer_cursor"] == 6, "跳过第 3 道次后其余道次应继续执行"
        assert json.loads(row["skip_log_json"])[0]["pass_no"] == 3

    def test_skip_request_validation(self):
        """跳层必须给原因；已结束/未开始的作业不可跳。"""
        p = _ready_printer()
        job = JobState(job_id="j2", slice_set_id="s", device_id="DV-TEST",
                       gcode_hash="h")
        ok, why = p.request_skip(job, "还没开始就跳")
        assert not ok and "不可跳层" in why
        job.state = "printing"          # 单元级：直接置打印态验证原因必填
        ok, why = p.request_skip(job, "   ")
        assert not ok and "原因" in why
        gcode = ";PASS 0 kind=fill mat=A z=0.0\nG1 X5 Y0"
        p.run(job, gcode)
        assert job.state == "done"
        ok, why = p.request_skip(job, "打完了才跳")
        assert not ok and "不可跳层" in why


class TestPRT012WithResume:
    """PRT-012 跳层 与 PRT-014 断电续打 同时使用时的语义。

    两者都写 `cursor["skipping"]`，是最容易互相踩坏的一对：
    - PRT-014：开始阶段跳过 `start_pass` 之前的道次
    - PRT-012：中途放弃当前道次剩余移动
    如果共用一个标志且不复位，跳层会把后续道次一并吞掉。
    """

    GCODE = "\n".join(
        f";PASS {n} kind=fill mat=A z={n * 0.1:.1f}\n"
        + "\n".join(f"G1 X{i} Y0" for i in range(1, 6))
        for n in range(4))          # 道次 0,1,2,3

    def test_resume_then_skip_keeps_later_passes(self):
        """从第 2 道次续打并跳过第 3 道次：第 2、3 道次的边界事件都应记录。"""
        p = _ready_printer()
        job = JobState(job_id="j-rs", slice_set_id="s", device_id="DV-TEST",
                       gcode_hash="h")
        plan = [{"type": "skip_pass", "pass_no": 3, "at_len_mm": 0.0,
                 "reason": "续打后跳过末层"}]
        p.run(job, self.GCODE, fault_plan=plan, start_pass=2)
        passes = [e["pass_no"] for e in job.events_log if e["ev"] == "pass"]
        skips = [e["pass_no"] for e in job.events_log if e["ev"] == "skip"]
        assert passes == [2, 3], f"续打应只跑 2、3，实际 {passes}"
        assert skips == [3], "应记录跳过第 3 道次"
        assert job.state == "done"
        assert job.defects == [], "跳层不是故障，不应产生缺陷"

    def test_skip_does_not_leak_into_next_pass(self):
        """跳过第 1 道次后，第 2、3 道次的打印移动必须恢复。"""
        p = _ready_printer()
        job = JobState(job_id="j-sk", slice_set_id="s", device_id="DV-TEST",
                       gcode_hash="h")
        p.run(job, self.GCODE,
              fault_plan=[{"type": "skip_pass", "pass_no": 1, "at_len_mm": 0.0,
                           "reason": "跳过第 1 层"}])
        passes = [e["pass_no"] for e in job.events_log if e["ev"] == "pass"]
        assert passes == [0, 1, 2, 3], f"跳层不得吞掉后续道次，实际 {passes}"
        assert job.layer_cursor == 3, "应一路走到最后道次"

    def test_resume_without_skip_reaches_last_pass(self):
        """纯续打（无跳层）必须跑到最后道次。"""
        p = _ready_printer()
        job = JobState(job_id="j-r", slice_set_id="s", device_id="DV-TEST",
                       gcode_hash="h")
        p.run(job, self.GCODE, start_pass=2)
        passes = [e["pass_no"] for e in job.events_log if e["ev"] == "pass"]
        assert passes == [2, 3], f"续打应跑 2、3，实际 {passes}"
        assert job.layer_cursor == 3
