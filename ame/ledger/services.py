"""切片与打印作业服务：把切片器/虚拟打印机接入存储（SLC/PRT 域落库）。"""
from __future__ import annotations

import json

from .. import new_id
from ..db.store import now_iso
from ..logging_setup import get_logger
from .workspace import CmdError
from ..printing.virtual import JobState, VirtualPrinter, attribute_defect
from ..slicing.gcode import export_gcode, gcode_content_hash
from ..slicing.slicer import build_schedule, build_slices, estimate

_LOG = get_logger("services")


def params_dict(ws) -> dict:
    p = ws.params()
    return {k: p[k] for k in p if k not in ("id", "material_a_id", "material_b_id")}


def do_slice(ws, note="切片", source_commit: str | None = None) -> dict:
    """执行切片并入库。阻断条件：无板框/断路短路/材料停用/参数越界（SLC-012）。

    source_commit 缺省取当前 head；重放（AGT-008）时显式传入同一起始版本，
    保证两边 G-code 头部一致、哈希可比。
    """
    outline, onote = ws.board_outline()
    if not outline:
        raise RuntimeError(f"不能切片：{onote}（STR-007 无板框不能进入切片）")
    p = ws.params()
    mat_a = ws.store.one("SELECT * FROM material_profile WHERE id=?", (p["material_a_id"],))
    mat_b = ws.store.one("SELECT * FROM material_profile WHERE id=?", (p["material_b_id"],))
    if not mat_a or not mat_a["enabled"] or not mat_b or not mat_b["enabled"]:
        raise RuntimeError("不能切片：材料缺失或已停用（SLC-013）")
    # 参数整体校验（第二道防线：参数可能被旧数据/外部写入绕过命令总线）
    try:
        ws._validate_params({k: p[k] for k in ws.PARAM_RANGES if k in p})
    except CmdError as e:
        raise RuntimeError(f"不能切片：{e}") from e
    if p["cure_temp_c"] < p["set_temp_c"] or p["set_temp_c"] < p["pregel_temp_c"]:
        raise RuntimeError("固化温度曲线不合法：预凝胶 ≤ 定型 ≤ 层固化（SLC-012）")
    pending = [f for f in ws.features() if f["status"] == "pending"]
    warnings = []
    if pending:
        warnings.append(f"{len(pending)} 个布尔特征待执行（V1 才执行布尔），"
                        "切片按当前几何进行（STR-006）")

    state = ws.mrc_state()
    slices = build_slices(state, params_dict(ws))
    if slices.get("skipped_degenerate_routes"):
        warnings.append(f"{slices['skipped_degenerate_routes']} 条畸形走线"
                        "（少于 2 点）已跳过切片，请在问题面板复查（NFR-002）")
    if slices.get("thickness_note"):
        warnings.append(slices["thickness_note"])
    if not slices["connectivity"]["ok"]:
        loc = slices["connectivity"]["opens"][:1] or slices["connectivity"]["shorts"][:1]
        where = loc[0].get("at") if loc else "?"
        raise RuntimeError(f"切片后连通性失败，阻断导出（SLC-007）：{loc} 定位 {where}")

    est = estimate(slices, params_dict(ws), mat_a, mat_b)
    src_commit = source_commit or ws.project["head_commit_id"]
    # G-code 头的 design_sha256 必须只取决于**设计输入**（SLC-008 确定性：
    # 同一输入连续导出哈希必须一致）。不能拿整个 state 做摘要——state 里含
    # 派生数据（如 printed_layers 来自最近一次切片），两次切片之间会变。
    gcode = export_gcode(slices, params_dict(ws), mat_a, mat_b,
                         ws.project["name"], src_commit, ws.design_digest())
    gh, grel = ws.store.put_blob(gcode.encode("utf-8"))
    content_hash = gcode_content_hash(gcode)
    sid = new_id()
    with ws.store.batch():          # 800+ 条 INSERT 只提交一次（性能 + 原子性）
        ws.store.execute(
            "INSERT INTO slice_set(id,project_id,source_commit_id,params_id,layer_count,"
            "stale,estimate_json,gcode_hash,gcode_blob,created_at) "
            "VALUES (?,?,?,?,?,0,?,?,?,?)",
            (sid, ws.project_id, src_commit, p["id"], slices["layer_count"],
             json.dumps(est, ensure_ascii=False), content_hash, gh, now_iso()))
        for pa in slices["passes"]:
            lid = new_id()
            mat_id = p["material_a_id"] if pa["mat"] == "A" else p["material_b_id"]
            ws.store.execute("INSERT INTO layer(id,slice_set_id,seq,pass_no,z_mm,h_mm,"
                             "kind,material_id,path_count,printable) "
                             "VALUES (?,?,?,?,?,?,?,?,?,1)",
                             (lid, sid, pa["pass_no"], pa["pass_no"], pa["z_mm"],
                              pa.get("h_mm", 0.0), pa["kind"],
                              mat_id, len(pa["paths"])))
            for path in pa["paths"]:
                ws.store.execute("INSERT INTO path(id,layer_id,seq,role,width_mm,speed_mm_s,"
                                 "pts_json) VALUES (?,?,?,?,?,?,?)",
                                 (new_id(), lid, path["seq"], path["role"], path["width_mm"],
                                  path["speed_mm_s"], json.dumps(path["pts"])))
    # gcode_hash 对外统一为内容哈希（与 slice_set.gcode_hash 同口径）；
    # 文件 blob 键单独给出，供取文本用（SLC-008 / DAT-005）
    prune_slices(ws)          # 保留最近 N 份，防止反复切片撑大工程库
    _LOG.info("切片完成 project=%s passes=%d layers=%d 总厚=%s gcode_hash=%s",
              ws.project_id[:8], len(slices["passes"]), slices["layer_count"],
              slices.get("total_thickness_mm"), content_hash[:12])
    return {"slice_set_id": sid, "passes": len(slices["passes"]),
            "pass_details": slices["passes"],      # 逐道次详情（预览/逐层播放用）
            "layer_count": slices["layer_count"],
            "total_thickness_mm": slices.get("total_thickness_mm"),
            "gcode_hash": content_hash, "gcode_blob": gh,
            "estimate": est, "warnings": warnings,
            "schedule": build_schedule(params_dict(ws), slices)}


