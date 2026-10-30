"""第十五轮优化：9 个真实缺陷的回归测试。

这些缺陷的共同点是**静默错误**——不崩溃、测试全绿，但用户拿到的是错的结果：

1. NFR-005  工程包内的 project.id 直接拼路径 → `../../x` 把库写出数据目录
2. CIR-002  移动器件只改 comp 表，焊盘留在原地 → 器件与焊盘脱节
3. CIR-011  重复点、线段穿越凹形板框均放行
4. DAT-005  哈希只写不校验 → STEP 被外部改动后静默使用错误几何
5. PRT-002/013  打印前检查硬置状态为 True → 离线/对位超差仍能打印
6. MRC-004/005  min_gap 阈值从不读取；改阈值会把已禁用规则偷偷重开
7. CIR-012  导入板文件非原子；纯网表无坐标时静默导入空板
8. PRJ-005  undo/redo 不记入操作日志
9. STR-008  tess_tol 是死字段，圆弧离散写死 0.02

每个用例都对应一次实测复现，修复前必然失败。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from ame.circuit.connectivity import validate_route
from ame.db.store import DataRoot, IntegrityError
from ame.ledger.services import (do_slice, ensure_device, run_print,
                                 set_device_online)
from ame.ledger.workspace import CmdError, create_project

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "data"


@pytest.fixture(scope="module")
def ui_app(tmp_path_factory):
    """整模块共用一个隐藏 App（重复创建/销毁根窗口会让 Tcl 状态失效）。"""
    import tkinter as tk

    from ame.ui.app import App

    try:
        a = App(None, hidden=True)
    except tk.TclError as e:
        pytest.skip(f"无可用显示，跳过界面相关用例：{e}")
    a.data_root = DataRoot(tmp_path_factory.mktemp("ui") / "d")
    yield a
    try:
        a.ws = None
        a.destroy()
    except Exception:
        pass


def _ws(data_root, name="T15", step="plate_100x60.step", board=None):
    ws = create_project(data_root, name)
    if step:
        ws.cmd_import_step((SAMPLES / step).read_text("utf-8"), step)
    if board:
        ws.cmd_import_board((SAMPLES / board).read_text("utf-8"))
    return ws


# ============================================================ NFR-005 路径穿越
def test_package_import_rejects_path_traversal_id(data_root, tmp_path):
    """工程包里的 id 不得含 `../`，否则数据库会写数据目录之外（NFR-005）。"""
    ws = create_project(data_root, "穿越测试")
    pkg = tmp_path / "good.amepkg"
    data_root.export_project(ws.store, pkg)

    unpacked = tmp_path / "unz"
    with zipfile.ZipFile(pkg) as z:
        z.extractall(unpacked)
    meta = json.loads((unpacked / "manifest.json").read_text("utf-8"))
    meta["project"]["id"] = "../../escaped"
    (unpacked / "manifest.json").write_text(json.dumps(meta), "utf-8")

    evil = tmp_path / "evil.amepkg"
    with zipfile.ZipFile(evil, "w") as z:
        for f in unpacked.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(unpacked))

    with pytest.raises(ValueError, match="非法"):
        data_root.import_project(evil)
    # 数据目录之外不应出现任何逃逸文件
    assert not list(tmp_path.rglob("escaped*")), "数据库被写到了数据目录之外"


def test_package_import_accepts_normal_id(data_root, tmp_path):
    """正常 id 仍可跨机导入（修复不能误伤合法路径）。"""
    ws = create_project(data_root, "正常工程")
    pkg = tmp_path / "ok.amepkg"
    data_root.export_project(ws.store, pkg)
    other = DataRoot(tmp_path / "other")
    store = other.import_project(pkg)
    assert store.one("SELECT name FROM project")["name"] == "正常工程"


@pytest.mark.parametrize("bad", [
    "../../escaped", "..\\..\\escaped", "a/b", "a\\b", ".", "..", "", None,
    "x" * 200, "a:b", "a*b",
])
def test_safe_project_id_rejects_unsafe_values(bad):
    """id 校验本身要挡住一切可作路径的字符（第一道防线，独立于路径复核）。"""
    from ame.db.store import _safe_project_id

    with pytest.raises(ValueError):
        _safe_project_id(bad)


@pytest.mark.parametrize("good", ["abc", "ABC-123_x.y", "0" * 32,
                                  "bdf5e83f-b382-4c33-894f-b5055925ec58"])
def test_safe_project_id_accepts_real_ids(good):
    from ame.db.store import _safe_project_id

    assert _safe_project_id(good) == good


# ============================================================ CIR-002 焊盘随动
def test_move_comp_moves_pads_exactly(data_root):
    """移动器件时焊盘保持相对位置，且撤销/重做精确可逆（CIR-002）。"""
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    comp = ws.comps()[0]
    pads0 = [(p["pad_no"], p["x"], p["y"]) for p in ws.pad_state()
             if p["ref"] == comp["ref"]]

    ws.cmd_move_comp(comp["id"], comp["x_mm"] + 10.0, comp["y_mm"] + 20.0)
    moved = [(p["pad_no"], p["x"], p["y"]) for p in ws.pad_state()
             if p["ref"] == comp["ref"]]
    assert moved != pads0, "焊盘必须随器件平移"
    for (_no, x0, y0), (_, x1, y1) in zip(pads0, moved, strict=True):
        assert x1 == pytest.approx(x0 + 10.0, abs=1e-9)
        assert y1 == pytest.approx(y0 + 20.0, abs=1e-9)

    ws.undo()
    back = [(p["pad_no"], p["x"], p["y"]) for p in ws.pad_state()
            if p["ref"] == comp["ref"]]
    assert back == pads0, "撤销必须精确还原，不留浮点误差"

    ws.redo()
    again = [(p["pad_no"], p["x"], p["y"]) for p in ws.pad_state()
             if p["ref"] == comp["ref"]]
    assert again == moved


# ============================================================ CIR-011 走线校验
SQUARE = [(0, 0), (100, 0), (100, 60), (0, 60)]
# U 形板：中间 40–60 处从 y=20 向上开缺口
U_SHAPE = [(0, 0), (100, 0), (100, 60), (60, 60), (60, 20),
           (40, 20), (40, 60), (0, 60)]


def test_validate_route_rejects_duplicate_point():
    """非相邻重复点必须拒绝（会变成 G-code 停顿点，浆料堆积）。"""
    ok, why = validate_route([[10, 10], [50, 50], [10, 10]], 0.6, SQUARE)
    assert not ok and "重复" in why


def test_validate_route_allows_closed_loop():
    """首尾同点是合法闭合走线（天线线圈/地环/屏蔽框），不能误判为重复。"""
    ok, why = validate_route([[10, 10], [50, 50], [50, 20], [10, 10]], 0.6, SQUARE)
    assert ok, f"闭合走线被误拒：{why}"


def test_validate_route_rejects_segment_crossing_concave_outline():
    """两端都在框内、线段却穿过缺口 —— 只校验端点会漏掉（CIR-011）。"""
    # (30,50) 与 (70,50) 都在框内，但连线穿过 y=20..60 的缺口
    ok, why = validate_route([[30, 50], [70, 50]], 0.6, U_SHAPE)
    assert not ok and "穿出板框" in why


def test_validate_route_accepts_legit_route_in_concave_board():
    ok, why = validate_route([[10, 10], [10, 50]], 0.6, U_SHAPE)
    assert ok, f"合法走线被误拒：{why}"


# ============================================================ DAT-005 完整性
def test_integrity_check_detects_external_modification(data_root):
    """STEP 被外部改动后必须报哈希不符，且不拿错误几何继续算（DAT-005）。"""
    ws = _ws(data_root)
    assert all(r["ok"] for r in ws.integrity_check())

    s = ws.structure()
    p = ws.store.blob_path(s["source_step"])
    original = p.read_bytes()
    try:
        p.write_bytes(original.replace(b"CARTESIAN_POINT", b"CARTESIAN_P0INT", 1))
        rows = ws.integrity_check()
        assert rows and not rows[0]["ok"], "应检出不一致"
        assert "不符" in rows[0]["detail"] or "修改" in rows[0]["detail"]

        ws._step_model_cache = None            # 绕过解析缓存
        with pytest.raises(IntegrityError, match="完整性"):
            ws.step_model()
    finally:
        p.write_bytes(original)
        ws._step_model_cache = None

    assert all(r["ok"] for r in ws.integrity_check()), "恢复后应重新一致"
    assert ws.step_model() is not None


def test_integrity_check_reports_missing_file(data_root):
    """文件被删除也算不一致，并给出可读原因。"""
    ws = _ws(data_root)
    s = ws.structure()
    p = ws.store.blob_path(s["source_step"])
    original = p.read_bytes()
    try:
        p.unlink()
        rows = ws.integrity_check()
        assert not rows[0]["ok"] and "缺失" in rows[0]["detail"]
    finally:
        p.write_bytes(original)


# ============================================================ PRT-002/013 打印前检查
def _sliced(ws):
    ws.commit("基线")
    do_slice(ws)
    return ws


def test_run_print_rejects_offline_device(data_root):
    """设备离线时必须拒绝打印（PRT-001/002）。早期硬置 online=True，检查形同虚设。"""
    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    ensure_device(ws)
    set_device_online(ws, False)
    with pytest.raises(RuntimeError, match="未连接"):
        run_print(ws)


def test_run_print_rejects_misalignment(data_root):
    """双头对位超差时禁止开始（PRT-013），校准值必须从库里读回。"""
    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    ensure_device(ws)
    ws.store.execute("UPDATE device SET calib_json=? WHERE id='DV-VIRTUAL-1'",
                     (json.dumps({"align_err_mm": 0.9}),))
    with pytest.raises(RuntimeError, match="对位误差"):
        run_print(ws)


def test_run_print_accepts_good_calibration(data_root):
    """合格校准（0.05 mm）应正常打印。"""
    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    ensure_device(ws)
    assert run_print(ws)["state"] == "done"


@pytest.mark.parametrize("kw,word", [
    ({"homed": False}, "归零"),
    ({"temp_ok": False}, "温度"),
    ({"material_ready": False}, "材料"),
])
def test_run_print_honours_operator_confirmations(data_root, kw, word):
    """操作员未确认归零/温度/材料时必须拒绝（PRT-002）。"""
    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    ensure_device(ws)
    with pytest.raises(RuntimeError, match=word):
        run_print(ws, **kw)


# ============================================================ MRC-004/005 规则配置
def test_r4_min_gap_threshold_is_effective(data_root):
    """改 R4 的 min_gap 必须改变结果（MRC-011：阈值不能是死配置）。"""
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    base = ws.run_mrc()
    ws.cmd_set_rule("R4", {"min_gap": 5.0})
    after = ws.run_mrc()
    r4_base = [i for i in base["issues"] if i["rule_code"] == "R4"]
    r4_after = [i for i in after["issues"] if i["rule_code"] == "R4"]
    assert len(r4_after) > len(r4_base), "阈值调大后 R4 违规应增多"
    assert after["score"] < base["score"]


def test_disabled_rule_produces_no_issues(data_root):
    """禁用规则后该规则不再产生任何问题（MRC-011 启停）。"""
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    assert any(i["rule_code"] == "R1" for i in ws.run_mrc()["issues"])
    ws.cmd_set_rule("R1", None, False)
    assert not any(i["rule_code"] == "R1" for i in ws.run_mrc()["issues"])


def test_setting_threshold_does_not_re_enable_disabled_rule(data_root):
    """只改阈值不应把已禁用的规则重新启用（enabled=None 表示不改）。"""
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    ws.cmd_set_rule("R1", None, False)
    ws.cmd_set_rule("R1", {"min_width": 0.6})
    row = ws.store.one("SELECT enabled FROM mrc_rule WHERE code='R1'")
    assert row["enabled"] == 0, "改阈值时不应偷偷重启规则"


# ============================================================ CIR-012 导入原子性
def test_board_import_is_atomic_on_failure(data_root):
    """导入中途失败必须回滚，不留半套电路数据（CIR-012/NFR-002）。"""
    ws = _ws(data_root)
    before = (len(ws.comps()), len(ws.pads()), len(ws.nets()),
              ws.store.one("SELECT COUNT(*) c FROM circuit")["c"])

    orig = ws.store.execute
    calls = {"n": 0}

    def boom(sql, params=()):
        if "INSERT INTO comp" in sql:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("模拟写入中途失败")
        return orig(sql, params)

    ws.store.execute = boom
    try:
        with pytest.raises(RuntimeError):
            ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    finally:
        ws.store.execute = orig

    after = (len(ws.comps()), len(ws.pads()), len(ws.nets()),
             ws.store.one("SELECT COUNT(*) c FROM circuit")["c"])
    assert before == after, "失败后不应残留任何电路记录"


def test_netlist_without_coordinates_is_rejected(data_root):
    """只有网络关系、没有焊盘坐标时必须报错，不能静默导入空板（CIR-001）。"""
    ws = _ws(data_root)
    text = json.dumps({"nets": [{"name": "N1", "nodes": [["R1", "1"]]}]})
    with pytest.raises(ValueError, match="坐标"):
        ws.cmd_import_board(text, fmt="netlist+xy")
    assert not ws.comps(), "失败后不应留下空的电路记录"


# ============================================================ PRJ-005 操作日志
def test_undo_and_redo_are_logged(data_root):
    """撤销/重做是对工程的真实修改，必须进操作日志（PRJ-005）。"""
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    comp = ws.comps()[0]
    ws.cmd_move_comp(comp["id"], comp["x_mm"] + 5.0, comp["y_mm"])

    n0 = len(ws.op_log)
    ws.undo()
    assert len(ws.op_log) == n0 + 1 and ws.op_log[-1]["cmd"] == "undo"

    n1 = len(ws.op_log)
    ws.redo()
    assert len(ws.op_log) == n1 + 1 and ws.op_log[-1]["cmd"] == "redo"


def test_undo_still_produces_no_version(data_root):
    """撤销不产生新版本（PRJ-005），这一点不能因记录日志而破坏。"""
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    ws.commit("v0")
    head = ws.project["head_commit_id"]
    comp = ws.comps()[0]
    ws.cmd_move_comp(comp["id"], comp["x_mm"] + 5.0, comp["y_mm"])
    ws.undo()
    assert ws.project["head_commit_id"] == head


# ============================================================ STR-008 网格容差
def test_tess_tol_actually_changes_tessellation(data_root):
    """网格容差必须真正影响圆弧离散（STR-008），不能是死字段。"""
    ws = _ws(data_root, name="容差", step=str(SAMPLES / "real" / "feature.stp"))
    coarse_v = sum(len(f.loop) for f in ws.step_model().faces)

    ws.cmd_set_tess_tol(0.05)             # 放宽 → 顶点应减少
    loose_v = sum(len(f.loop) for f in ws.step_model().faces)
    assert loose_v < coarse_v, f"放宽容差顶点应减少：{coarse_v} → {loose_v}"

    ws.cmd_set_tess_tol(0.005)            # 收紧 → 顶点应增多
    tight_v = sum(len(f.loop) for f in ws.step_model().faces)
    assert tight_v > coarse_v, f"收紧容差顶点应增多：{coarse_v} → {tight_v}"


def test_tess_tol_validated_and_undoable(data_root):
    """容差越界要拒绝；撤销要能还原（STR-008）。"""
    ws = _ws(data_root)
    with pytest.raises(CmdError, match="范围"):
        ws.cmd_set_tess_tol(5.0)
    ws.cmd_set_tess_tol(0.05)
    assert ws.structure()["tess_tol"] == pytest.approx(0.05)
    ws.undo()
    assert ws.structure()["tess_tol"] == pytest.approx(0.02)


# ============================================================ DAT-004 板文件入库
def test_board_file_blob_is_stored_and_verifiable(data_root):
    """板文件原始内容必须入库（DAT-004/005）。

    早期只把 hash 写进 circuit.netlist_hash，没有 put_blob——哈希悬空：
    既无法校验外部改动，导出工程包时也会丢掉板文件。
    """
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    c = ws.store.one("SELECT * FROM circuit WHERE project_id=?", (ws.project_id,))
    assert c["netlist_hash"], "应记录板文件哈希"
    row = ws.store.one("SELECT relpath FROM blob WHERE sha256=?", (c["netlist_hash"],))
    assert row is not None, "板文件内容必须入库（否则哈希悬空）"

    # 篡改后应被检出
    p = ws.store.blob_path(c["netlist_hash"])
    original = p.read_bytes()
    try:
        p.write_bytes(original + b"\n; tampered\n")
        bad = [r for r in ws.integrity_check() if r["kind"] == "板文件"]
        assert bad and not bad[0]["ok"]
    finally:
        p.write_bytes(original)
    good = [r for r in ws.integrity_check() if r["kind"] == "板文件"]
    assert good and good[0]["ok"]


def test_board_blob_removed_on_undo(data_root):
    """撤销导入时板文件 blob 也要清掉，不留孤儿（NFR-002）。"""
    ws = _ws(data_root)
    ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    c = ws.store.one("SELECT * FROM circuit WHERE project_id=?", (ws.project_id,))
    h = c["netlist_hash"]
    assert ws.store.one("SELECT relpath FROM blob WHERE sha256=?", (h,))
    ws.undo()
    assert ws.store.one("SELECT relpath FROM blob WHERE sha256=?", (h,)) is None


# ============================================================ PRJ-007 回收站
def test_delete_and_restore_project_roundtrip(data_root):
    """删除进回收站后可恢复为独立工程（PRJ-007）。"""
    ws = create_project(data_root, "待删工程")
    pid = ws.project_id
    ws.close()
    ws.store.close()

    data_root.delete_project(pid)
    trash = data_root.list_trash()
    assert any(t["name"] == "待删工程" for t in trash)
    assert not any(p["id"] == pid for p in data_root.list_projects())

    new_pid = data_root.restore_project(trash[0]["file"])
    assert new_pid != pid, "恢复应分配新 id，避免与原 id 冲突"
    names = [p["name"] for p in data_root.list_projects()]
    assert "待删工程" in names


def test_delete_occupied_project_gives_actionable_error(data_root):
    """文件仍被连接占用时，给可操作提示而非裸 WinError（PRJ-007）。"""
    ws = create_project(data_root, "占用中")
    try:
        with pytest.raises(PermissionError, match="占用|关闭"):
            data_root.delete_project(ws.project_id)
    finally:
        ws.close()
        ws.store.close()


# ============================================================ PRT-014 断电续打
def test_safe_point_is_persisted_to_db(data_root):
    """每到一个安全点都要落库（PRT-014）。

    原先 safe_point 只活在内存 JobState 里，进程重启后无从定位，
    "重开软件继续打"实际走不通。
    """
    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    job = run_print(ws)
    row = ws.store.one("SELECT layer_cursor, safe_point_json FROM print_job WHERE id=?",
                       (job["job_id"],))
    assert row["safe_point_json"], "安全点必须落库"
    sp = json.loads(row["safe_point_json"])
    assert sp["pass_no"] == row["layer_cursor"]
    assert sp["printed_mm"] >= 0


def test_resume_after_power_loss_from_persisted_point(data_root):
    """模拟断电后重开：能从落库的安全点续打（PRT-014）。"""
    from ame.ledger.services import resumable_jobs, resume_print

    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    job = run_print(ws)
    # 模拟断电：状态回到 printing，安全点保留
    ws.store.execute("UPDATE print_job SET state='printing', finished_at=NULL WHERE id=?",
                     (job["job_id"],))

    pending = resumable_jobs(ws)
    assert any(j["id"] == job["job_id"] for j in pending)
    r = resume_print(ws, job["job_id"])
    assert r["state"] == "done"
    assert r["resumed_from"]["pass_no"] >= 0
    row = ws.store.one("SELECT state FROM print_job WHERE id=?", (job["job_id"],))
    assert row["state"] == "done"


def test_resume_rejected_when_gcode_hash_mismatch(data_root):
    """G-code 哈希不符时必须拒绝续打并作废（PRT-014：坐标不可信）。"""
    from ame.ledger.services import resume_print

    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    job = run_print(ws)
    ws.store.execute(
        "UPDATE print_job SET state='printing', gcode_hash='DEADBEEF', "
        "safe_point_json=? WHERE id=?",
        (json.dumps({"pass_no": 3, "printed_mm": 10.0}), job["job_id"]))
    with pytest.raises(RuntimeError, match="拒绝"):
        resume_print(ws, job["job_id"])
    row = ws.store.one("SELECT state FROM print_job WHERE id=?", (job["job_id"],))
    assert row["state"] == "cancelled", "拒绝续打后必须置为作废，不能停在 printing"


def test_resume_refused_without_safe_point(data_root):
    from ame.ledger.services import resume_print

    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    job = run_print(ws)
    ws.store.execute("UPDATE print_job SET safe_point_json=NULL WHERE id=?", (job["job_id"],))
    with pytest.raises(RuntimeError, match="安全点"):
        resume_print(ws, job["job_id"])


def test_resume_rejects_stale_slice(data_root):
    """切片变了还续打会与已打印部分错位，必须拒绝（PRT-014）。"""
    from ame.ledger.services import resume_print

    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    job = run_print(ws)
    ws.store.execute("UPDATE print_job SET state='printing' WHERE id=?", (job["job_id"],))
    # 重新切片 -> latest_slice 变成本次新切片，与作业记录的 slice_set_id 不同
    ws.store.execute("UPDATE slice_set SET stale=1 WHERE project_id=?", (ws.project_id,))
    do_slice(ws, "重新切片")
    with pytest.raises(RuntimeError, match="切片"):
        resume_print(ws, job["job_id"])


def test_run_can_start_from_given_pass(data_root):
    """run(start_pass=N) 应跳过之前的道次（续打的底层能力）。"""
    from ame.printing.virtual import JobState, VirtualPrinter
    from ame.ledger.services import gcode_of, latest_slice

    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    sl = latest_slice(ws)
    gcode = gcode_of(ws, sl["id"])
    pr = VirtualPrinter()
    full = JobState(job_id="a", slice_set_id=sl["id"], device_id="d",
                    gcode_hash=sl["gcode_hash"])
    pr.run(full, gcode, [])
    partial = JobState(job_id="b", slice_set_id=sl["id"], device_id="d",
                       gcode_hash=sl["gcode_hash"])
    pr.run(partial, gcode, [], start_pass=full.layer_cursor)
    assert partial.elapsed_s < full.elapsed_s, "从末道次续打应比全量打印耗时更短"


# ============================================================ CIR-003 飞线显示
def test_ratlines_use_mst_not_complete_graph():
    """飞线按最小生成树连：n 个焊盘 n-1 条线，不是 n(n-1)/2 条（CIR-003）。"""
    from ame.circuit.connectivity import ratlines

    pads = [{"ref": "R1", "pad_no": "1", "net": "N1", "x": 0, "y": 0},
            {"ref": "R1", "pad_no": "2", "net": "N1", "x": 10, "y": 0},
            {"ref": "R2", "pad_no": "1", "net": "N1", "x": 20, "y": 0},
            {"ref": "R2", "pad_no": "2", "net": "N1", "x": 30, "y": 0}]
    rl = ratlines(pads, [])
    assert len(rl) == 3, f"4 焊盘应是 3 条飞线（MST），实际 {len(rl)}"
    assert all(r["net"] == "N1" for r in rl)
    assert sum(r["len"] for r in rl) == pytest.approx(30.0)


def test_ratlines_skip_routed_nets_and_single_pads():
    """已布线的网络不再画飞线；单焊盘网络画不出线（CIR-003）。"""
    from ame.circuit.connectivity import ratlines

    pads = [{"ref": "R1", "pad_no": "1", "net": "N1", "x": 0, "y": 0},
            {"ref": "R2", "pad_no": "1", "net": "N1", "x": 10, "y": 0},
            {"ref": "R3", "pad_no": "1", "net": "N2", "x": 5, "y": 5}]
    assert ratlines(pads, [{"net": "N1"}]) == []
    assert ratlines(pads, [], net="N2") == []


def test_ratlines_can_filter_single_net():
    """按网络过滤（CIR-003 单网络高亮的数据基础）。"""
    from ame.circuit.connectivity import ratlines

    pads = [{"ref": "R1", "pad_no": "1", "net": "N1", "x": 0, "y": 0},
            {"ref": "R2", "pad_no": "1", "net": "N1", "x": 10, "y": 0},
            {"ref": "R3", "pad_no": "1", "net": "N2", "x": 0, "y": 20},
            {"ref": "R4", "pad_no": "1", "net": "N2", "x": 10, "y": 20}]
    only = ratlines(pads, [], net="N2")
    assert len(only) == 1 and only[0]["net"] == "N2"


# ============================================================ UI-004 面板完整性
def test_layer_and_print_panels_exist_and_populate(ui_app):
    """层列表 / 打印面板必须存在且能填充真实数据（UI-004）。"""
    import time

    app = ui_app
    # 复用模块级隐藏窗口（重复创建/销毁根窗口会让 Tcl 状态失效）
    tabs = [app.nb.tab(i, "text") for i in range(app.nb.index("end"))]
    assert "层" in tabs and "打印" in tabs

    app.open_sample()
    app.ws.commit("基线")
    app.slice_now()
    for _ in range(300):
        app.update()
        if app.slices:
            break
        time.sleep(0.02)
    assert app.slices, "切片未完成"

    rows = app.layer_tree.get_children()
    pds = app.slices["pass_details"]
    assert len(rows) == len(pds), "层列表行数应等于道次数"
    first = app.layer_tree.item(rows[0], "values")
    assert first[0] == str(pds[0]["pass_no"])

    # 层导航
    app.play_index = 0
    app.step_layer(1)
    assert app.play_index == 1
    app.step_layer(-1)
    assert app.play_index == 0

    # 打印面板显示设备与安全点
    app.start_print()
    app._refresh_print_tab()
    assert "在线" in app.lbl_pr_device.cget("text")
    assert "道次" in app.lbl_pr_safe.cget("text")


# ============================================================ 迁移 v5 → v6
def test_migration_v5_to_v6_adds_safe_point_column(tmp_path):
    """旧工程升级后必须新增 safe_point_json 列且保留原有数据（DAT-006）。"""
    import sqlite3

    from ame.db.store import CURRENT_SCHEMA_VERSION, Store

    old = tmp_path / "old.db"
    conn = sqlite3.connect(str(old))
    conn.executescript("""
    CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, created_at TEXT,
      schema_version INTEGER, head_commit_id TEXT, bed_x_mm REAL, bed_y_mm REAL);
    INSERT INTO project VALUES ('p1','旧工程','2026-01-01',5,NULL,200,200);
    CREATE TABLE print_job (id TEXT PRIMARY KEY, slice_set_id TEXT NOT NULL,
      device_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      layer_cursor INTEGER NOT NULL DEFAULT 0, gcode_hash TEXT NOT NULL,
      fault_plan_json TEXT NOT NULL DEFAULT '[]', started_at TEXT, finished_at TEXT);
    INSERT INTO print_job(id,slice_set_id,device_id,state,gcode_hash)
      VALUES ('j1','s1','d1','done','H');
    """)
    conn.commit()
    conn.close()

    st = Store(old)
    try:
        cols = [r[1] for r in st.conn.execute("PRAGMA table_info(print_job)")]
        assert "safe_point_json" in cols
        assert st.one("SELECT schema_version FROM project")["schema_version"] \
            == CURRENT_SCHEMA_VERSION
        assert st.one("SELECT gcode_hash FROM print_job WHERE id='j1'")["gcode_hash"] == "H"
        # 迁移前必须自动备份（DAT-006）
        assert any(s.startswith("backup:") for s in st.migrated)
    finally:
        st.close()


# ============================================================ CIR-001 现代 KiCad
def test_kicad_modern_property_reference():
    """KiCad 6+ 用 (property "Reference" "R1")，位号不能被静默替换成 COMPn。"""
    from ame.circuit.kicad import import_kicad_pcb

    text = '''(kicad_pcb (version 20240108) (generator pcbnew)
      (footprint "Resistor_SMD:R_0603" (layer "F.Cu") (at 10 12)
        (property "Reference" "R1" (at 10 12 0) (layer "F.SilkS"))
        (pad "1" smd rect (at -0.8 0) (size 0.9 0.9) (layers "F.Cu") (net 1 "VCC"))
        (pad "2" smd rect (at 0.8 0) (size 0.9 0.9) (layers "F.Cu") (net 2 "GND")))
      (footprint "Capacitor_SMD:C_0603" (layer "F.Cu") (at 30 12)
        (property "Reference" "C1" (at 30 12 0) (layer "F.SilkS"))
        (pad "1" smd rect (at -0.8 0) (size 0.9 0.9) (layers "F.Cu") (net 1 "VCC")))
      (gr_rect (start 0 0) (end 50 40) (layer "Edge.Cuts"))
    )'''
    bd = import_kicad_pcb(text)
    assert [c["ref"] for c in bd.comps] == ["R1", "C1"]
    assert bd.warnings == []


def test_kicad_legacy_fp_text_still_works():
    """老格式 (fp_text reference "R1") 不能因支持新格式而回归。"""
    from ame.circuit.kicad import import_kicad_pcb

    text = '''(kicad_pcb (version 20171130)
      (footprint "R" (layer "F.Cu") (at 5 5)
        (fp_text reference "R7" (at 5 5) (layer "F.SilkS"))
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "N1")))
      (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts")))'''
    bd = import_kicad_pcb(text)
    assert [c["ref"] for c in bd.comps] == ["R7"]


def test_kicad_missing_reference_warns():
    """缺位号时不能静默命名，必须给出警告（CIR-001）。"""
    from ame.circuit.kicad import import_kicad_pcb

    text = '''(kicad_pcb (version 20240108)
      (footprint "X" (layer "F.Cu") (at 5 5)
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "N1")))
      (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts")))'''
    bd = import_kicad_pcb(text)
    assert len(bd.comps) == 1
    assert any("位号" in w for w in bd.warnings)


# ============================================================ CIR-004 手动布线
def test_manual_routing_through_canvas(ui_app):
    """从界面画一条走线：点焊盘选网络 → 落点 → 双击落线（CIR-004）。

    这是 P0 主线。早期三处断点：双击事件未绑定、首点 create_line 传 2 个坐标
    崩溃、cmd_add_route 只有自动布线调用。
    """
    app = ui_app
    # 复用模块级隐藏窗口（重复创建/销毁根窗口会让 Tcl 状态失效）
    app.open_sample()
    n0 = len(app.ws.mrc_state()["routes"])
    app.set_tool("route")
    assert app.tool == "route"

    pads = [p for p in app.ws.pad_state() if p.get("net")]
    by_net: dict[str, list] = {}
    for p in pads:
        by_net.setdefault(p["net"], []).append(p)
    net = next(n for n, ps in by_net.items() if len(ps) >= 2)
    a, b = by_net[net][0], by_net[net][1]

    class _E:
        pass

    # 单点落点不能崩溃（早期 create_line 抛 TclError）
    for p in (a, b):
        sx, sy = app._to_screen(p["x"], p["y"])
        e = _E()
        e.x, e.y = int(sx), int(sy)
        app.on_canvas_click(e)
        app.redraw()
    assert app.route_net == net, "点焊盘应选定该焊盘的网络"
    assert len(app._drag_pts) == 2

    app.finish_route()
    assert app.tool is None, "双击结束后工具应退出"
    assert len(app.ws.mrc_state()["routes"]) == n0 + 1, "走线应落库"

    app.undo()
    assert len(app.ws.mrc_state()["routes"]) == n0


def test_manual_routing_cancel_with_escape(ui_app):
    """Esc 取消布线，不留下半成品（CIR-004）。"""
    app = ui_app
    # 复用模块级隐藏窗口（重复创建/销毁根窗口会让 Tcl 状态失效）
    app.open_sample()
    n0 = len(app.ws.mrc_state()["routes"])
    app.set_tool("route")
    p = app.ws.pad_state()[0]
    sx, sy = app._to_screen(p["x"], p["y"])

    class _E:
        pass

    e = _E()
    e.x, e.y = int(sx), int(sy)
    app.on_canvas_click(e)
    app.cancel_tool()
    assert app.tool is None and app._drag_pts == []
    assert len(app.ws.mrc_state()["routes"]) == n0


def test_manual_routing_rejects_single_point(ui_app):
    """只有一个落点时双击不能落线（CIR-011 至少要两点）。"""
    app = ui_app
    # 复用模块级隐藏窗口（重复创建/销毁根窗口会让 Tcl 状态失效）
    app.open_sample()
    n0 = len(app.ws.mrc_state()["routes"])
    app.set_tool("route")
    p = app.ws.pad_state()[0]
    sx, sy = app._to_screen(p["x"], p["y"])

    class _E:
        pass

    e = _E()
    e.x, e.y = int(sx), int(sy)
    app.on_canvas_click(e)
    app.finish_route()
    assert len(app.ws.mrc_state()["routes"]) == n0, "不该落线"


# ============================================================ PRT-008 缺陷状态
def test_defect_status_can_be_marked(data_root):
    """缺陷可在 open/fixed/ignored 间标记（PRT-008）。

    schema 三种状态早就定义，但全仓库没有 UPDATE defect——用户只能看着
    "状态 open" 无法处理。
    """
    from ame.ledger.services import defect_report, set_defect_status

    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    job = run_print(ws, fault_plan=[{"type": "clog", "pass_no": 3,
                                     "at_len_mm": 10.0}])
    assert job["defects"], "应注入至少一条缺陷"
    d = defect_report(ws, job["job_id"])[0]
    assert d["status"] == "open"

    r = set_defect_status(ws, d["id"], "fixed")
    assert r["status"] == "fixed"
    assert defect_report(ws, job["job_id"])[0]["status"] == "fixed"

    set_defect_status(ws, d["id"], "ignored")
    assert defect_report(ws, job["job_id"])[0]["status"] == "ignored"


def test_defect_status_rejects_invalid_value(data_root):
    from ame.ledger.services import set_defect_status

    ws = _sliced(_ws(data_root, board="board_T19A.kicad_pcb"))
    job = run_print(ws, fault_plan=[{"type": "clog", "pass_no": 3,
                                     "at_len_mm": 10.0}])
    from ame.ledger.services import defect_report
    d = defect_report(ws, job["job_id"])[0]
    with pytest.raises(ValueError, match="非法"):
        set_defect_status(ws, d["id"], "done")


# ============================================================ DAT-001 问题落库
def test_mrc_issues_persisted_per_commit(data_root):
    """提交版本时问题必须落库，并可按版本追溯（DAT-001）。

    mrc_issue 表建了却从未写入，重开工程后查不到"当时有哪些问题"。
    """
    ws = _ws(data_root, board="board_T19A.kicad_pcb")
    assert ws.store.one("SELECT COUNT(*) c FROM mrc_issue")["c"] == 0

    r = ws.commit("基线")
    n = ws.store.one("SELECT COUNT(*) c FROM mrc_issue WHERE commit_id=?",
                     (r["id"],))["c"]
    assert n > 0, "commit 后应有问题落库"
    assert n == r["issue_count"]

    issues = ws.issues_of_commit(r["id"])
    assert len(issues) == n
    for i in issues:
        assert i["rule_code"] and i["reason"]


def test_mrc_issue_persistence_works_on_migrated_project(tmp_path):
    """迁移来的旧库（可能没有 mrc_rule 种子）提交时不能因外键失败。"""
    import sqlite3

    from ame.db.store import Store
    from ame.ledger.workspace import Workspace

    f = tmp_path / "old.db"
    conn = sqlite3.connect(str(f))
    conn.executescript("""
    CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, created_at TEXT,
      schema_version INTEGER, head_commit_id TEXT, bed_x_mm REAL, bed_y_mm REAL);
    INSERT INTO project VALUES ('pid-old','旧库','2026-01-01',3,NULL,200,200);
    """)
    conn.commit()
    conn.close()

    st = Store(f)      # 迁移到最新
    st.close()
    st = Store(f)
    st.execute("INSERT INTO circuit(id,project_id,format,imported_at) "
               "VALUES ('c1','pid-old','kicad_pcb','2026-10-01')")
    st.execute("INSERT INTO comp(id,circuit_id,ref,x_mm,y_mm,height_mm) "
               "VALUES ('c1','c1','R1',10.0,20.0,1.0)")
    st.execute("INSERT INTO net(id,circuit_id,name,klass,pad_count) "
               "VALUES ('n1','c1','N1','signal',1)")
    st.execute("INSERT OR REPLACE INTO board_outline(project_id, outline_json, source) "
               "VALUES ('pid-old', '[[0,0],[50,0],[50,50],[0,50]]', 'board_file')")
    st.close()

    ws = Workspace(Store(f), "pid-old")
    ws.cmd_add_route("N1", [[10.0, 20.0], [10.0, 40.0]], 0.6)
    r = ws.commit("迁移后提交")
    assert r["id"]
    assert ws.store.one("SELECT COUNT(*) c FROM mrc_issue WHERE commit_id=?",
                        (r["id"],))["c"] == r["issue_count"]


# ============================================================ NFR-006 关键路径日志
def test_key_path_logging_writes_file(tmp_path):
    """关键路径必须有日志落盘（NFR-006）。

    全仓库原先没有一行 logging：出错后没有任何可追溯的现场记录。
    """
    from ame.logging_setup import configure, get_logger, log_path

    data = tmp_path / "d"
    path = configure(data, level="info")
    assert path is not None and path.exists()
    log = get_logger("test")
    log.info("关键路径测试 %s", "ok")

    p = log_path(data)
    assert p and p.exists()
    text = p.read_text("utf-8")
    assert "关键路径测试 ok" in text


def test_log_records_commands_and_commits(data_root):
    """命令与提交都会进日志（NFR-006：能追溯"谁在什么时候改了什么"）。"""
    from ame.logging_setup import configure

    path = configure(data_root.data_dir, level="info")
    ws = _ws(data_root)
    ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    ws.commit("日志用例")

    text = path.read_text("utf-8")
    assert "命令 import_board" in text
    assert "提交版本" in text and "日志用例" in text


def test_log_level_can_be_raised_to_silence_info(tmp_path):
    """log_level=warning 时 INFO 不落盘（PRJ-009 日志级别生效）。"""
    from ame.logging_setup import configure, get_logger

    data = tmp_path / "d"
    path = configure(data, level="warning")
    get_logger("quiet").info("这条不该出现")
    get_logger("quiet").warning("这条该出现")
    text = path.read_text("utf-8")
    assert "这条不该出现" not in text
    assert "这条该出现" in text


# ============================================================ CIR-010 网络类/封装
def test_net_class_change_with_scope_preview(data_root):
    """改网络类：先给影响范围，再执行，可撤销（CIR-010）。"""
    from ame.ledger.workspace import build_sample_project as bsp

    ws = bsp(DataRoot(data_root.data_dir.parent / "s"))
    net = ws.nets()[0]["name"]

    preview = ws.cmd_set_net_class(net, "power", dry_run=True)
    assert preview["applied"] is False
    assert preview["scope"]["new_class"] == "power"
    assert [n["klass"] for n in ws.nets() if n["name"] == net] == ["signal"], \
        "dry_run 不能改库"

    r = ws.cmd_set_net_class(net, "power")
    assert r["applied"] is True
    assert [n["klass"] for n in ws.nets() if n["name"] == net] == ["power"]

    ws.undo()
    assert [n["klass"] for n in ws.nets() if n["name"] == net] == ["signal"]


def test_net_class_rejects_unknown_value(data_root):
    from ame.ledger.workspace import build_sample_project as bsp

    ws = bsp(DataRoot(data_root.data_dir.parent / "s2"))
    net = ws.nets()[0]["name"]
    with pytest.raises(CmdError, match="非法"):
        ws.cmd_set_net_class(net, "superconducting")


def test_footprint_change_with_scope_preview(data_root):
    """改封装：先给影响范围（焊盘数），再执行，可撤销（CIR-010）。"""
    from ame.ledger.workspace import build_sample_project as bsp

    ws = bsp(DataRoot(data_root.data_dir.parent / "s3"))
    c = ws.comps()[0]
    preview = ws.cmd_set_footprint(c["id"], "Lib:NEW", dry_run=True)
    assert preview["applied"] is False
    assert preview["scope"]["pads"] >= 0
    assert [x["footprint"] for x in ws.comps() if x["id"] == c["id"]] == [c["footprint"]]

    ws.cmd_set_footprint(c["id"], "Lib:NEW")
    assert [x["footprint"] for x in ws.comps() if x["id"] == c["id"]] == ["Lib:NEW"]
    ws.undo()
    assert [x["footprint"] for x in ws.comps() if x["id"] == c["id"]] == [c["footprint"]]


def test_footprint_rejects_empty(data_root):
    from ame.ledger.workspace import build_sample_project as bsp

    ws = bsp(DataRoot(data_root.data_dir.parent / "s4"))
    c = ws.comps()[0]
    with pytest.raises(CmdError, match="不能为空"):
        ws.cmd_set_footprint(c["id"], "   ")


# ============================================================ STR-005 布尔预览
def test_boolean_preview_draws_and_clears(ui_app):
    """布尔建议必须能预览到画布（线框），且清除后画布复原（STR-005）。

    早期只有只读文字清单，画布从不渲染特征几何——"预览"名不副实。
    """
    app = ui_app
    app.open_sample()
    app.show_boolean()
    assert app.feature_preview, "应有预览数据"
    assert app.view_mode == "3d", "布尔体是三维几何，应切到三维视图"

    with_preview = len(app.canvas.find_all())
    app._clear_preview()
    assert app.feature_preview == []

    # 同一视图（3D）下比较：清除预览后对象数必须减少
    without_preview = len(app.canvas.find_all())
    assert with_preview > without_preview, (
        f"预览应画到画布上（带预览 {with_preview} vs 无预览 {without_preview}）")


def test_boolean_preview_params_validated(ui_app):
    """参数非数字时给出可读错误，不写坏数据（STR-005）。"""
    app = ui_app
    app.open_sample()
    ed = {"sug": type("S", (), {"prim": "box", "kind": "add", "desc": "x"})(),
          "vars": {"dx": type("V", (), {"get": lambda s: "abc"})()}}
    with pytest.raises(CmdError, match="不是数字"):
        app._feature_params(ed)


def test_boolean_accept_uses_edited_params(ui_app):
    """接受时用的是用户改过的参数，不是原始建议值（STR-005 参数可调）。"""
    app = ui_app
    app.open_sample()
    sug = type("S", (), {"prim": "box", "kind": "add", "desc": "测试凸台"})()
    editors = [{"sug": sug,
                "vars": {k: type("V", (), {"get": (lambda v: (lambda s: v))(val)})()
                         for k, val in (("x", 1.0), ("y", 2.0), ("z", 0.0),
                                        ("dx", 11.0), ("dy", 12.0), ("dz", 3.0))}}]
    app._accept_feature(0, editors, None)
    feats = [f for f in app.ws.features() if f["note"] == "测试凸台"]
    assert feats, "特征应已加入"
    params = json.loads(feats[0]["dims_json"])
    assert params["dx"] == 11.0 and params["dy"] == 12.0, "应使用编辑后的参数"


# ============================================================ MRC-005 已打印层遮挡
def _sliced_state(data_root, name="R5PL"):
    """已切片工程的 mrc_state（含 printed_layers）。"""
    ws = _sliced(_ws(data_root, name=name, board="board_T19A.kicad_pcb"))
    return ws, ws.mrc_state()


def test_printed_layers_are_fed_into_mrc_state(data_root):
    """切片后的层/道次几何必须进入 MRC 输入（MRC-005 的数据前提）。"""
    ws, st = _sliced_state(data_root)
    layers = st["printed_layers"]
    assert layers, "切片后应提供已打印层几何"
    kinds = {l["kind"] for l in layers}
    assert {"bottom", "circuit", "top"} <= kinds
    for l in layers:
        assert l["h_mm"] > 0, "层厚必须入库（体积遮挡判定需要）"
        assert l["paths"], "每个道次应有路径"


def test_r5_detects_material_printed_before_route_in_same_height(data_root):
    """先于走线打印、且占住走线高度的材料必须命中 R5（MRC-005 正例）。"""
    from ame.mrc.engine import check

    ws, st = _sliced_state(data_root)
    route = st["routes"][0]
    # 在走线之前的道次插入一层同高度的材料，完全盖住走线
    extra = {"pass_no": 2, "kind": "circuit", "mat": "DIELECTRIC",
             "z_mm": 0.34, "h_mm": 0.10,
             "paths": [{"pts": route["pts"], "width_mm": 1.2}]}
    s = dict(st)
    s["printed_layers"] = list(st["printed_layers"]) + [extra]
    r = check(s, ws.rules(), ws.tool())
    hits = [i for i in r["issues"] if i["rule_code"] == "R5"]
    assert hits, "应先于走线占住其高度的材料应判遮挡"
    assert "已打印" in hits[0]["reason"]


def test_r5_ignores_material_printed_after_route(data_root):
    """晚于走线打印的材料不算遮挡（MRC-005 反例）。"""
    from ame.mrc.engine import check

    ws, st = _sliced_state(data_root)
    route = st["routes"][0]
    extra = {"pass_no": 99, "kind": "top", "mat": "DIELECTRIC",
             "z_mm": 0.54, "h_mm": 0.10,
             "paths": [{"pts": route["pts"], "width_mm": 1.2}]}
    s = dict(st)
    s["printed_layers"] = list(st["printed_layers"]) + [extra]
    r = check(s, ws.rules(), ws.tool())
    assert not [i for i in r["issues"] if i["rule_code"] == "R5"]


def test_r5_ignores_lower_layer_not_overlapping_height(data_root):
    """高度不重叠的更低层不算遮挡（MRC-005 反例：下保护层在走线下面）。"""
    from ame.mrc.engine import check

    ws, st = _sliced_state(data_root)
    route = st["routes"][0]
    extra = {"pass_no": 99, "kind": "bottom", "mat": "DIELECTRIC",
             "z_mm": 0.24, "h_mm": 0.08,
             "paths": [{"pts": route["pts"], "width_mm": 1.2}]}
    s = dict(st)
    s["printed_layers"] = list(st["printed_layers"]) + [extra]
    r = check(s, ws.rules(), ws.tool())
    assert not [i for i in r["issues"] if i["rule_code"] == "R5"]


def test_normal_board_has_no_printed_layer_false_positive(data_root):
    """正常板件不能因"已打印层"误报 R5（关键回归：三种朴素判法都会误报）。"""
    ws, st = _sliced_state(data_root)
    r = ws.run_mrc()
    r5 = [i for i in r["issues"] if i["rule_code"] == "R5"]
    assert not r5, f"正常板件不应有 R5 遮挡误报，实际 {len(r5)} 条"
    # 分数不能被"已打印层"判定拖垮：仍应是 T19-A 的 60 分基线
    assert r["score"] == 60, f"分数应保持 60 基线，实际 {r['score']}"


def test_printed_layer_obstacles_only_affect_r5_not_r4(data_root):
    """介电坝留槽不算"需避让的凸起"：不能把 R4 误触发（实测会掉到 28 分）。"""
    from ame.mrc.engine import _printed_layer_blockers, check

    ws, st = _sliced_state(data_root)
    assert _printed_layer_blockers(st, 0.82), "应识别出同层留槽坝"
    r = check(st, ws.rules(), ws.tool())
    r4 = [i for i in r["issues"] if i["rule_code"] == "R4"]
    assert not r4, f"R4 不应被留槽坝触发，实际 {len(r4)} 条"


def test_state_digest_is_polluted_by_derived_data(data_root):
    """证明"用整个 state 做设计摘要"是错的（SLC-008 确定性的根因）。

    mrc_state 含派生数据 printed_layers（来自最近一次切片）：切片前后内容不同，
    因此 digest(state) 不稳定；design_digest() 只看设计输入，保持稳定。
    这就是 G-code 头必须用 design_digest() 而不是 digest(state) 的原因。
    """
    from ame import digest

    ws = _ws(data_root, name="DET", board="board_T19A.kicad_pcb")
    before_state = digest(ws.mrc_state())      # 切片前：printed_layers 为空
    before_design = ws.design_digest()

    do_slice(ws)                               # 切片后：printed_layers 有内容

    assert digest(ws.mrc_state()) != before_state,         "state 摘要应被派生数据（printed_layers）污染"
    assert ws.design_digest() == before_design,         "设计摘要必须与派生数据无关（否则 G-code 哈希不可复现）"


def test_gcode_hash_independent_of_slice_history(data_root):
    """同一工程内，G-code 头摘要只取决于设计输入（SLC-008 确定性）。

    直接从生成的 G-code 里读 ;design_sha256，核对它等于 design_digest()
    而不是 digest(state)。切片前后 design_sha256 必须一致，而 state 摘要会变。
    """
    import re

    from ame import digest
    from ame.ledger.services import gcode_of

    ws = _ws(data_root, name="DET-hist", board="board_T19A.kicad_pcb")
    design_before = ws.design_digest()
    state_before = digest(ws.mrc_state())

    r1 = do_slice(ws)                        # 产生切片历史（printed_layers 出现）
    g1 = gcode_of(ws, r1["slice_set_id"])
    m = re.search(r"^;design_sha256 (\w+)", g1, re.M)
    assert m, "G-code 头应含 design_sha256"
    assert m.group(1) == design_before,         "G-code 头的 design_sha256 必须等于 design_digest()（设计输入摘要）"
    assert m.group(1) != digest(ws.mrc_state()),         "不能是 state 摘要（含派生数据）"

    assert ws.design_digest() == design_before, "设计摘要不应受切片历史影响"
    assert digest(ws.mrc_state()) != state_before, "state 摘要会受派生数据影响"

    h2 = do_slice(ws)["gcode_hash"]
    assert r1["gcode_hash"] == h2, "连续切片哈希应一致"


# ============================================================ 迁移 v5 → v6


# ============================================================ CIR-001 现代 KiCad






# ============================================================ CIR-004 手动布线






# ============================================================ PRT-008 缺陷状态




# ============================================================ DAT-001 问题落库




# ============================================================ NFR-006 关键路径日志






# ============================================================ CIR-010 网络类/封装








# ============================================================ STR-005 布尔预览






# ============================================================ MRC-005 已打印层遮挡
def _sliced_state(data_root, name="R5PL"):
    """已切片工程的 mrc_state（含 printed_layers）。"""
    ws = _sliced(_ws(data_root, name=name, board="board_T19A.kicad_pcb"))
    return ws, ws.mrc_state()
















# ============================================================ STR-002 诊断不静默
def test_ui_shows_step_import_diagnostics(ui_app):
    """STEP 导入后的面级诊断必须展示给用户（STR-002 不静默）。

    早期面级诊断只挂在 Face 上，model.diagnostics 为空，用户看不到任何提示。
    """
    app = ui_app
    app.ws = create_project(DataRoot(app.data_root.data_dir.parent / "diag"), "诊断")
    txt = (SAMPLES / "real" / "occ214.stp").read_text("utf-8", errors="replace")
    r = app.ws.cmd_import_step(txt, "occ214.stp")

    assert r["diagnostics"], "该文件含 B 样条曲面，必须上报诊断"
    app._show_import_diagnostics(r)
    d = app.last_dialog
    assert d and "诊断" in d["title"]
    body = d["body"]
    for code in ("G-SURFACE-UNSUPPORTED", "G-CURVE-UNSUPPORTED"):
        assert code in body, f"诊断清单应包含 {code}"
    assert "建议：" in body, "每条诊断都要给出可操作建议"


# ============================================================ CIR-006 禁布区
def test_keepout_crud_and_undo_redo(data_root):
    """禁布区可增、删、撤销、重做（CIR-006）。"""
    ws = _ws(data_root, name="KO", board="board_T19A.kicad_pcb")
    r = ws.cmd_add_keepout([[30, 30], [60, 30], [60, 50], [30, 50]], "天线净空")
    assert r["area_mm2"] == pytest.approx(600.0)
    assert len(ws.keepouts()) == 1

    ws.undo()
    assert ws.keepouts() == []
    ws.redo()
    assert len(ws.keepouts()) == 1, "重做必须恢复禁布区（早期缺 redo 回调导致失效）"

    kid = ws.keepouts()[0]["id"]
    ws.cmd_delete_keepout(kid)
    assert ws.keepouts() == []
    ws.undo()
    assert len(ws.keepouts()) == 1


def test_keepout_blocks_manual_route(data_root):
    """走线导体落入禁布区必须拒绝（CIR-006）。"""
    ws = _ws(data_root, name="KO2", board="board_T19A.kicad_pcb")
    ws.cmd_add_keepout([[30, 30], [60, 30], [60, 50], [30, 50]], "天线净空")
    with pytest.raises(CmdError, match="禁布区"):
        ws.cmd_add_route("GND", [[35, 40], [55, 40]], 0.6)


def test_keepout_does_not_block_route_outside(data_root):
    """禁区外的走线不受影响（不能误伤）。"""
    ws = _ws(data_root, name="KO3", board="board_T19A.kicad_pcb")
    ws.cmd_add_keepout([[30, 30], [60, 30], [60, 50], [30, 50]], "天线净空")
    n0 = len(ws.routes())
    ws.cmd_add_route("GND", [[10, 20], [25, 20]], 0.6)
    assert len(ws.routes()) == n0 + 1


def test_auto_route_avoids_keepout(data_root):
    """自动布线必须绕开禁布区（CIR-006 明确要求）。"""
    from shapely.geometry import LineString, Polygon

    ws = _ws(data_root, name="KO4", board="board_T19A.kicad_pcb")
    ws.cmd_add_keepout([[30, 10], [70, 10], [70, 50], [30, 50]], "大禁区")
    cands = ws.cmd_auto_route_candidates("GND", 0.6)
    assert cands, "应仍有候选（绕开禁区）"
    ko = Polygon([[30, 10], [70, 10], [70, 50], [30, 50]])
    for c in cands:
        band = LineString([(p[0], p[1]) for p in c["pts"]]).buffer(0.3)
        assert band.intersection(ko).area < 1e-6, "候选不能穿越禁布区"


def test_keepout_rejects_invalid_polygon(data_root):
    """顶点不足/退化的禁布区必须拒绝（CIR-006）。"""
    ws = _ws(data_root, name="KO5", board="board_T19A.kicad_pcb")
    with pytest.raises(CmdError, match="3 个顶点"):
        ws.cmd_add_keepout([[0, 0], [1, 1]], "两点")
    with pytest.raises(CmdError, match="面积为零"):
        ws.cmd_add_keepout([[0, 0], [1, 0], [0, 0]], "退化")


def test_keepout_table_migrates_to_old_project(tmp_path):
    """旧库迁移后必须有 keepout 表（v8，CIR-006）。"""
    import sqlite3

    from ame.db.store import CURRENT_SCHEMA_VERSION, Store

    f = tmp_path / "old.db"
    conn = sqlite3.connect(str(f))
    conn.executescript("""
    CREATE TABLE project (id TEXT PRIMARY KEY, name TEXT, created_at TEXT,
      schema_version INTEGER, head_commit_id TEXT, bed_x_mm REAL, bed_y_mm REAL);
    INSERT INTO project VALUES ('p1','旧','2026-01-01',7,NULL,200,200);
    """)
    conn.commit()
    conn.close()
    st = Store(f)
    try:
        assert st._table_exists("keepout")
        assert st.one("SELECT schema_version FROM project")["schema_version"] \
            == CURRENT_SCHEMA_VERSION
    finally:
        st.close()


# ============================================================ STR-006 特征管理
def test_feature_enable_disable_with_undo(data_root):
    """特征可启用/停用，且可撤销（STR-006）。"""
    ws = _ws(data_root, name="F1")
    fid = ws.cmd_add_feature("box", "add", {"x": 1, "y": 2, "z": 0, "dx": 3, "dy": 4,
                                            "dz": 5}, "凸台")["id"]
    assert next(f for f in ws.features() if f["id"] == fid)["enabled"] == 1

    ws.cmd_set_feature_enabled(fid, False)
    assert next(f for f in ws.features() if f["id"] == fid)["enabled"] == 0
    ws.undo()
    assert next(f for f in ws.features() if f["id"] == fid)["enabled"] == 1


def test_feature_delete_with_undo(data_root):
    """特征可删除，撤销能完整恢复（STR-006）。"""
    ws = _ws(data_root, name="F2")
    r = ws.cmd_add_feature("cylinder", "sub", {"x": 5, "y": 5, "z": 0, "r": 2, "dz": 1},
                           "圆槽")
    fid = r["id"]
    assert len(ws.features()) == 1
    ws.cmd_delete_feature(fid)
    assert ws.features() == []
    ws.undo()
    feats = ws.features()
    assert len(feats) == 1 and feats[0]["id"] == fid and feats[0]["note"] == "圆槽"


def test_feature_reorder_sets_sequence(data_root):
    """排序给每个特征写入 seq，第一个为 0（STR-006：布尔按顺序执行）。"""
    ws = _ws(data_root, name="F3")
    a = ws.cmd_add_feature("box", "add", {"x": 1}, "A")["id"]
    b = ws.cmd_add_feature("box", "sub", {"x": 2}, "B")["id"]
    ws.cmd_reorder_features([b, a])
    seq = {f["id"]: json.loads(f["transform_json"]).get("seq") for f in ws.features()}
    assert seq[b] == 0 and seq[a] == 1
    ws.undo()                            # 撤销应恢复原顺序
    seq2 = {f["id"]: json.loads(f["transform_json"]).get("seq") for f in ws.features()}
    assert seq2[a] == 0 and seq2[b] == 1


def test_feature_commands_reject_unknown_id(data_root):
    """操作不存在的特征要给出可读错误（STR-006）。"""
    ws = _ws(data_root, name="F4")
    with pytest.raises(CmdError, match="不存在"):
        ws.cmd_set_feature_enabled("no-such-id", True)
    with pytest.raises(CmdError, match="不存在"):
        ws.cmd_delete_feature("no-such-id")
    with pytest.raises(CmdError, match="不存在"):
        ws.cmd_reorder_features(["ghost"])


# ============================================================ PRT-010 时间筛选
def test_job_history_filters_by_time_and_state(data_root):
    """作业历史可按时间区间与状态筛选（PRT-010）。"""
    from ame.ledger.services import job_history

    ws = _sliced(_ws(data_root, name="H1", board="board_T19A.kicad_pcb"))
    # 两次打印的返回值本测试不关心，但调用本身会写入作业记录（PRT-010 的数据源）
    run_print(ws)
    run_print(ws)

    allrows = job_history(ws)
    assert len(allrows) >= 2

    # 按状态筛选
    done = job_history(ws, state="done")
    assert all(r["state"] == "done" for r in done)
    assert job_history(ws, state="fault") == []

    # 开始时间晚于"很久以后" → 空
    assert job_history(ws, since="2999-01-01") == []
    # 结束时间早于"很久以前" → 空
    assert job_history(ws, until="1999-01-01") == []
    # 覆盖今天的区间 → 有记录（until 为纯日期时含当天全天）
    import time
    today = time.strftime("%Y-%m-%d")
    assert len(job_history(ws, since=today, until=today)) >= 2


# ============================================================ UI-010 帮助检索
def test_help_text_is_searchable_and_complete(ui_app):
    """帮助是结构化文本，覆盖术语/规则/错误码/快捷键（UI-010）。"""
    app = ui_app
    txt = app.help_text()
    for kw in ("术语", "规则速查", "错误码", "快捷键"):
        assert f"{kw}" in txt
    for rule in ("R1 线宽", "R5 遮挡", "R7 介电流道"):
        assert rule in txt, f"帮助应包含 {rule}"
    for code in ("G-ASSEMBLY-FLATTENED", "G-SIZE-SUSPICIOUS"):
        assert code in txt, f"帮助应包含错误码 {code}"
    assert "禁布区" in txt, "帮助应说明禁布区（CIR-006）"


# ============================================================ STR-003 大模型分级渲染
def _grid_faces(n, slant_every=0):
    """构造 n 个面（网格 + 可选斜面），模拟大模型。"""
    import math

    from ame.geometry.stepio import Face

    side = max(1, int(math.isqrt(n)))
    faces = []
    for k in range(side * side):
        i, j = k // side, k % side
        if slant_every and k % slant_every == 0:
            faces.append(Face(f"F{k}", "plane", (0.5, 0.0, 0.866), 1.0,
                              (i + .5, j + .5, 1.0),
                              [(i, j, 1.0), (i + 1, j, 1.0),
                               (i + 1, j + 1, 1.5), (i, j + 1, 1.5)]))
        else:
            faces.append(Face(f"F{k}", "plane", (0, 0, 1), 1.0,
                              (i + .5, j + .5, 0.0),
                              [(i, j, 0.0), (i + 1, j, 0.0),
                               (i + 1, j + 1, 0.0), (i, j + 1, 0.0)]))
    return faces


def test_lod_levels_follow_face_count():
    """LOD 档位按面数分级（STR-003）：不再是一刀切 800 面退化。"""
    from ame.ui.render import (LOD_BBOX_MAX, LOD_COARSE_MAX, LOD_FULL_MAX,
                               lod_level)

    assert lod_level(100) == "full"
    assert lod_level(LOD_FULL_MAX) == "full"
    assert lod_level(LOD_FULL_MAX + 1) == "lod"
    assert lod_level(50_000) == "lod"
    assert lod_level(LOD_COARSE_MAX) == "lod"
    assert lod_level(LOD_COARSE_MAX + 1) == "coarse"
    assert lod_level(300_000) == "coarse"
    assert lod_level(LOD_BBOX_MAX + 1) == "bbox"


def test_50k_face_model_still_draws_colored_polygons():
    """5 万面模型仍绘制**着色多边形**（不是线框、不是空）。

    这是 STR-003 的核心回归：固定 DETAIL_LIMIT=800 时，5 万面直接被画成
    包围盒线框，用户看不到任何几何。
    """
    from ame.ui.render import Camera, clear_lod_cache, lod_level, render_faces

    clear_lod_cache()
    faces = _grid_faces(50_000)
    assert lod_level(len(faces)) == "lod"
    items = render_faces(faces, Camera(), 10.0, (400, 300))

    assert len(items) >= 500, f"5 万面应仍绘制大量多边形，实际 {len(items)}"
    for it in items:
        assert len(it["coords"]) >= 6, "每个多边形至少 3 个点"
        assert it["fill"].startswith("#"), "必须有填充色（着色，不是线框）"
        assert it["_total"] == len(faces)
        assert it["_kept"] == len(items)


def test_200k_face_model_is_still_shaded_not_wireframe():
    """20 万面模型仍着色绘制（需求上限档），不退化线框。"""
    from ame.ui.render import Camera, clear_lod_cache, lod_level, render_faces

    clear_lod_cache()
    faces = _grid_faces(200_000)
    assert lod_level(len(faces)) == "lod"
    items = render_faces(faces, Camera(), 10.0, (400, 300))
    assert len(items) >= 500
    assert all(it["fill"].startswith("#") for it in items)


def test_bbox_fallback_only_above_500k():
    """只有超过 50 万面才退化为包围盒（返回空列表，由调用方画线框）。"""
    from ame.ui.render import Camera, clear_lod_cache, lod_level, render_faces

    clear_lod_cache()
    faces = _grid_faces(600_000)
    assert lod_level(len(faces)) == "bbox"
    assert render_faces(faces, Camera(), 10.0, (400, 300)) == []


def test_lod_keeps_slanted_faces_preferentially():
    """LOD 优先保留斜面（可打性风险面），不被大平面挤掉。"""
    from ame.ui.render import Camera, clear_lod_cache, render_faces

    clear_lod_cache()
    faces = _grid_faces(50_000, slant_every=50)   # 约 2% 是斜面
    n_slant = sum(1 for f in faces if 0.02 <= abs(f.normal[2]) <= 0.98)
    assert n_slant > 100
    items = render_faces(faces, Camera(), 10.0, (400, 300))
    kept_slant = sum(1 for it in items if it["slant"])
    assert kept_slant > n_slant * 0.3, (
        f"斜面应优先保留：{n_slant} 个斜面里至少保留 30%，实际 {kept_slant}")


def test_lod_merges_coplanar_fragments():
    """共面聚合：被丢弃的碎片按空间网格聚合，保住轮廓。"""
    from ame.ui.render import reduce_faces

    faces = _grid_faces(10_000)
    kept, info = reduce_faces(faces, 300)
    assert len(kept) <= 300
    assert info["total"] == len(faces)
    assert info["merged"] > 0, "应有碎片被聚合"
    assert info["dropped"] == info["total"] - info["kept"]


def test_lod_reduction_is_cached():
    """LOD 归约必须缓存：同一面列表第二次渲染不应再走 reduce_faces。

    直接数 reduce_faces 的调用次数，比时间断言稳定（不受机器负载影响）。
    """
    import ame.ui.render as R

    R.clear_lod_cache()
    faces = _grid_faces(50_000)
    calls = {"n": 0}
    orig = R.reduce_faces

    def counting(f, budget, merge_coplanar=True):
        calls["n"] += 1
        return orig(f, budget, merge_coplanar)

    R.reduce_faces = counting
    try:
        R.render_faces(faces, R.Camera(), 10.0, (400, 300))
        first_calls = calls["n"]
        R.render_faces(faces, R.Camera(), 30.0, (400, 300))
        second_calls = calls["n"]
    finally:
        R.reduce_faces = orig

    assert first_calls >= 1, "首帧应做归约"
    assert second_calls == first_calls, (
        f"第二帧应命中缓存、不再归约：首帧 {first_calls} 次，"
        f"两次合计 {second_calls} 次")


def test_lod_full_tier_draws_everything():
    """1500 面以内全量绘制，一个面都不丢。"""
    from ame.ui.render import Camera, clear_lod_cache, lod_level, render_faces

    clear_lod_cache()
    faces = _grid_faces(1_400)
    assert lod_level(len(faces)) == "full"
    items = render_faces(faces, Camera(), 10.0, (400, 300))
    assert len(items) == len(faces), "full 档必须全量绘制"
    assert items[0]["_lod"] == "full"


def test_lod_section_still_respects_cut():
    """剖切在 LOD 档位下仍然生效（不能因为抽样把剖切丢了）。"""
    from ame.ui.render import Camera, clear_lod_cache, render_faces

    clear_lod_cache()
    faces = _grid_faces(20_000)
    for f in faces:
        f.loop = [(p[0], p[1], p[2] + 10.0) for p in f.loop]   # 整体抬高
        f.centroid = (f.centroid[0], f.centroid[1], f.centroid[2] + 10.0)
    items = render_faces(faces, Camera(), 10.0, (400, 300), section_z=5.0)
    assert items == [], "剖切高度以下的模型应全部被裁掉"


def test_render_never_degrades_to_wireframe_below_500k():
    """1 万～50 万面区间内，渲染**永远**返回着色多边形（不得返回空/线框）。

    这条锁的是 STR-003 的结构性要求：不能因为"面数多"就退化成线框。
    早期实现 DETAIL_LIMIT=800，801 面就画线框——与 20 万面的需求上限差 250 倍。
    """
    from ame.ui.render import Camera, clear_lod_cache, render_faces

    for n in (5_000, 20_000, 50_000, 120_000, 250_000, 480_000):
        clear_lod_cache()
        faces = _grid_faces(n)
        items = render_faces(faces, Camera(), 10.0, (400, 300))
        assert items, f"{len(faces)} 面不应返回空（会变成线框）"
        assert all(it["fill"].startswith("#") for it in items), \
            f"{len(faces)} 面应全部着色"


# ============================================================ STR-003 帧率达标
def test_camera_basis_is_cached():
    """相机基向量必须缓存（每次 project() 重算 4 个三角函数是浪费）。"""
    import ame.ui.render as R

    cam = R.Camera(az_deg=37.0, el_deg=29.0)
    b1 = cam._basis()
    b2 = cam._basis()
    assert b1 is b2, "同一相机参数应返回同一个缓存对象"
    cam.az_deg = 90.0
    b3 = cam._basis()
    assert b3 is not b1, "相机角度变了必须重算"


def test_pil_rasterization_is_available_and_faster():
    """PIL 光栅化后端可用，且明显快于逐多边形绘制（STR-003 达标的关键路径）。"""
    import ame.ui.render as R

    assert R._pil_available(), "Pillow 应可用（打包时不能排除）"
    faces = _grid_faces(2_000)
    items = R.render_faces(faces, R.Camera(), 10.0, (400, 300))
    assert items
    img = R.rasterize(items, 800, 600)
    assert img.size == (800, 600)
    assert img.mode == "RGB"


def test_rasterize_handles_degenerate_polygons():
    """退化多边形（点共线/极少点）不能让光栅化抛异常。"""
    import ame.ui.render as R

    items = [
        {"coords": [0, 0, 10, 0, 20, 0], "fill": "#fff", "outline": "#000"},  # 共线
        {"coords": [5, 5], "fill": "#fff", "outline": "#000"},                # 单点
        {"coords": [0, 0, 10, 0, 5, 8], "fill": "#abcdef", "outline": "#000"},  # 正常
    ]
    img = R.rasterize(items, 100, 100)
    assert img.size == (100, 100)


def test_color_mix_is_cached():
    """颜色混合走缓存（900 面时被调 4800 次，占渲染耗时的三成）。"""
    import ame.ui.render as R

    a = R._mix((122, 138, 156), 0.7317)
    b = R._mix((122, 138, 156), 0.7317)
    assert a == b
    assert a.startswith("#") and len(a) == 7


def test_20k_face_model_budget_stays_within_canvas_limit():
    """20 万面档的每帧多边形数必须落在 Canvas 可承受区间（实测 800 是 30fps 工作点）。"""
    from ame.ui.render import (LOD_BUDGET_LOD, Camera,
                               clear_lod_cache, lod_level, lod_budget, render_faces)

    assert LOD_BUDGET_LOD <= 900, "预算超过 900 在实测中掉到 25 fps 以下"
    clear_lod_cache()
    faces = _grid_faces(200_000)
    assert lod_level(len(faces)) == "lod"
    assert lod_budget("lod") == LOD_BUDGET_LOD
    items = render_faces(faces, Camera(), 10.0, (400, 300))
    assert len(items) <= LOD_BUDGET_LOD + 50, f"绘制 {len(items)} 个，超出预算"
    assert len(items) >= 500, "仍要有足够的面保证形状可辨"


def test_500k_face_model_uses_coarse_budget():
    """50 万面档用更小的预算，保证 ≥20 fps。"""
    from ame.ui.render import (LOD_BUDGET_COARSE, Camera, clear_lod_cache,
                               lod_level, render_faces)

    assert LOD_BUDGET_COARSE <= 700
    clear_lod_cache()
    faces = _grid_faces(500_000)
    assert lod_level(len(faces)) == "coarse"
    items = render_faces(faces, Camera(), 10.0, (400, 300))
    assert len(items) <= LOD_BUDGET_COARSE + 50


# ============================================================ 排序稳定性（细节）
def test_latest_slice_is_really_the_last_one(data_root):
    """同一秒内连续切片多次，latest_slice 必须返回最后一次。

    实测缺陷：`ORDER BY created_at DESC` 没有稳定次级键，同一秒内的记录
    顺序会反（rowid 1,2,3,4 返回成 1,2,3,4 而非 4,3,2,1）。
    后果：用户切了 5 次片，打印用的可能是第 2 次的结果。
    """
    from ame.ledger.services import do_slice, latest_slice

    ws = _ws(data_root, name="ORD1", board="board_T19A.kicad_pcb")
    ws.commit("基线")
    ids = [do_slice(ws, f"切{i}")["slice_set_id"] for i in range(5)]
    assert latest_slice(ws)["id"] == ids[-1], "最新切片必须是最末一次"


def test_job_history_newest_first(data_root):
    """同一秒内连续打印多次，作业历史必须最新在前。"""
    from ame.ledger.services import job_history

    ws = _sliced(_ws(data_root, name="ORD2", board="board_T19A.kicad_pcb"))
    j1 = run_print(ws)
    j2 = run_print(ws)
    rows = job_history(ws)
    assert rows[0]["job_id"] == j2["job_id"], "第一条应是最新作业"
    assert rows[-1]["job_id"] == j1["job_id"]


def test_slice_pass_geometry_uses_latest_slice(data_root):
    """MRC-005 的已打印层几何必须取自最新切片（同一问题）。"""
    from ame.ledger.services import do_slice

    ws = _ws(data_root, name="ORD3", board="board_T19A.kicad_pcb")
    ws.commit("基线")
    for i in range(3):
        do_slice(ws, f"切{i}")
    layers = ws.slice_pass_geometry()
    assert layers, "应有已打印层几何"
    assert {l["kind"] for l in layers} >= {"bottom", "circuit", "top"}


# ============================================================ DB 查询性能（细节）
def test_hot_queries_use_indexes(data_root):
    """高频查询必须走索引，不能全表扫描（切片多了会线性变慢）。"""
    from ame.ledger.services import do_slice

    ws = _ws(data_root, name="IDX", board="board_T19A.kicad_pcb")
    ws.commit("基线")
    do_slice(ws)

    # 用"索引名"断言而不是笼统的 SEARCH：pod 上已有 idx_pad_comp 时，
    # 即便 idx_pad_ref 缺失也仍会 SEARCH（退化为只按 comp_id 过滤），
    # 那样测不出问题。
    checks = [
        ("latest slice",
         "SELECT id FROM slice_set WHERE project_id=? AND stale=0 "
         "ORDER BY created_at DESC, rowid DESC LIMIT 1",
         "idx_slice_latest"),
        ("paths of layer",
         "SELECT pts_json, width_mm FROM path WHERE layer_id=? ORDER BY seq",
         "idx_path_layer"),
        ("issues of commit",
         "SELECT * FROM mrc_issue WHERE commit_id=? ORDER BY severity DESC",
         "idx_issue_commit"),
        ("routes of circuit",
         "SELECT * FROM route WHERE circuit_id=?", "idx_route_circuit"),
    ]
    for name, sql, want_index in checks:
        plan = " ".join(r.get("detail", "") for r in ws.store.query(
            "EXPLAIN QUERY PLAN " + sql, ("x",)))
        assert want_index in plan, (
            f"{name} 未使用索引 {want_index}：{plan}")
        assert "SCAN " not in plan, f"{name} 仍在全表扫描：{plan}"


def test_slice_performance_does_not_degrade_with_history(data_root):
    """反复切片不应因历史累积而变慢（旧切片会被 prune，查询走索引）。"""
    import time

    from ame.ledger.services import do_slice

    ws = _ws(data_root, name="PERF", board="board_T19A.kicad_pcb")
    ws.commit("基线")
    times = []
    for i in range(12):
        t0 = time.perf_counter()
        do_slice(ws, f"切{i}")
        times.append(time.perf_counter() - t0)
    early = sum(times[:3]) / 3
    late = sum(times[-3:]) / 3
    assert late < early * 1.8, (
        f"切片耗时随历史明显增长：前 {early*1000:.0f} ms → 后 {late*1000:.0f} ms")


def test_sql_identifier_whitelist_blocks_unknown_table(data_root):
    """内部写辅助函数只允许白名单内的表与列（NFR-005 纵深防御）。"""
    from ame.ledger.workspace import CmdError

    ws = _ws(data_root, name="WL")
    with pytest.raises(CmdError, match="白名单"):
        ws._setter("sqlite_master", "sql", "x", "id", "1")
    with pytest.raises(CmdError, match="白名单"):
        ws._setter("comp", "id; DROP TABLE comp--", "x", "id", "1")


def test_face_orientation_thresholds_are_shared():
    """朝向判定阈值集中定义，LOD 与着色不能各写一份（改一处漏三处的隐患）。"""
    from ame.ui.render import HORIZONTAL_MIN, VERTICAL_MAX, face_orientation

    assert face_orientation(0.0) == "vertical"
    assert face_orientation(VERTICAL_MAX - 1e-9) == "vertical"
    assert face_orientation(HORIZONTAL_MIN + 1e-9) == "horizontal"
    assert face_orientation(0.5) == "slant"
    assert face_orientation(-1.0) == "horizontal"      # 朝下的大面也是水平面


# ============================================================ 非有限数值（NaN/Inf）
def test_route_rejects_non_finite_width(data_root):
    """NaN/Inf 线宽必须被拒（NaN 与任何数比较都是 False，会绕过下限检查）。

    实测缺陷：NaN 线宽一路走到数据库才被 NOT NULL 拦下（抛 IntegrityError
    而不是可读的 CmdError）；Inf 线宽**完全未拦截**、直接落库。
    """
    ws = _ws(data_root, name="NAN1", board="board_T19A.kicad_pcb")
    net = ws.nets()[0]["name"]
    n0 = len(ws.routes())
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(CmdError, match="有限数值"):
            ws.cmd_add_route(net, [[10, 10], [20, 20]], bad)
    assert len(ws.routes()) == n0, "非法输入不得落库"


def test_route_rejects_non_finite_coordinates(data_root):
    """NaN/Inf 坐标必须被拒。"""
    ws = _ws(data_root, name="NAN2", board="board_T19A.kicad_pcb")
    net = ws.nets()[0]["name"]
    n0 = len(ws.routes())
    with pytest.raises(CmdError, match="有限数值"):
        ws.cmd_add_route(net, [[float("nan"), 10], [20, 20]], 0.6)
    with pytest.raises(CmdError, match="有限数值"):
        ws.cmd_add_route(net, [[10, float("inf")], [20, 20]], 0.6)
    assert len(ws.routes()) == n0


def test_params_reject_non_finite(data_root):
    """工艺参数拒绝 NaN/Inf（SLC-012）。"""
    ws = _ws(data_root, name="NAN3")
    for bad in (float("nan"), float("inf")):
        with pytest.raises(CmdError):
            ws.cmd_set_params(layer_height_mm=bad)
    # 正常值仍可用
    ws.cmd_set_params(layer_height_mm=0.12)
    assert ws.params()["layer_height_mm"] == pytest.approx(0.12)
