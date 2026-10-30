"""虚拟打印机与故障注入（PRT-001..009/014）。

按 G-code 回放模拟层、路径、时间（PRT-006）；六种故障可按道次+位置注入且
结果可复现（PRT-007）；缺陷记录与归因（PRT-008/009）；断电续打（PRT-014）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

FAULT_TYPES = ["open_circuit", "clog", "overtemp", "oven_undertemp", "misalign", "material_out"]
FAULT_CN = {"open_circuit": "断线", "clog": "堵头", "overtemp": "超温",
            "oven_undertemp": "烘箱失温", "misalign": "对位漂移", "material_out": "断料"}
# 归因分类（PRT-009）：设计 / 工艺 / 设备
ATTRIBUTION = {
    "open_circuit": ("design", "路径断线多因走线几何或线宽不足，建议回滚到上一版本或加宽走线后重切"),
    "clog": ("process", "堵头与浆料粘度/针径/速度相关，建议检查材料批次并降低速度 10–20%"),
    "overtemp": ("device", "超温触发独立切断，建议检修热台/烘箱测温回路后重跑"),
    "oven_undertemp": ("device", "烘箱失温导致固化不足，建议按当前温度重走完整升温曲线（禁止直接续时）"),
    "misalign": ("process", "双头对位漂移超差，建议热态重新标定 A/B 偏移后再打印"),
    "material_out": ("process", "断料，建议核对体积估算与料筒余量、补料后从安全点续打"),
}


def parse_gcode(text: str) -> list[dict]:
    """把 G-code 解析为事件流：pass / move / cure / dwell。"""
    events = []
    cur = {"pass_no": 0, "z": 0.0, "kind": "", "mat": ""}
    x = y = 0.0
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith(";PASS "):
            toks = ln.split()[1:]
            kv = dict(p.split("=", 1) for p in toks if "=" in p)
            # 道次号是位置参数（";PASS 4 kind=..."），不是 key=value
            no = next((t for t in toks if "=" not in t and t.isdigit()), "0")
            cur = {"pass_no": int(no), "kind": kv.get("kind", ""),
                   "mat": kv.get("mat", ""), "z": float(kv.get("z", "0"))}
            events.append({"ev": "pass", **cur})
        elif ln.startswith(";CURE"):
            parts = ln.split()
            events.append({"ev": "cure", "phase": parts[1], "temp": float(parts[2][2:]),
                           "dur_s": float(parts[3][2:-1]), "pass_no": cur["pass_no"],
                           "z": cur["z"]})
        elif ln.startswith("G0") or ln.startswith("G1"):
            x1, y1 = x, y
            for tok in ln.split()[1:]:
                if tok.startswith("X"):
                    x1 = float(tok[1:])
                elif tok.startswith("Y"):
                    y1 = float(tok[1:])
            d = math.hypot(x1 - x, y1 - y)
            if d > 1e-6:
                events.append({"ev": "move", "x": x1, "y": y1, "d": d,
                               "printing": ln.startswith("G1") and "X" in ln,
                               "pass_no": cur["pass_no"], "z": cur["z"]})
            x, y = x1, y1
        elif ln.startswith("G4"):
            for tok in ln.split():
                if tok.startswith("P"):
                    events.append({"ev": "dwell", "dur_s": float(tok[1:]) / 1000})
    return events


@dataclass
class JobState:
    job_id: str
    slice_set_id: str
    device_id: str
    gcode_hash: str
    state: str = "pending"
    layer_cursor: int = 0
    progress_pct: float = 0.0
    pos: tuple = (0.0, 0.0)
    elapsed_s: float = 0.0
    events_log: list = field(default_factory=list)
    defects: list = field(default_factory=list)
    safe_point: tuple | None = None   # (pass_no, 打印长度) 断电安全点
    # 操作员跳层请求（PRT-012）：request_skip() 置入，run() 在下一个事件
    # 边界消费。虚拟打印是瞬时回放，"运行中"只存在于回调窗口里，
    # 因此经 on_event/on_safepoint 回调置此字段即等价于真实设备的按钮。
    pending_skip: dict | None = None


class VirtualPrinter:
    """虚拟打印机（PRT-001/002/003/004/006/007/011/014）。"""

    def __init__(self, device_id="DV-VIRTUAL-1", calib: dict | None = None):
        self.device_id = device_id
        self.online = False
        self.homed = False
        self.temp_ok = False
        self.material_ready = False
        self.calib = calib or {"offset_a": [0, 0], "offset_b": [0, 0],
                               "z_a": 0, "z_b": 0, "calibrated_at": "2026-10-26T00:00:00",
                               "align_err_mm": 0.05}
        self.align_max = 0.1   # 对位超差禁止开始（PRT-013）

    def connect(self):
        self.online = True
        return True

    def disconnect(self):
        self.online = False

    def precheck(self, gcode_hash: str, expect_hash: str) -> tuple[bool, str]:
        """打印前检查（PRT-002）：不满足禁止开始并给原因。"""
        if not self.online:
            return False, "设备未连接"
        if not self.homed:
            return False, "未归零"
        if not self.temp_ok:
            return False, "温度未达标（热台/烘箱未到设定值）"
        if not self.material_ready:
            return False, "材料未就绪（E1/E2 料筒未确认）"
        if gcode_hash != expect_hash:
            return False, "G-code 哈希不符，切片可能已过期，请重新切片"
        if self.calib.get("align_err_mm", 1.0) > self.align_max:
            return False, (f"双头对位误差 {self.calib['align_err_mm']:.3f} mm 超过 "
                           f"{self.align_max} mm，请热态标定")
        return True, "就绪"

    # ---------------------------------------------------------------- 回放
    def _fire_fault(self, job: JobState, fault: dict, ev: dict,
                    cur_pass: int, on_event) -> None:
        """记录一条缺陷（PRT-008/009）：状态置 fault + 归因 + 证据链。"""
        job.state = "fault"
        sev = {"overtemp": 3, "open_circuit": 3, "misalign": 2, "clog": 2,
               "material_out": 2, "oven_undertemp": 3}[fault["type"]]
        klass, advice = ATTRIBUTION[fault["type"]]
        d = {"type": fault["type"], "type_cn": FAULT_CN[fault["type"]],
             "pass_no": ev.get("pass_no", cur_pass),
             "layer_z_mm": round(ev.get("z", 0.0), 3),
             "at": [round(ev.get("x", 0.0), 2), round(ev.get("y", 0.0), 2)],
             "bbox": [round(ev.get("x", 0) - 4, 1), round(ev.get("y", 0) - 4, 1),
                      round(ev.get("x", 0) + 4, 1), round(ev.get("y", 0) + 4, 1)],
             "severity": sev, "attribution": klass, "advice": advice,
             "evidence": {"fault": fault, "event": {k: ev.get(k) for k in ('x', 'y', 'z', 'pass_no')},
                          "elapsed_s": round(job.elapsed_s, 1)}}
        job.defects.append(d)
        job.events_log.append({"ev": "fault", **d})
        if on_event:
            on_event({"ev": "fault", **d})

    def _on_pass(self, job: JobState, ev: dict, cursor: dict, start_pass: int | None,
                 on_safepoint) -> None:
        """道次边界：更新当前道次；断电续打时跳过 start_pass 之前的道次（PRT-014）。

        注意：cur_pass 在跳过的道次上也要更新——后续 move/cure 事件携带的
        pass_no 只用于与 fault_plan 匹配，跳过段不会产生注入故障，
        但游标必须跟住事件流，否则恢复打印后的道次号会错。
        """
        cursor["cur_pass"] = ev["pass_no"]
        if cursor["skipping"] and cursor["cur_pass"] < (start_pass or 0):
            return
        cursor["skipping"] = False
        cursor["pass_print_len"] = 0.0
        job.layer_cursor = cursor["cur_pass"]
        job.safe_point = (cursor["cur_pass"], 0.0)
        job.events_log.append({"ev": "pass", "pass_no": cursor["cur_pass"],
                               "z": ev["z"]})
        if on_safepoint:
            on_safepoint(cursor["cur_pass"], 0.0)

    def _on_move(self, job: JobState, ev: dict, cursor: dict, total_d: float,
                 fault_plan: list[dict], on_safepoint, on_event) -> None:
        """移动事件：进度/位置/时间；打印移动还负责注入打印类故障与安全点。"""
        cursor["done_d"] += ev["d"]
        job.progress_pct = round(cursor["done_d"] / total_d * 100, 1)
        job.pos = (ev["x"], ev["y"])
        if not ev.get("printing"):
            job.elapsed_s += ev["d"] / 60.0
            return
        cursor["pass_print_len"] += ev["d"]
        job.elapsed_s += ev["d"] / 20.0
        for f in fault_plan:
            if (f["type"] in ("open_circuit", "clog", "misalign", "material_out")
                    and f["pass_no"] == cursor["cur_pass"]
                    and cursor["pass_print_len"] >= f["at_len_mm"]):
                self._fire_fault(job, f, ev, cursor["cur_pass"], on_event)
                break
        for f in fault_plan:
            # 声明式跳层（PRT-012）：与故障注入同一确定性框架——
            # 打印到 at_len_mm 时放弃该层剩余路径，后续层继续。
            if (f.get("type") == "skip_pass"
                    and f["pass_no"] == cursor["cur_pass"]
                    and cursor["pass_print_len"] >= f["at_len_mm"]
                    and id(f) not in cursor["fired_skips"]):
                cursor["fired_skips"].add(id(f))
                self._apply_skip(job, cursor, f.get("reason", "计划跳过"))
                return
        job.safe_point = (cursor["cur_pass"], round(cursor["pass_print_len"], 2))
        if on_safepoint and not job.defects:
            on_safepoint(cursor["cur_pass"], round(cursor["pass_print_len"], 2))

    def _on_cure(self, job: JobState, ev: dict, cursor: dict,
                 fault_plan: list[dict], on_event) -> None:
        """固化/停留事件。

        注意：G-code 里每个 ;CURE 注释后面紧跟一条 G4 停留，二者表示**同一段
        保温时间**。原先 cure 与 dwell 都累加，导致固化时间算两遍、总时长
        虚高一倍（实测 22646 s vs 真实 12423 s）。
        现在：cure 记录时段与温度（不加时），dwell 累加实际停留时间。
        """
        job.state = "curing"
        dur = ev.get("dur_s", 0.0)
        if ev["ev"] == "dwell":
            job.elapsed_s += dur
        for f in fault_plan:
            # 固化故障按层只触发一次（同一层有多个固化相位）
            if (f["type"] in ("overtemp", "oven_undertemp")
                    and f["pass_no"] == cursor["cur_pass"]
                    and id(f) not in cursor["fired_cures"]):
                cursor["fired_cures"].add(id(f))
                self._fire_fault(job, {**f, "at_len_mm": 0},
                                 {"x": job.pos[0], "y": job.pos[1],
                                  "z": ev.get("z", 0.0), "pass_no": cursor["cur_pass"]},
                                 cursor["cur_pass"], on_event)
                break
        job.events_log.append({"ev": "cure", "phase": ev.get("phase", ""),
                               "dur_s": dur, "pass_no": cursor["cur_pass"]})
        if job.state != "fault":
            job.state = "printing"

    def request_skip(self, job: JobState, reason: str) -> tuple[bool, str]:
        """请求跳过当前层的剩余路径（PRT-012）。

        返回 (是否受理, 说明)。打印/固化中受理；未开始、已结束、故障中的
        作业没有"当前层"可跳。真正生效在 run() 的下一个事件边界。
        """
        if job.state not in ("printing", "curing"):
            return False, "当前状态不可跳层（仅打印/固化中可跳）"
        if not reason.strip():
            return False, "跳层必须记录原因"
        job.pending_skip = {"reason": reason.strip()}
        return True, "将在当前层末尾跳过剩余路径，之后继续下一层"

    def _apply_skip(self, job: JobState, cursor: dict, reason: str) -> None:
        """执行跳层：记录原因，放弃当前道次剩余事件（run 主循环跳到下个 ;PASS）。"""
        cursor["skipping"] = True
        job.events_log.append({"ev": "skip",
                               "pass_no": cursor["cur_pass"],
                               "at_len_mm": round(cursor["pass_print_len"], 2),
                               "reason": reason})

    def run(self, job: JobState, gcode_text: str, fault_plan: list[dict] | None = None,
            on_event=None, speed_factor: float = 0.0,
            start_pass: int | None = None, on_safepoint=None) -> JobState:
        """执行作业。speed_factor=0 表示瞬时模拟（演示/测试），>0 为实时倍率。

        fault_plan: [{type, pass_no, at_len_mm}] —— 同一参数重复运行，故障位置一致。
        start_pass: 断电续打时从该道次开始回放（PRT-014），之前的道次跳过。
        on_safepoint: 每到达安全点回调 (pass_no, printed_len) 供调用方落库，
                      否则进程重启后无法定位安全点。

        事件处理拆成 `_on_pass/_on_move/_on_cure` 三个方法；本函数只负责
        事件分派、断点续打的跳过判定与"固化类故障需要 on_event 转发"这一
        处与 fire 的桥接（处理器内部不感知 on_event）。
        """
        fault_plan = fault_plan or []
        events = parse_gcode(gcode_text)
        total_d = sum(e.get("d", 0.0) for e in events if e["ev"] == "move")
        job.state = "printing"
        cursor = {"cur_pass": 0, "pass_print_len": 0.0, "done_d": 0.0,
                  "skipping": start_pass is not None,
                  "fired_cures": set(),    # 固化类故障按层去重
                  "fired_skips": set()}    # 声明式跳层每条只执行一次

        for ev in events:
            if job.state == "fault":
                break
            if ev["ev"] == "pass":
                self._on_pass(job, ev, cursor, start_pass, on_safepoint)
            elif cursor["skipping"]:
                continue
            elif ev["ev"] == "move":
                self._on_move(job, ev, cursor, total_d, fault_plan,
                              on_safepoint, on_event)
            # 操作员跳层（PRT-012）：请求在事件边界消费。放在 move 处理后，
            # 本事件已按原计划完成，从下一事件起放弃当前道次剩余路径。
            req = job.pending_skip
            if req is not None and not cursor["skipping"]:
                job.pending_skip = None
                self._apply_skip(job, cursor, req["reason"])
            elif ev["ev"] in ("cure", "dwell"):
                self._on_cure(job, ev, cursor, fault_plan, on_event)
            if on_event and ev["ev"] != "move":
                on_event(ev)

        if job.state != "fault":
            job.state = "done"
            job.progress_pct = 100.0
        return job

    # ---------------------------------------------------------------- 暂停/恢复（PRT-003/004）
    def pause(self, job: JobState, mode: str = "printing") -> str:
        """mode: printing=打印中暂停（停挤出/回吸/抬针）；curing=固化中暂停（恒温保持）。"""
        if job.state not in ("printing", "curing"):
            return "当前状态不可暂停"
        job.events_log.append({"ev": "pause", "mode": mode})
        if mode == "printing":
            job.state = "paused"
            return "已停挤出、回吸 0.4 mm、抬针 2 mm；恢复前将重校坐标与起线条件"
        job.state = "paused"
        return "烘箱保持恒温；恢复时按当前温度重新计算剩余保温时间（禁止直接续时）"

    def resume(self, job: JobState) -> str:
        if job.state != "paused":
            return "未处于暂停状态"
        # PRT-003/004：按本次暂停的模式恢复——打印中暂停回到打印态，
        # 固化中暂停回到固化态（剩余保温时间按当前温度重算，禁止直接续时）。
        mode = next((e.get("mode") for e in reversed(job.events_log)
                     if e.get("ev") == "pause"
                     and e.get("mode") in ("printing", "curing")), "printing")
        job.state = "curing" if mode == "curing" else "printing"
        job.events_log.append({"ev": "resume", "mode": mode})
        return "已恢复：坐标重校通过，从安全点继续"

    # ---------------------------------------------------------------- 急停（PRT-011）
    def estop(self, job: JobState) -> str:
        self.temp_ok = False
        job.state = "cancelled"
        job.events_log.append({"ev": "estop",
                               "note": "硬件急停独立于软件；加热与出料已切断，记录保留"})
        return "急停：加热与出料已切断，事件已记录"

    # ---------------------------------------------------------------- 断电续打（PRT-014）
    def resume_after_power_loss(self, job: JobState, gcode_hash_expect: str) -> str:
        """重新上电后可选择从最近安全点继续，或放弃并标记作废。

        坐标可信度校验不过（G-code 哈希不符）时必须拒绝续打并置为作废态，
        不能停在打印态造成"看起来还在打"的假象。
        """
        if job.safe_point is None:
            job.state = "cancelled"
            job.events_log.append({"ev": "power_resume_refused",
                                   "reason": "无安全点记录"})
            return "无安全点记录，放弃并标记作废"
        if job.gcode_hash != gcode_hash_expect:
            job.state = "cancelled"
            job.events_log.append({"ev": "power_resume_refused",
                                   "reason": "G-code 哈希不符"})
            return "坐标可信度校验不过（G-code 哈希不符），拒绝续打，作业已标记作废"
        job.state = "printing"
        job.layer_cursor = job.safe_point[0]
        job.events_log.append({"ev": "power_resume", "safe_point": job.safe_point})
        return (f"从安全点（道次 {job.safe_point[0]}，已打印 {job.safe_point[1]:.1f} mm）继续")


def attribute_defect(defect: dict, commits: list[dict]) -> dict:
    """缺陷归因到版本（PRT-009）：给出回滚/重切建议与差异。"""
    klass = ATTRIBUTION[defect["type"]][0]
    out = {"defect": defect, "class": klass}
    if klass == "design" and commits:
        prev = commits[-2] if len(commits) >= 2 else None
        out["rollback_to"] = prev["id"] if prev else None
        out["diff_hint"] = (f"回滚到 {prev['reason']}（{prev['id'][:8]}）后重切"
                            if prev else "无上一版本可回滚，建议修复走线后提交新版本")
    elif klass == "process":
        out["diff_hint"] = "调整工艺参数（速度/温度/批次）后重新切片，无需改设计"
    else:
        out["diff_hint"] = "设备检修后直接重跑同一作业"
    return out