# 每个工程保留的切片份数上限（含最新的）。改参数重切是高频操作，
# 每份切片有 800+ 条路径记录，不设上限会让工程库无限膨胀。
SLICE_KEEP = 5


def prune_slices(ws, keep: int = SLICE_KEEP) -> int:
    """只保留最近 keep 份切片（及其层/路径），返回清理的份数。

    DAT-004/NFR-002：数据目录不应因反复切片无限增长。保留最近若干份
    足以支撑"改参数对比上一次结果"，更早的从历史版本可重切恢复。
    """
    rows = ws.store.query(
        "SELECT id FROM slice_set WHERE project_id=? "
        f"ORDER BY {ws.store.recent_order()}",
        (ws.project_id,))
    drop = [r["id"] for r in rows[keep:]]
    if not drop:
        return 0
    with ws.store.batch():
        for sid in drop:
            ws.store.execute(
                "DELETE FROM path WHERE layer_id IN "
                "(SELECT id FROM layer WHERE slice_set_id=?)", (sid,))
            ws.store.execute("DELETE FROM layer WHERE slice_set_id=?", (sid,))
            ws.store.execute("DELETE FROM slice_set WHERE id=?", (sid,))
    # 删除量较大时回收磁盘空间（VACUUM 会重建库文件，代价较高，故设阈值）
    if len(drop) >= 3:
        try:
            # VACUUM 是 SQLite 专有（PG 用 VACUUM 但语法/事务要求不同，
            # 且 psycopg 的 conn.execute 不接受）。下沉到实现层的可选方法。
            _vacuum = getattr(ws.store, "vacuum", None)
            if _vacuum is not None:
                _vacuum()
        except Exception:
            pass
    return len(drop)


def latest_slice(ws) -> dict | None:
    row = ws.store.one("SELECT * FROM slice_set WHERE project_id=? AND stale=0 "
                       f"ORDER BY {ws.store.recent_order()} LIMIT 1", (ws.project_id,))
    return row


def gcode_of(ws, slice_set_id: str) -> str:
    row = ws.store.one("SELECT gcode_hash, gcode_blob FROM slice_set WHERE id=?",
                       (slice_set_id,))
    if not row:
        raise RuntimeError("切片不存在")
    key = row.get("gcode_blob") or row["gcode_hash"]
    return ws.store.get_blob(key).decode("utf-8")


def set_device_online(ws, online: bool, device_id="DV-VIRTUAL-1") -> str:
    """设置设备连接状态（PRT-001）。走服务层而非界面直写数据库（AGT-009）。"""
    ensure_device(ws, device_id)
    ws.store.execute("UPDATE device SET online=? WHERE id=?",
                     (1 if online else 0, device_id))
    return device_id


def _save_safepoint(store, job_id: str, pass_no: int, printed_len: float) -> None:
    """落一个续打安全点（PRT-014）。

    打印与续打共用同一写入口：两处曾各写一份相同 UPDATE，字段口径一旦
    分叉就会让"断电续打"按错误坐标继续。
    """
    store.execute(
        "UPDATE print_job SET layer_cursor=?, safe_point_json=? WHERE id=?",
        (pass_no, json.dumps({"pass_no": pass_no, "printed_mm": printed_len}),
         job_id))


def ensure_device(ws, device_id="DV-VIRTUAL-1") -> str:
    """确保设备记录存在。首次创建时置为在线（虚拟打印机默认已连接）。"""
    if not ws.store.one("SELECT id FROM device WHERE id=?", (device_id,)):
        ws.store.execute("INSERT INTO device(id,name,protocol,fw_version,calib_json,"
                         "online,bed_x_mm,bed_y_mm) VALUES (?,?,?,?,'{\"offset_a\":[0,0],"
                         "\"offset_b\":[0,0],\"z_a\":0,\"z_b\":0,\"calibrated_at\":"
                         "\"2026-10-26T00:00:00\",\"align_err_mm\":0.05}',1,?,?)",
                         (device_id, "虚拟打印机 1", "virtual", "v0",
                          ws.project["bed_x_mm"], ws.project["bed_y_mm"]))
    return device_id


def run_print(ws, fault_plan: list[dict] | None = None, device_id="DV-VIRTUAL-1",
              on_event=None, homed=True, temp_ok=True, material_ready=True) -> dict:
    """虚拟打印（PRT-006）：前检查→执行→缺陷入库（PRT-008/009）。

    设备在线状态与双头对位误差从库里读取（PRT-001/013）。早期实现把
    online/homed/temp_ok/material_ready 全部硬置为 True，导致"设备离线仍能
    打印""对位超差不禁打"，打印前检查形同虚设。
    homed/temp_ok/material_ready 由调用方（界面/测试）传入，代表操作员确认的
    实时状态；默认 True 便于脚本化演示。
    """
    sl = latest_slice(ws)
    if not sl:
        raise RuntimeError("无有效切片（或已过期），请先切片")
    ensure_device(ws, device_id)
    dev = ws.store.one("SELECT * FROM device WHERE id=?", (device_id,))
    if not dev or not dev["online"]:
        raise RuntimeError(f"设备 {device_id} 未连接（PRT-001）：请先连接设备再开始打印")
    try:
        calib = json.loads(dev["calib_json"]) if dev.get("calib_json") else {}
    except (json.JSONDecodeError, TypeError):
        calib = {}
    gcode = gcode_of(ws, sl["id"])
    printer = VirtualPrinter(device_id, calib=calib)
    printer.connect()
    printer.homed = bool(homed)
    printer.temp_ok = bool(temp_ok)
    printer.material_ready = bool(material_ready)
    ok, why = printer.precheck(gcode_content_hash(gcode), sl["gcode_hash"])
    if not ok:
        raise RuntimeError(f"打印前检查未通过（PRT-002）：{why}")
    job = JobState(job_id=new_id(), slice_set_id=sl["id"], device_id=device_id,
                   gcode_hash=sl["gcode_hash"])
    ws.store.execute("INSERT INTO print_job(id,slice_set_id,device_id,state,layer_cursor,"
                     "gcode_hash,fault_plan_json,started_at) VALUES (?,?,?,?,0,?,?,?)",
                     (job.job_id, sl["id"], device_id, "printing", sl["gcode_hash"],
                      json.dumps(fault_plan or []), now_iso()))

    def save_safepoint(pass_no: int, printed_len: float):
        """每到一个安全点就落库（PRT-014）：进程被打断也能定位续打点。"""
        _save_safepoint(ws.store, job.job_id, pass_no, printed_len)

    printer.run(job, gcode, fault_plan or [], on_event=on_event,
                on_safepoint=save_safepoint)
    for d in job.defects:
        ev_h, _ = ws.store.put_blob(json.dumps(d["evidence"], ensure_ascii=False,
                                               sort_keys=True).encode("utf-8"))
        ws.store.execute("INSERT INTO defect(id,job_id,layer_z_mm,pass_no,type,severity,"
                         "bbox_json,evidence,status,attribution_commit_id,note) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (new_id(), job.job_id, d["layer_z_mm"], d["pass_no"], d["type"],
                          d["severity"], json.dumps(d["bbox"]), ev_h, "open",
                          sl["source_commit_id"], d["advice"]))
    # 跳层留痕（PRT-012）：运行中的跳层记录随作业落库，事后可查。
    skips = [e for e in job.events_log if e.get("ev") == "skip"]
    ws.store.execute("UPDATE print_job SET state=?, finished_at=?, skip_log_json=? "
                     "WHERE id=?",
                     (job.state, now_iso(),
                      json.dumps(skips, ensure_ascii=False) if skips else None,
                      job.job_id))
    prune_jobs(ws)            # 作业历史保留上限，防止无限增长拖慢查询
    _LOG.info("打印完成 job=%s state=%s 耗时=%.1fs 缺陷=%d 跳层=%d",
              job.job_id[:8], job.state, job.elapsed_s, len(job.defects), len(skips))
    return {"job_id": job.job_id, "state": job.state,
            "elapsed_s": round(job.elapsed_s, 1), "defects": job.defects,
            "skips": skips}


def resumable_jobs(ws) -> list[dict]:
    """可续打的作业（PRT-014）：有安全点但未正常结束的作业。"""
    rows = ws.store.query(
        "SELECT * FROM print_job WHERE safe_point_json IS NOT NULL "
        "AND state NOT IN ('done','cancelled') "
        f"ORDER BY {ws.store.recent_order(time_col='started_at')}")
    out = []
    for r in rows:
        try:
            sp = json.loads(r["safe_point_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        out.append({**dict(r), "safe_point": sp})
    return out


def skip_log(ws, job_id: str) -> list[dict]:
    """某次作业的跳层记录（PRT-012"记录可查"）：[{pass_no, at_len_mm, reason}]。"""
    row = ws.store.one("SELECT skip_log_json FROM print_job WHERE id=?", (job_id,))
    if not row or not row["skip_log_json"]:
        return []
    try:
        out = json.loads(row["skip_log_json"])
    except (json.JSONDecodeError, TypeError):
        return []
    return out if isinstance(out, list) else []


def resume_print(ws, job_id: str, device_id="DV-VIRTUAL-1") -> dict:
    """断电续打（PRT-014）：从落库的安全点所在道次继续。

    坐标可信度：G-code 哈希必须与作业记录一致，否则拒绝并作废（不能停在
    "看起来还在打"的假象）。续打前同样过一遍打印前检查（PRT-002）。
    """
    row = ws.store.one("SELECT * FROM print_job WHERE id=?", (job_id,))
    if not row:
        raise RuntimeError(f"作业 {job_id} 不存在")
    if not row["safe_point_json"]:
        raise RuntimeError("该作业没有安全点记录，无法续打（PRT-014）")
    try:
        sp = json.loads(row["safe_point_json"])
    except (json.JSONDecodeError, TypeError) as e:
        raise RuntimeError("安全点记录已损坏，请重新切片打印") from e

    sl = latest_slice(ws)
    if not sl or sl["id"] != row["slice_set_id"]:
        raise RuntimeError("切片已变更或过期，续打会与已打印部分错位，请重新切片")
    dev = ws.store.one("SELECT * FROM device WHERE id=?", (device_id,))
    if not dev or not dev["online"]:
        raise RuntimeError(f"设备 {device_id} 未连接（PRT-001）")

    gcode = gcode_of(ws, sl["id"])
    printer = VirtualPrinter(device_id)
    msg = printer.resume_after_power_loss(
        JobState(job_id=job_id, slice_set_id=sl["id"], device_id=device_id,
                 gcode_hash=row["gcode_hash"],
                 safe_point=(sp["pass_no"], sp.get("printed_mm", 0.0))),
        sl["gcode_hash"])
    if "拒绝" in msg or "作废" in msg:
        ws.store.execute("UPDATE print_job SET state='cancelled', finished_at=? WHERE id=?",
                         (now_iso(), job_id))
        raise RuntimeError(f"续打被拒绝：{msg}")

    job = JobState(job_id=job_id, slice_set_id=sl["id"], device_id=device_id,
                   gcode_hash=sl["gcode_hash"])
    printer.connect()
    printer.homed = printer.temp_ok = printer.material_ready = True
    ok, why = printer.precheck(gcode_content_hash(gcode), sl["gcode_hash"])
    if not ok:
        raise RuntimeError(f"续打前检查未通过（PRT-002）：{why}")

    ws.store.execute("UPDATE print_job SET state='printing', finished_at=NULL WHERE id=?",
                     (job_id,))

    def save_safepoint(pass_no: int, printed_len: float):
        _save_safepoint(ws.store, job_id, pass_no, printed_len)

    printer.run(job, gcode, [], start_pass=sp["pass_no"],
                on_safepoint=save_safepoint)
    ws.store.execute("UPDATE print_job SET state=?, finished_at=? WHERE id=?",
                     (job.state, now_iso(), job_id))
    return {"job_id": job_id, "state": job.state, "resumed_from": sp,
            "message": msg, "elapsed_s": round(job.elapsed_s, 1),
            "defects": job.defects}


def set_defect_status(ws, defect_id: str, status: str, note: str | None = None) -> dict:
    """标记缺陷状态（PRT-008）：open / fixed / ignored。

    schema 里三种状态早就定义了，但全仓库没有一条 UPDATE defect——
    用户只能看着"状态 open"却无法处理。这里补上唯一的写入口。
    """
    if status not in ("open", "fixed", "ignored"):
        raise ValueError(f"缺陷状态非法：{status!r}（只允许 open/fixed/ignored）")
    row = ws.store.one("SELECT * FROM defect WHERE id=?", (defect_id,))
    if not row:
        raise KeyError(f"缺陷 {defect_id} 不存在")
    old_status, old_note = row["status"], row["note"]
    new_note = old_note if note is None else note
    ws.store.execute("UPDATE defect SET status=?, note=? WHERE id=?",
                     (status, new_note, defect_id))
    return {"id": defect_id, "status": status, "note": new_note,
            "old_status": old_status}


def defect_report(ws, job_id: str) -> list[dict]:
    """缺陷列表 + 归因（PRT-009）。"""
    defects = ws.store.query("SELECT * FROM defect WHERE job_id=?", (job_id,))
    commits = ws.history()
    out = []
    for d in defects:
        dd = dict(d)
        dd["attribution"] = attribute_defect(
            {"type": d["type"]}, commits + [{"id": "head", "reason": "当前版本"}])
        out.append(dd)
    return out


# ================================================================ 作业历史（PRT-010，P1）
JOB_KEEP = 50       # 每个工程保留的打印作业条数上限


def prune_jobs(ws, keep: int = JOB_KEEP) -> int:
    """只保留最近 keep 条打印作业（缺陷随作业删除）。返回清理条数。"""
    rows = ws.store.query(
        f"SELECT id FROM print_job ORDER BY {ws.store.recent_order(time_col='started_at')}")
    drop = [r["id"] for r in rows[keep:]]
    if not drop:
        return 0
    with ws.store.batch():
        for jid in drop:
            ws.store.execute("DELETE FROM defect WHERE job_id=?", (jid,))
            ws.store.execute("DELETE FROM print_job WHERE id=?", (jid,))
    return len(drop)


def job_history(ws, project_id: str | None = None,
                since: str | None = None, until: str | None = None,
                state: str | None = None) -> list[dict]:
    """历次打印作业：时间、版本、参数、结果、耗时（PRT-010）。

    project_id / since / until / state 均可选：
    - since/until：ISO 时间字符串（"2026-10-27" 或 "2026-10-27T20:00:00"），
      按 started_at 左闭右开过滤；
    - state：按作业状态过滤（done / fault / cancelled / printing…）。
    """
    sql = ("SELECT j.*, s.source_commit_id, s.layer_count, s.gcode_hash, "
           "(SELECT COUNT(*) FROM defect d WHERE d.job_id=j.id) AS defect_count "
           "FROM print_job j JOIN slice_set s ON j.slice_set_id=s.id "
           "WHERE s.project_id=?")
    args: list = [project_id or ws.project_id]
    if since:
        sql += " AND j.started_at >= ?"
        args.append(since)
    if until:
        # until 为日期（无时间部分）时含当天全天
        edge = f"{until}T23:59:59" if len(until) == 10 else until
        sql += " AND j.started_at <= ?"
        args.append(edge)
    if state:
        sql += " AND j.state = ?"
        args.append(state)
    # 同一秒内多次打印时 started_at 相同，必须用稳定次级键（rowid/ctid），
    # 否则"最新作业"可能取到较早的那次（实测顺序会反）。
    sql += f" ORDER BY {ws.store.recent_order('j', time_col='started_at')}"
    rows = ws.store.query(sql, tuple(args))
    out = []
    for r in rows:
        est = ws.store.one("SELECT estimate_json FROM slice_set WHERE id=?",
                           (r["slice_set_id"],))
        try:
            es = json.loads(est["estimate_json"]) if est else {}
        except json.JSONDecodeError:
            es = {}
        out.append({
            "job_id": r["id"], "state": r["state"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "source_commit": r["source_commit_id"], "layers": r["layer_count"],
            "gcode_hash": (r["gcode_hash"] or "")[:12],
            "defects": r["defect_count"], "faults": json.loads(r["fault_plan_json"] or "[]"),
            "estimated_print_s": (es.get("time_s") or {}).get("print"),
        })
    return out


# ================================================================ 热力图数据（MRC-010，P1）
def _heatmap_targets(issue: dict, routes: dict, pads: dict, comps: dict) -> list[str]:
    """把一条问题的 target_id 解析成热力图对象键（走线/焊盘 id）。

    大多数问题直接给 id；R2/R7 的 target_id 是 "标签|标签"（如
    "走线GND|焊盘U1.3"），要按短 id 前缀与焊盘 ref 编号双向回查。
    面（target_type=face）不落点——面级问题挂在整个结构上，进热力图
    只会造出一个盖全板的假热点。
    """
    tid = issue["target_id"]
    if issue["target_type"] == "route":
        return [tid] if tid in routes else []
    if issue["target_type"] == "face":
        return []
    if tid in pads or tid in comps:
        return [tid]
    keys: list[str] = []
    if "|" not in tid:
        return keys
    for part in tid.split("|"):
        short = part.split("走线")[-1][:8] if "走线" in part else part[:8]
        keys += [rid for rid in routes if rid.startswith(short)]
        keys += [pid for pid in pads if pid.startswith(short)]
    for part in tid.split("|"):
        if "焊盘" in part:
            name = part.split("焊盘")[-1]
            keys += [pid for pid, p in pads.items()
                     if f"{p['ref']}.{p['pad_no']}" == name]
    return keys


def _heatmap_points(agg: dict, routes: dict, pads: dict) -> list[dict]:
    """聚合结果 → 热力图点位：走线取中点，焊盘取中心。"""
    points = []
    for k, a in agg.items():
        if k in routes:
            pts = routes[k]["pts"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x, y = (sum(xs) / len(xs), sum(ys) / len(ys))
            label = f"走线 {routes[k]['net']}"
        elif k in pads:
            x, y = pads[k]["x"], pads[k]["y"]
            label = f"焊盘 {pads[k]['ref']}.{pads[k]['pad_no']}"
        else:
            continue
        points.append({"x": round(x, 2), "y": round(y, 2),
                       "weight": round(a["weight"], 1),
                       "rules": sorted(a["rules"]), "label": label})
    return points


def heatmap(ws, issues: list[dict] | None = None, score: int | None = None) -> dict:
    """按对象给出违规强度，供热力图渲染（MRC-010）。

    返回 {points:[{x,y,weight,rule,label}], bbox:[x0,y0,x1,y1], max_weight}。
    走线取中点，焊盘/面取中心；weight = 该对象累计扣分。

    ``issues`` / ``score`` 由调用方传入时直接复用其评分结果：界面每帧重绘
    都会取热力图数据，若这里再跑一遍 run_mrc，画布重绘就会连带整轮规则检查
    （308 走线实测 109 ms/次）。不传则现场评分，保证纯函数式调用仍可用。
    """
    if issues is None or score is None:
        mrc = ws.run_mrc()
        issues = mrc["issues"] if issues is None else issues
        score = mrc["score"] if score is None else score
    st = ws.mrc_state()
    routes = {r["id"]: r for r in st["routes"]}
    pads = {p["id"]: p for p in st["pads"]}
    comps = {c["ref"]: c for c in st["comps"]}
    agg: dict[str, dict] = {}
    for i in issues:
        for k in set(_heatmap_targets(i, routes, pads, comps)):
            a = agg.setdefault(k, {"weight": 0.0, "rules": set()})
            a["weight"] += i["penalty"]
            a["rules"].add(i["rule_code"])
    outline, _ = ws.board_outline() or ([], "")
    if outline:
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        bbox = [min(xs), min(ys), max(xs), max(ys)]
    else:
        bbox = [0, 0, 100, 100]
    points = _heatmap_points(agg, routes, pads)
    return {"points": points, "bbox": bbox,
            "max_weight": max((p["weight"] for p in points), default=0.0),
            "score": score}
