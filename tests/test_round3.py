"""第三轮优化验收：DAT-006 迁移、PRJ-007 工程管理、SLC-013 材料库、UI-006 快捷键。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ame.db.store import CURRENT_SCHEMA_VERSION, Store
from ame.ledger.services import do_slice, run_print
from ame.ledger.workspace import CmdError, Workspace, create_project

from conftest import build_project


# ================================================================ DAT-006 迁移
@pytest.fixture(scope="module")
def ui_app():
    """整模块共用一个隐藏 App。

    Tk 在同一进程内反复创建/销毁根窗口会让 Tcl 解释器进入不可用状态
    （表现为 "no display name" 或 "Tcl wasn't installed properly"）。
    早期这里是每个用例 `App(...)` 一次，在全量运行时随机失败并被 skip，
    看起来像"环境没显示"，实际是测试写法问题。
    """
    import tkinter as tk
    from ame.ui.app import App
    try:
        a = App(None, hidden=True)
    except tk.TclError as e:
        pytest.skip(f"无可用显示：{e}")
    yield a
    try:
        a.ws = None
        a.destroy()
    except Exception:
        pass


def _make_v1_project(data_dir: Path) -> Path:
    """构造一个 v1 旧结构工程库（无 pad.w_mm、route.strategy 无 imported）。"""
    db = data_dir / "projects"
    db.mkdir(parents=True, exist_ok=True)
    f = db / "old-project.db"
    conn = sqlite3.connect(str(f))
    conn.executescript("""
        CREATE TABLE project(id TEXT PRIMARY KEY, name TEXT, schema_version INTEGER,
          head_commit_id TEXT, unit TEXT, bed_x_mm REAL, bed_y_mm REAL, created_at TEXT);
        INSERT INTO project VALUES('pid-old','旧工程',1,NULL,'mm',200,200,'2026-10-01');
        CREATE TABLE pad(id TEXT PRIMARY KEY, comp_id TEXT, net_id TEXT, pad_no TEXT,
          x_mm REAL, y_mm REAL, shape TEXT);
        INSERT INTO pad VALUES('p1','c1','n1','1',10.0,20.0,'rect');
        CREATE TABLE route(id TEXT PRIMARY KEY, circuit_id TEXT, net_id TEXT,
          pts_json TEXT, width_mm REAL, surface TEXT, layer_index INTEGER, strategy TEXT);
        INSERT INTO route VALUES('r1','c1','n1','[[1,2,0]]',0.6,'top',0,'manual');
    """)
    conn.commit()
    conn.close()
    return f


def test_dat006_migration_upgrades_and_backs_up(tmp_path):
    """DAT-006：打开旧库自动逐级迁移到当前版本；迁移前自动备份；旧数据保留。"""
    f = _make_v1_project(tmp_path)
    st = Store(f)
    assert st.migrated, "旧库应触发迁移"
    assert any(s.startswith("backup:") for s in st.migrated)
    assert st.one("SELECT schema_version v FROM project")["v"] == CURRENT_SCHEMA_VERSION
    # 新列有默认值
    pad = st.one("SELECT * FROM pad WHERE id='p1'")
    assert pad["w_mm"] == 1.5 and pad["h_mm"] == 1.0
    # 旧 route 数据保留
    assert st.one("SELECT pts_json FROM route WHERE id='r1'")["pts_json"] == "[[1,2,0]]"
    # 新 CHECK 生效：imported 策略可入库
    st.execute("INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,surface,"
               "layer_index,strategy) VALUES ('r2','c1','n1','[[3,4,0]]',0.6,'top',0,"
               "'imported')")
    # 备份文件在 backups/ 下
    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1
    st.close()


def test_dat006_migration_is_idempotent(tmp_path):
    """DAT-006：迁移后重开不再触发迁移；备份不重复。"""
    f = _make_v1_project(tmp_path)
    st = Store(f)
    assert st.migrated
    st.close()
    st2 = Store(f)
    assert st2.migrated == []
    st2.close()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_dat006_new_projects_at_current_version(data_root):
    """DAT-006：新建工程直接是当前版本，打开不触发迁移。"""
    ws = build_project(data_root, name="新版")
    assert ws.store.migrated == []
    assert ws.project["schema_version"] == CURRENT_SCHEMA_VERSION


def test_dat006_migrated_project_fully_usable(tmp_path):
    """DAT-006：迁移后的工程能走完整链路（导入→布线→切片）。"""
    f = _make_v1_project(tmp_path)
    st = Store(f)
    st.close()
    # 通过 Workspace 恢复：先把 pad 归到 circuit（旧库 fixture 未建 circuit 表，
    # 迁移后的库补齐结构后可正常使用）
    st = Store(f)
    st.execute("INSERT INTO circuit(id,project_id,format,imported_at) "
               "VALUES ('c1','pid-old','kicad_pcb','2026-10-01')")
    st.execute("INSERT INTO comp(id,circuit_id,ref,x_mm,y_mm,height_mm) "
               "VALUES ('c1','c1','R1',10.0,20.0,1.0)")
    st.execute("INSERT INTO net(id,circuit_id,name,klass,pad_count) "
               "VALUES ('n1','c1','N1','signal',1)")
    st.close()
    st = Store(f)
    st.execute("INSERT OR REPLACE INTO board_outline(project_id, outline_json, source) "
               "VALUES ('pid-old', '[[0,0],[50,0],[50,50],[0,50]]', 'board_file')")
    st.close()
    ws = Workspace(Store(f), "pid-old")
    st_before = ws.mrc_state()
    assert len(st_before["pads"]) == 1 and len(st_before["routes"]) == 1
    o, note = ws.board_outline()
    assert o and "Edge.Cuts" in note          # 板框回退生效
    ws.cmd_add_route("N1", [[10.0, 20.0], [10.0, 40.0]], 0.6)
    ws.commit("迁移后继续编辑")
    assert len(ws.routes()) == 2


# ================================================================ PRJ-007 工程管理
def test_prj007_copy_is_independent(data_root):
    """PRJ-007：复制后副本数据完整（含切片），且与原工程互不影响。"""
    ws = build_project(data_root, name="原工程")
    ws.commit("导入")
    do_slice(ws)
    pid = ws.project_id
    ws.store.close()

    info = data_root.copy_project(pid, "副本工程")
    s1 = Store(data_root.data_dir / "projects" / f"{pid}.db")
    s2 = Store(data_root.data_dir / "projects" / f"{info['id']}.db")
    assert s2.one("SELECT name FROM project")["name"] == "副本工程"
    assert s2.one("SELECT COUNT(*) c FROM version")["c"] == \
        s1.one("SELECT COUNT(*) c FROM version")["c"]
    assert s2.one("SELECT COUNT(*) c FROM slice_set")["c"] == \
        s1.one("SELECT COUNT(*) c FROM slice_set")["c"]
    # 原工程修改不影响副本
    s1.execute("UPDATE project SET name='改名' WHERE id=?", (pid,))
    assert s2.one("SELECT name FROM project")["name"] == "副本工程"
    s1.close()
    s2.close()


def test_prj007_rename_keeps_history(data_root):
    """PRJ-007：重命名只改名称，版本历史完整保留。"""
    ws = build_project(data_root, name="旧名")
    ws.commit("导入")
    pid = ws.project_id
    n_hist = len(ws.history())
    ws.store.close()
    data_root.rename_project(pid, "  新名 ")
    st = Store(data_root.data_dir / "projects" / f"{pid}.db")
    assert st.one("SELECT name FROM project")["name"] == "新名"
    assert st.one("SELECT COUNT(*) c FROM version")["c"] == n_hist
    st.close()


def test_prj007_delete_goes_to_trash_and_restores(data_root):
    """PRJ-007：删除进回收站（可恢复），恢复后数据完整且分配新 id。"""
    ws = build_project(data_root, name="待删")
    ws.commit("导入")
    pid = ws.project_id
    n_routes = len(ws.routes())
    ws.store.close()

    r = data_root.delete_project(pid)
    assert Path(r["trashed_to"]).exists()
    assert not (data_root.data_dir / "projects" / f"{pid}.db").exists()
    trash = data_root.list_trash()
    assert len(trash) == 1

    new_pid = data_root.restore_project(Path(r["trashed_to"]).name)
    assert new_pid != pid                       # 恢复用新 id 避免冲突
    st = Store(data_root.data_dir / "projects" / f"{new_pid}.db")
    assert st.one("SELECT name FROM project")["name"] == "待删"
    assert st.one("SELECT COUNT(*) c FROM route")["c"] == n_routes
    st.close()


def test_prj007_delete_missing_project_raises(data_root):
    """PRJ-007：删除不存在的工程给出明确错误。"""
    with pytest.raises(FileNotFoundError):
        data_root.delete_project("no-such-id")


def test_prj007_copy_self_check_guard(data_root, monkeypatch):
    """PRJ-007：复制闭环自检——若副本损坏立即报错而不是返回假成功。"""
    from ame.db import store as store_mod
    ws = build_project(data_root, name="自检")
    ws.commit("导入")
    ws.store.close()

    def bad_backup_to(self, dst_path):
        # 模拟备份产物损坏：只写了半个库
        open(dst_path, "wb").close()
    monkeypatch.setattr(store_mod.Store, "_backup_to", bad_backup_to)
    with pytest.raises(RuntimeError) as ei:
        data_root.copy_project(ws.project_id, "坏副本")
    assert "自检" in str(ei.value)


# ================================================================ SLC-013 材料库
def test_slc013_add_material_and_use(data_root):
    """SLC-013：新增材料后可用于切片选择列表。"""
    ws = build_project(data_root, name="材料")
    before = {m["id"] for m in ws.materials(enabled_only=True)}
    r = ws.cmd_add_material("低温银浆 C", "conductor",
                            {"viscosity_mPa_s": 9000}, "AG-2026-11", 55.0)
    after = {m["id"] for m in ws.materials(enabled_only=True)}
    assert r["id"] in after and r["id"] not in before
    ws.undo()
    assert r["id"] not in {m["id"] for m in ws.materials(enabled_only=True)}
    ws.redo()
    assert r["id"] in {m["id"] for m in ws.materials(enabled_only=True)}


def test_slc013_disable_used_material_rejected(data_root):
    """SLC-013：停用正被工艺参数引用的材料被拒绝（避免切片材料缺失）。"""
    ws = build_project(data_root, name="停用")
    p = ws.params()
    with pytest.raises(CmdError) as ei:
        ws.cmd_set_material_enabled(p["material_a_id"], False)
    assert "正被" in str(ei.value)
    # 未被引用的材料可以停用
    r = ws.cmd_add_material("备用介电", "dielectric")
    ws.cmd_set_material_enabled(r["id"], False)
    assert all(m["id"] != r["id"] for m in ws.materials(enabled_only=True))
    ws.undo()
    assert any(m["id"] == r["id"] for m in ws.materials(enabled_only=True))


def test_slc013_disabled_material_blocks_slice(data_root):
    """SLC-013：切换到停用材料后切片被阻断并说明原因。"""
    ws = build_project(data_root, name="阻断")
    ws.commit("导入")
    spare_a = ws.cmd_add_material("测试银浆", "conductor")
    ws.cmd_set_material_enabled(spare_a["id"], False)      # 未被引用，可停用
    ws.cmd_set_params(material_a_id=spare_a["id"])         # 把停用材料设为当前
    with pytest.raises(RuntimeError) as ei:
        do_slice(ws)
    assert "材料" in str(ei.value)


def test_slc013_invalid_role_rejected(data_root):
    """SLC-013：非法角色/空名称被拒绝。"""
    ws = build_project(data_root, name="校验")
    with pytest.raises(CmdError):
        ws.cmd_add_material("坏材料", "unknown")
    with pytest.raises(CmdError):
        ws.cmd_add_material("   ", "conductor")


# ================================================================ UI-006 快捷键行为
def test_ui006_delete_shortcut_deletes_selected_route(data_root, ui_app):
    """UI-006：Delete 删除选中走线（走命令总线、可撤销）。"""
    ws = build_project(data_root, name="快捷键")
    n0 = len(ws.routes())
    rid = ws.routes()[0]["id"]
    # 模拟选中走线后按 Delete（直接调用绑定的事件处理）
    a = ui_app
    a.data_root = data_root
    a.ws = ws
    a.selected = {"type": "route", "data": {"id": rid}}
    a.delete_selected_route()
    assert len(ws.routes()) == n0 - 1
    assert a.lbl_status.cget("text")  # 状态栏已更新
    ws.undo()
    assert len(ws.routes()) == n0


def test_ui006_project_menu_methods_safe_without_workspace(data_root, ui_app):
    """UI-007：未打开工程时调用工程管理方法有引导而不崩溃。"""
    a = ui_app
    a.data_root = data_root
    a.ws = None
    a.hidden = True          # 隐藏模式：不弹对话框
    a.rename_project()       # 无工程 → _require_ws 引导，不异常
    a.copy_project()
    a.delete_project()
    assert a.ws is None


# ================================================================ NFR-002 畸形数据
def test_nfr002_degenerate_route_does_not_crash_chain(data_root):
    """NFR-002：单点畸形走线——MRC/切片/打印不崩溃，切片给出跳过警告。"""
    ws = build_project(data_root, name="畸形")
    cid = ws.store.one("SELECT id FROM circuit LIMIT 1")["id"]
    nid = ws.store.one("SELECT id FROM net WHERE name='VCC'")["id"]
    ws.store.execute("INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,surface,"
                     "layer_index,strategy) VALUES ('bad1',?,?,'[[50,20,0]]',0.6,'top',0,"
                     "'imported')", (cid, nid))
    m = ws.run_mrc()                                   # MRC 不崩
    assert m["score"] >= 0
    sl = do_slice(ws)                                  # 切片不崩
    assert sl["passes"] == 7
    assert any("畸形走线" in w for w in sl["warnings"])
    job = run_print(ws)                                # 打印不崩
    assert job["state"] == "done"


# ================================================================ SLC-012 参数校验
@pytest.mark.parametrize("param,value,expect", [
    ("layer_height_mm", "abc", "必须是数字"),
    ("layer_height_mm", float("nan"), "NaN"),
    ("layer_height_mm", float("inf"), "NaN 或无穷大"),
    ("speed_mm_s", -5, "越界"),
    ("speed_mm_s", 0, "越界"),
    ("flow_factor", 0, "越界"),
    ("flow_factor", -1, "越界"),
    ("cure_min", 0, "越界"),
    ("ramp_c_min", 0, "越界"),
    ("final_cure_min", 0, "越界"),
    ("line_width_comp", 100, "越界"),
    ("silver_two_fills", 5, "0 或 1"),
    ("cure_mode", "bogus", "只能是"),
    ("material_a_id", "不存在的材料", "不存在"),
])
def test_slc012_param_validation_rejects_bad_values(data_root, param, value, expect):
    """SLC-012：工艺参数的类型/范围/枚举/外键全部校验，非法值给出可读原因。"""
    ws = build_project(data_root, name="参数")
    with pytest.raises(CmdError) as ei:
        ws.cmd_set_params(**{param: value})
    assert expect in str(ei.value), str(ei.value)
    # 失败不改变状态（CIR-012）
    assert ws.params()[param] != value or str(ws.params()[param]) != str(value)


def test_slc012_param_valid_values_applied(data_root):
    """SLC-012：合法参数正常写入并影响切片。"""
    ws = build_project(data_root, name="合法参数")
    r = ws.cmd_set_params(speed_mm_s=30, flow_factor=1.2, cure_min=8,
                          silver_two_fills=1, cure_mode="pass")
    assert set(r["changed"]) == {"speed_mm_s", "flow_factor", "cure_min",
                                 "silver_two_fills", "cure_mode"}
    sl = do_slice(ws)
    assert sl["passes"] == 8          # 两次填 → 8 道次


def test_slc012_dirty_params_blocked_at_slice(data_root):
    """SLC-012：绕过命令总线写入的脏参数，切片时被第二道防线拦截。"""
    ws = build_project(data_root, name="脏参数")
    ws.commit("导入")
    ws.store.execute("UPDATE process_params SET speed_mm_s=-9")
    with pytest.raises(RuntimeError) as ei:
        do_slice(ws)
    assert "越界" in str(ei.value) and "打印速度" in str(ei.value)


def test_slc012_no_zero_division_with_degenerate_speed(data_root):
    """SLC-012/NFR-002：速度为极小值时不抛 ZeroDivisionError。"""
    ws = build_project(data_root, name="除零")
    ws.commit("导入")
    ws.store.execute("UPDATE process_params SET ramp_c_min=0")
    # 切片层已拦截；即便绕过，估算也不应除零崩溃
    from ame.slicing.slicer import build_schedule
    params = {k: ws.params()[k] for k in ws.params() if k != "id"}
    params["ramp_c_min"] = 0
    sched = build_schedule(params, {"passes": [
        {"pass_no": 0, "kind": "bottom", "mat": "B", "z_mm": 0.08,
         "h_mm": 0.08, "paths": []}]})
    assert sched and all(x["duration_s"] >= 0 for x in sched)


# ================================================================ MRC-011 阈值校验
@pytest.mark.parametrize("code,threshold,expect", [
    ("R1", {"min_width": "abc"}, "必须是数字"),
    ("R1", {"min_width": -1}, "越界"),
    ("R1", {"min_width": float("nan")}, "NaN"),
    ("R3", {"max_slope_deg": 999}, "越界"),
    ("R1", {"nonexistent": 1}, "没有阈值字段"),
    ("R1", ["not", "dict"], "必须是键值表"),
])
def test_mrc011_threshold_validation(data_root, code, threshold, expect):
    """MRC-011：规则阈值同样做类型/范围/字段名校验。"""
    ws = build_project(data_root, name="阈值")
    with pytest.raises(CmdError) as ei:
        ws.cmd_set_rule(code, threshold)
    assert expect in str(ei.value), str(ei.value)


def test_mrc011_dirty_threshold_does_not_crash_engine(data_root):
    """NFR-002：数据库阈值被写坏（非 JSON/类型错）时 MRC 回退默认值不崩溃。"""
    ws = build_project(data_root, name="脏阈值")
    base = ws.run_mrc()["score"]
    for dirty in ('not json', '{"min_width":"bad"}', 'null', '[]', '{}'):
        ws.store.execute("UPDATE mrc_rule SET threshold_json=? WHERE code='R1'", (dirty,))
        m = ws.run_mrc()
        assert m["score"] >= 0
    ws.store.execute("UPDATE mrc_rule SET threshold_json="
                     "'{\"min_width\":0.5}' WHERE code='R1'")
    assert ws.run_mrc()["score"] == base


# ================================================================ PRJ-003 持久化
def test_prj003_survives_hard_kill(tmp_path):
    """PRJ-003：进程被强制结束（不 close）后，另一进程能完整读回数据。"""
    import subprocess
    import sys
    from ame.db.store import DataRoot
    from ame.ledger.workspace import Workspace

    ws = create_project(DataRoot(tmp_path / "d"), "强杀")
    pid = ws.project_id
    data_dir = str(tmp_path / "d")
    ws.store.close()
    repo = Path(__file__).resolve().parent.parent.as_posix()
    script = tmp_path / "w.py"
    script.write_text(f'''
import sys, os
sys.path.insert(0, r"{repo}")
from ame.db.store import DataRoot
from ame.ledger.workspace import Workspace
ws = Workspace(DataRoot(r"{data_dir}").open_project("{pid}"), "{pid}")
ws.cmd_import_step(open(r"{repo}/samples/data/plate_100x60.step", encoding="utf-8").read(), "p")
ws.cmd_import_board(open(r"{repo}/samples/data/board_T19A.kicad_pcb", encoding="utf-8").read())
ws.commit("before kill")
os._exit(0)
''', encoding="utf-8")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    ws2 = Workspace(DataRoot(data_dir).open_project(pid), pid)
    assert len(ws2.routes()) == 8 and len(ws2.pads()) == 20
    assert len(ws2.history()) >= 2
    ws2.store.close()


def test_prj003_batch_rollback_is_atomic(data_root):
    """NFR-002：批量写入失败时整批回滚，不留半套数据。"""
    ws = build_project(data_root, name="原子性")
    ws.commit("导入")
    head = ws.project["head_commit_id"]

    class Boom(Exception):
        pass

    def bad_batch():
        with ws.store.batch():
            ws.store.execute(
                "INSERT INTO slice_set(id,project_id,source_commit_id,params_id,"
                "layer_count,created_at) VALUES ('half',?,?,'PP-DEFAULT',3,'now')",
                (ws.project_id, head))
            raise Boom()

    with pytest.raises(Boom):
        bad_batch()
    assert ws.store.one("SELECT id FROM slice_set WHERE id='half'") is None


# ================================================================ STR-007 板框校验
@pytest.mark.parametrize("pts,expect", [
    ([(0, 0), (10, 0)], "至少需要 3"),
    ([(0, 0), (10, 0), (0, 0)], "至少需要 3"),
    ([(0, 0), (10, 0), (10, 0), (0, 0)], "退化"),
    ([(0, 0), (float("nan"), 0), (10, 10), (0, 10)], "NaN"),
    ([(0, 0), (float("inf"), 0), (10, 10), (0, 10)], "NaN/无穷大"),
    ([(0, 0), (1e6, 0), (1e6, 1e6), (0, 1e6)], "合理量程"),
    ([(0, 0), (0.5, 0), (0.5, 0.5), (0, 0.5)], "面积过小"),
    ("not a list", "坐标点列表"),
])
def test_str007_outline_validation_rejects_invalid(data_root, pts, expect):
    """STR-007：板框顶点数/退化/NaN/量程/面积全部校验，非法输入给出可读原因。"""
    ws = build_project(data_root, name="板框校验")
    with pytest.raises(CmdError) as ei:
        ws.cmd_set_outline(pts)
    assert expect in str(ei.value), str(ei.value)


def test_str007_outline_accepts_valid_and_warns_on_mismatch(data_root):
    """STR-007：合法板框被接受并归一化（去闭合尾点）；与模型不重叠时给警告而非拒绝。"""
    ws = build_project(data_root, name="板框合法")
    r = ws.cmd_set_outline([(0, 0), (100, 0), (100, 60), (0, 60), (0, 0)])
    assert r["outline"] == [[0.0, 0.0], [100.0, 0.0], [100.0, 60.0], [0.0, 60.0]]
    assert not r["warning"]
    # 远离模型的板框：接受但带警告（允许预留工艺边框）
    r2 = ws.cmd_set_outline([(500, 500), (600, 500), (600, 600), (500, 600)])
    assert r2["outline"] and "不重叠" in r2["warning"]


# ================================================================ 数据完整性
def test_data_integrity_no_orphans_after_full_chain(data_root):
    """DAT-001/004：完整链路（导入→切片→打印）后无孤儿记录、引用计数一致。"""
    ws = build_project(data_root, name="完整性")
    ws.commit("导入")
    do_slice(ws)
    run_print(ws, fault_plan=[{"type": "clog", "pass_no": 4, "at_len_mm": 30}])
    orphan_sql = [
        "SELECT COUNT(*) c FROM route r LEFT JOIN net n ON r.net_id=n.id WHERE n.id IS NULL",
        "SELECT COUNT(*) c FROM pad p LEFT JOIN comp x ON p.comp_id=x.id WHERE x.id IS NULL",
        "SELECT COUNT(*) c FROM path p LEFT JOIN layer l ON p.layer_id=l.id WHERE l.id IS NULL",
        "SELECT COUNT(*) c FROM layer l LEFT JOIN slice_set s ON l.slice_set_id=s.id "
        "WHERE s.id IS NULL",
        "SELECT COUNT(*) c FROM defect d LEFT JOIN print_job j ON d.job_id=j.id "
        "WHERE j.id IS NULL",
        "SELECT COUNT(*) c FROM version v LEFT JOIN version p ON v.parent_id=p.id "
        "WHERE v.parent_id IS NOT NULL AND p.id IS NULL",
        "SELECT COUNT(*) c FROM jumper j LEFT JOIN pad p ON j.from_pad_id=p.id "
        "WHERE p.id IS NULL",
    ]
    for sql in orphan_sql:
        assert ws.store.one(sql)["c"] == 0, sql
    # 每张表的 id 唯一
    for tbl in ("comp", "pad", "net", "route", "layer", "path", "version", "slice_set"):
        assert ws.store.one(f"SELECT COUNT(*) c FROM {tbl}")["c"] == \
            ws.store.one(f"SELECT COUNT(DISTINCT id) c FROM {tbl}")["c"], tbl
    # 网络 pad_count 与实际焊盘数一致
    bad = ws.store.query(
        "SELECT n.id FROM net n WHERE n.pad_count <> (SELECT COUNT(*) FROM pad p "
        "WHERE p.net_id=n.id)")
    assert bad == []


# ================================================================ MRC-004/005 凸台识别
def test_mrc004_005_boss_detected_as_obstacle(data_root):
    """MRC-004/005：带凸台的 STEP 应识别为遮挡障碍物（原先恒为空，R5 永不命中）。"""
    ws = build_project(data_root, name="凸台", step="plate_boss.step", board=None)
    st = ws.mrc_state()
    assert st["obstacles"], "plate_boss 有明显的 12×12×3 凸台，必须被识别"
    ob = st["obstacles"][0]
    assert ob["height"] > 2.0
    assert (ob["x1"] - ob["x0"]) > 5 and (ob["y1"] - ob["y0"]) > 5


def test_mrc005_route_through_boss_triggers_r5(data_root):
    """MRC-005/T11：走线穿过结构凸台 → R5 命中并指出遮挡物名称。"""
    ws = build_project(data_root, name="遮挡", step="plate_boss.step",
                       board="board_10.kicad_pcb")
    cid = ws.store.one("SELECT id FROM circuit LIMIT 1")["id"]
    nid = ws.store.one("SELECT id FROM net WHERE name='N1'")["id"]
    ws.store.execute(
        "INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,surface,"
        "layer_index,strategy) VALUES ('obs1',?,?,'[[20,31],[70,31]]',0.6,'top',0,"
        "'manual')", (cid, nid))
    ws.invalidate()
    m = ws.run_mrc()
    r5 = [i for i in m["issues"] if i["rule_code"] == "R5"]
    assert r5, "穿过凸台的走线必须命中 R5"
    assert "凸台" in r5[0]["reason"] and "遮挡" in r5[0]["reason"]


def test_flat_plate_has_no_false_obstacles(data_root):
    """MRC-004/005：平整板不能误报凸台（否则所有走线都被判遮挡）。"""
    ws = build_project(data_root, name="平板", step="plate_100x60.step")
    assert ws.mrc_state()["obstacles"] == []


# ================================================================ 缓存一致性（外部写入）
def test_cache_invalidates_on_external_write(data_root):
    """性能缓存：绕过命令总线直接改库后，视图必须自动失效（否则界面显示旧数据）。"""
    ws = build_project(data_root, name="缓存外部写")
    s1 = ws.run_mrc()["score"]
    cid = ws.store.one("SELECT id FROM circuit LIMIT 1")["id"]
    nid = ws.store.one("SELECT id FROM net WHERE name='VCC'")["id"]
    ws.store.execute(
        "INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,surface,"
        "layer_index,strategy) VALUES('ext1',?,?,'[[20,10],[20,48]]',0.6,'top',0,"
        "'manual')", (cid, nid))
    s2 = ws.run_mrc()["score"]
    assert s2 != s1, "外部写入后分数应变化（缓存已自动失效）"
    assert len([r for r in ws.mrc_state()["routes"] if r["id"] == "ext1"]) == 1


def test_cache_still_fast_after_guard(data_root):
    """性能：加了外部写入检测后缓存命中仍应远快于重建。"""
    import time
    ws = build_project(data_root, name="缓存速度")
    ws.mrc_state()
    t0 = time.perf_counter()
    for _ in range(200):
        ws.mrc_state()
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"200 次缓存命中耗时 {elapsed:.2f}s，过快退化"


# ================================================================ 七条规则有效性审计
def _mk_route(ws, rid, net, pts, width=0.6):
    cid = ws.store.one("SELECT id FROM circuit LIMIT 1")["id"]
    nid = ws.store.one("SELECT id FROM net WHERE name=?", (net,))["id"]
    ws.store.execute(
        "INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,surface,"
        "layer_index,strategy) VALUES(?,?,?,?,?,'top',0,'manual')",
        (rid, cid, nid, __import__("json").dumps(pts), width))
    ws.invalidate()


def _rule_hit(ws, code):
    return [i for i in ws.run_mrc()["issues"] if i["rule_code"] == code]


def test_rule_r1_thin_line_fires(data_root):
    """MRC-001：细线必须命中 R1。"""
    ws = build_project(data_root, name="R1")
    _mk_route(ws, "t1", "N1", [[10, 10], [30, 10]], width=0.30)
    hits = _rule_hit(ws, "R1")
    assert hits and "线宽" in hits[0]["reason"]


def test_rule_r2_near_nets_fires(data_root):
    """MRC-002：异网走线过近必须命中 R2。"""
    ws = build_project(data_root, name="R2")
    _mk_route(ws, "t2a", "N1", [[10, 45], [30, 45]])
    _mk_route(ws, "t2b", "N2", [[10, 45.9], [30, 45.9]])
    assert _rule_hit(ws, "R2"), "0.3 mm 异网间距应命中 R2"


def test_rule_r3_slope_fires(data_root):
    """MRC-003：60° 悬垂面必须命中 R3（T11）。"""
    ws = build_project(data_root, name="R3", step="overhang60.step", board=None)
    hits = _rule_hit(ws, "R3")
    assert hits and "坡度" in hits[0]["reason"]


def test_rule_r4_tip_clearance_fires(data_root):
    """MRC-004：走线距器件 0.5 mm（< 要求的最小间隙 0.82）必须命中 R4。"""
    ws = build_project(data_root, name="R4", board="board_10.kicad_pcb")
    # R1 器件在 (15,20)，本体 13.5–16.5 × 18.5–21.5；走线 y=22 距 0.5 mm
    _mk_route(ws, "t4", "N7", [[15, 22.0], [20, 22.0]])
    hits = _rule_hit(ws, "R4")
    assert hits and "最小间隙" in hits[0]["reason"]
    # 文案要说明阈值来源（针尖外径/2 + 间隙），否则用户无法判断该改什么
    assert "针尖外径" in hits[0]["reason"] and "间隙" in hits[0]["reason"]


def test_rule_r4_threshold_is_configurable(data_root):
    """MRC-011：改 R4 的 min_gap 必须改变结果（阈值不能是死配置）。

    早期实现从不读取 min_gap，把阈值从 0.5 改到 5.0 分数一点不变。
    """
    ws = build_project(data_root, name="R4T", board="board_10.kicad_pcb")
    # 距器件 3 mm：默认阈值（0.82）不命中
    _mk_route(ws, "t4t", "N7", [[15, 24.5], [20, 24.5]])
    assert not _rule_hit(ws, "R4"), "默认阈值下 3 mm 间距不应命中"
    ws.cmd_set_rule("R4", {"min_gap": 3.0})
    hits = _rule_hit(ws, "R4")
    assert hits, "min_gap 调到 3.0 后 3 mm 间距应命中（阈值必须生效）"


def test_rule_r5_occlusion_fires(data_root):
    """MRC-005：走线穿过器件必须命中 R5（T11）。"""
    ws = build_project(data_root, name="R5", board="board_10.kicad_pcb")
    _mk_route(ws, "t5", "N5", [[15, 20], [20, 20]])
    hits = _rule_hit(ws, "R5")
    assert hits and "遮挡" in hits[0]["reason"]


def test_rule_r6_oven_gradient_fires(data_root):
    """MRC-006：走线紧靠炉门侧（温差超限）必须命中 R6。"""
    ws = build_project(data_root, name="R6", board="board_10.kicad_pcb")
    _mk_route(ws, "t6", "N7", [[1, 30], [50, 30]])
    hits = _rule_hit(ws, "R6")
    assert hits and "温差" in hits[0]["reason"]


def test_rule_r7_dam_channel_fires(data_root):
    """MRC-007：异网走线间的介电坝通道不足必须命中 R7。"""
    ws = build_project(data_root, name="R7", board="board_10.kicad_pcb")
    _mk_route(ws, "t7a", "N9", [[10, 50], [30, 50]])
    _mk_route(ws, "t7b", "N11", [[10, 51.4], [30, 51.4]])
    hits = _rule_hit(ws, "R7")
    assert hits and "介电坝" in hits[0]["reason"]


def test_all_seven_rules_can_fire(data_root):
    """汇总：七条规则全部具备命中能力（防止某条规则因数据依赖而形同虚设）。"""
    checks = {
        "R1": ("t1", "N1", [[10, 10], [30, 10]], 0.30),
        "R4": ("t4", "N7", [[15, 22.0], [20, 22.0]], 0.6),
        "R5": ("t5", "N5", [[15, 20], [20, 20]], 0.6),
        "R6": ("t6", "N7", [[1, 30], [50, 30]], 0.6),
    }
    fired = set()
    for code, (rid, net, pts, w) in checks.items():
        ws = build_project(data_root, name=f"all-{code}", board="board_10.kicad_pcb")
        _mk_route(ws, rid, net, pts, w)
        if _rule_hit(ws, code):
            fired.add(code)
    # R2/R7 需要两条异网走线
    ws = build_project(data_root, name="all-R2R7", board="board_10.kicad_pcb")
    _mk_route(ws, "a", "N1", [[10, 45], [30, 45]])
    _mk_route(ws, "b", "N2", [[10, 45.9], [30, 45.9]])
    for code in ("R2", "R7"):
        if _rule_hit(ws, code):
            fired.add(code)
    # R3 需要斜面模型
    ws3 = build_project(data_root, name="all-R3", step="overhang60.step", board=None)
    if _rule_hit(ws3, "R3"):
        fired.add("R3")
    assert fired == {"R1", "R2", "R3", "R4", "R5", "R6", "R7"}, f"未能命中的规则：{ {'R1','R2','R3','R4','R5','R6','R7'} - fired}"


# ================================================================ AGT-008 重放健壮性
def test_agt008_replay_degrades_when_slice_blocked(data_root):
    """AGT-008：重放时若切片被阻断（连通性失败），应返回错误而不是抛异常给界面。"""
    from ame.agent.loop import clone_workspace, replay
    from samples.gen_samples import kicad_board, two_pin

    outline = [(5, 5), (110, 5), (110, 60), (5, 60)]
    comps = [two_pin(f"R{i}", 20 + (i % 4) * 25, 20 if i < 4 else 45, f"A{i}", f"A{i+1}")
             for i in range(8)]
    nets = sorted({c["pads"][0]["net"] for c in comps}
                  | {c["pads"][1]["net"] for c in comps})
    # 造成短路的走线：重切必被 SLC-007 阻断
    segs = [{"netno": nets.index("A0") + 1, "net": "A0",
             "pts": [[17.2, 20], [30, 20]], "width": 0.3},
            {"netno": nets.index("A1") + 1, "net": "A1",
             "pts": [[30, 20], [42.8, 20]], "width": 0.3}]
    ws = build_project(data_root, name="重放阻断", step="plate_100x60.step", board=None)
    ws.cmd_import_board(kicad_board(outline, comps,
                                    [(i + 1, n) for i, n in enumerate(nets)], segs))
    ws.commit("导入")
    snap = clone_workspace(ws, "rp")
    rp = replay(snap, [], ws, do_slice, source_commit=ws.project["head_commit_id"])
    assert rp["gcode_match"] is False
    assert "slice_error" in rp and "连通性" in rp["slice_error"]


# ================================================================ NFR-008 跨机可移植性
def test_nfr008_cross_machine_roundtrip(tmp_path):
    """NFR-008：工程包可跨机器导入，换机后可继续编辑。

    验收口径（需求 §6 NFR-008）：数据与安装目录分离；工程包可跨机器导入；
    判定「换机器后工程可继续编辑」。这里用两个独立 DataRoot 模拟两台机器。
    """
    from ame.db.store import DataRoot
    from ame.ledger.services import do_slice
    from ame.ledger.workspace import Workspace, create_project
    from conftest import SAMPLES

    # ---- 机器 A：建工程、导入、切片 ----
    root_a = DataRoot(tmp_path / "machineA" / "data")
    ws_a = create_project(root_a, "跨机工程")
    ws_a.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"), "plate.step")
    ws_a.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    ws_a.commit("机器A导入")
    sl_a = do_slice(ws_a)
    n_routes, n_pads, n_versions = len(ws_a.routes()), len(ws_a.pads()), len(ws_a.history())

    pkg = tmp_path / "transfer.amepkg"
    root_a.export_project(ws_a.store, pkg)
    assert pkg.exists() and pkg.stat().st_size > 0
    ws_a.store.close()

    # ---- 机器 B：完全不同的数据目录 ----
    root_b = DataRoot(tmp_path / "machineB" / "ame-data")
    assert root_b.data_dir != root_a.data_dir
    st_b = root_b.import_project(pkg)
    pid_b = st_b.one("SELECT id FROM project")["id"]
    ws_b = Workspace(st_b, pid_b)

    # 数据完整
    assert len(ws_b.routes()) == n_routes
    assert len(ws_b.pads()) == n_pads
    assert len(ws_b.history()) == n_versions
    # 大文件（STEP）经内容寻址一并迁移，可读回
    model = ws_b.step_model()
    assert model is not None and len(model.faces) > 0
    assert list((root_b.data_dir / "blobs").rglob("*")), "blob 未随包迁移"

    # ---- 关键判定：换机后可继续编辑 ----
    # 选一条加宽后不会与邻近焊盘重叠的走线（VCC 横线两侧空间充足）
    vcc = next(r for r in ws_b.mrc_state()["routes"] if r["net"] == "VCC")
    old_w = vcc["width"]
    ws_b.cmd_change_width(vcc["id"], 0.8)
    assert ws_b.store.one("SELECT width_mm FROM route WHERE id=?",
                          (vcc["id"],))["width_mm"] == 0.8 != old_w
    ws_b.commit("机器B继续编辑")
    assert len(ws_b.history()) == n_versions + 1
    # 继续编辑后仍能重切片出 G-code
    sl_b = do_slice(ws_b)
    assert sl_b["passes"] == 7 and sl_b["gcode_hash"]
    # 改了线宽，哈希应与机器A不同（证明确实按新设计重切）
    assert sl_b["gcode_hash"] != sl_a["gcode_hash"]
    ws_b.store.close()


def test_nfr008_data_dir_separate_from_install_dir(tmp_path, monkeypatch):
    """NFR-008/DAT-004：数据目录与代码（安装）目录分离，且可被 AME_DATA_DIR 改写。"""
    from pathlib import Path as _P

    import ame
    from ame.db.store import DataRoot

    code_dir = _P(ame.__file__).resolve().parent.parent   # 仓库/安装目录
    d = DataRoot(tmp_path / "sep" / "data")
    assert not str(d.data_dir).startswith(str(code_dir)), \
        f"数据目录 {d.data_dir} 不应位于代码目录 {code_dir} 下"
    # 工程库与 blob 都落在数据目录内，不写进代码目录
    from ame.ledger.workspace import create_project
    ws = create_project(d, "分离")
    ws.commit("v1")
    assert str(ws.store.db_path).startswith(str(d.data_dir))
    ws.store.close()

    # 环境变量可重定向（换机器/换盘符时不用改代码）
    monkeypatch.setenv("AME_DATA_DIR", str(tmp_path / "envdata"))
    assert _P(DataRoot().data_dir) == (tmp_path / "envdata").resolve()


def test_nfr008_import_as_copy_avoids_id_collision(tmp_path):
    """NFR-008/PRJ-006：同一数据目录重复导入同包时，可选择「导入为副本」而不是被硬拒。"""
    from ame.db.store import DataRoot
    from ame.ledger.workspace import Workspace, create_project
    from conftest import SAMPLES

    root = DataRoot(tmp_path / "d")
    ws = create_project(root, "原始")
    ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    ws.commit("v1")
    pid = ws.project_id
    pkg = tmp_path / "x.amepkg"
    root.export_project(ws.store, pkg)
    ws.store.close()

    # 默认模式：同 id 冲突被拒（防误覆盖）
    with pytest.raises(FileExistsError):
        root.import_project(pkg)
    # 副本模式：分配新 id + 副本名称，数据完整且可编辑
    st2 = root.import_project(pkg, as_copy=True)
    pid2 = st2.one("SELECT id FROM project")["id"]
    assert pid2 != pid
    assert "副本" in st2.one("SELECT name FROM project")["name"]
    assert st2.one("SELECT COUNT(*) c FROM route")["c"] == 8
    ws2 = Workspace(st2, pid2)
    ws2.cmd_add_route("VCC", [[30, 8], [60, 8]], 0.6)
    assert len(ws2.routes()) == 9
    # 互不影响
    st1 = root.open_project(pid)
    assert st1.one("SELECT COUNT(*) c FROM route")["c"] == 8
    st1.close()
    st2.close()


def test_nfr006_module_boundaries_and_audit(data_root):
    """NFR-006（可部分自动化）：模块边界清晰、关键路径有审计、测试可一键运行。"""
    from pathlib import Path as _P

    repo = _P(__file__).resolve().parent.parent
    # 1) 模块边界：四域 + 账本 + 存储各自独立成包
    for mod in ("geometry", "circuit", "mrc", "slicing", "printing", "agent",
                "ledger", "db"):
        assert (repo / "ame" / mod / "__init__.py").exists(), f"缺少模块 {mod}"
    # 2) 分层不反转：非 UI 层不得反向依赖 UI
    for py in (repo / "ame").rglob("*.py"):
        if "ui" in py.parts:
            continue
        src = py.read_text("utf-8")
        assert "from ..ui" not in src, f"{py} 反向依赖 UI 层（模块边界被破坏）"
    # 3) 关键路径有审计记录（AGT-011）
    from ame.agent.loop import run_designer
    ws = build_project(data_root, name="审计")
    res = run_designer(ws, auth="auto", max_rounds=3)
    assert res["audit"], "Agent 缺少审计记录"
    assert all("round" in a for a in res["audit"])
    # 4) 测试可一键运行
    bat = repo / "run_tests.bat"
    assert bat.exists() and "pytest" in bat.read_text("utf-8", errors="replace")


# ================================================================ CIR-008 顶面/底面走线
def test_cir008_top_and_bottom_surfaces_supported(data_root):
    """CIR-008：首版至少支持顶面/底面走线，且数据结构预留层号。

    验收要点原文：「切到另一面，走线仍可见且连通性正确」。
    """
    ws = build_project(data_root, name="双面", board="board_T19A.kicad_pcb")
    ws.cmd_add_route("VCC", [[22.8, 15], [77.2, 15]], 0.6, surface="bottom")
    st = ws.mrc_state()
    bottom = [r for r in st["routes"] if r["surface"] == "bottom"]
    assert bottom, "底面走线应能落库"
    assert "layer_index" in bottom[0], "数据结构应预留层号（CIR-008）"
    # 底面走线参与连通性检查
    from ame.circuit.connectivity import check_connectivity
    res = check_connectivity(["VCC"], [p for p in st["pads"] if p["net"] == "VCC"],
                             st["routes"], [])
    assert res["ok"], "底面走线应计入连通性"


def test_cir008_different_layers_do_not_short(data_root):
    """CIR-008：顶/底面属不同物理层，异网走线同路径不构成短路；同层才算。"""
    from ame.circuit.connectivity import check_connectivity

    # 顶 + 底 → 不短路
    ws = build_project(data_root, name="跨层", board="board_T19A.kicad_pcb")
    ws.cmd_add_route("N1", [[30, 20], [70, 20]], 0.6, surface="top")
    ws.cmd_add_route("N2", [[30, 20], [70, 20]], 0.6, surface="bottom")
    st = ws.mrc_state()
    pads = [p for p in st["pads"] if p["net"] in ("N1", "N2")]
    assert check_connectivity(["N1", "N2"], pads, st["routes"], [])["shorts"] == []

    # 顶 + 顶 → 短路
    ws2 = build_project(data_root, name="同层", board="board_T19A.kicad_pcb")
    ws2.cmd_add_route("N1", [[30, 20], [70, 20]], 0.6, surface="top")
    ws2.cmd_add_route("N2", [[30, 20], [70, 20]], 0.6, surface="top")
    st2 = ws2.mrc_state()
    pads2 = [p for p in st2["pads"] if p["net"] in ("N1", "N2")]
    assert len(check_connectivity(["N1", "N2"], pads2, st2["routes"], [])["shorts"]) == 1


# ================================================================ 几何基元统一（重构防退化）
def test_geometry_primitives_shared_across_layers():
    """重构：焊盘/走线几何在 connectivity / mrc / slicer 三层必须完全一致。

    历史上三处各写一份，只有 connectivity 支持旋转焊盘——切片与 MRC 会把
    旋转焊盘当轴对齐矩形算错。此测试锁定"唯一实现"这一约束。
    """
    from ame.circuit.connectivity import pad_polygon as conn_pad
    from ame.geometry.primitives import pad_polygon as base_pad
    from ame.mrc.engine import _pad_polygon as eng_pad
    from ame.slicing.slicer import _pad_poly as slc_pad

    cases = [
        {"x": 10, "y": 20, "w": 4.0, "h": 1.0, "shape": "rect", "rot": 0},
        {"x": 10, "y": 20, "w": 4.0, "h": 1.0, "shape": "rect", "rot": 90},
        {"x": 10, "y": 20, "w": 4.0, "h": 1.0, "shape": "rect", "rot": 45},
        {"x": 10, "y": 20, "w": 4.0, "h": 1.0, "shape": "circle", "rot": 0},
        {"x": 10, "y": 20, "w": 2.0, "h": 1.0, "shape": "oval", "rot": 30},
    ]
    for c in cases:
        areas = [base_pad(c).area, conn_pad(c).area, eng_pad(c).area, slc_pad(c).area]
        assert len({round(a, 6) for a in areas}) == 1, f"{c} 几何不一致: {areas}"


def test_rotated_pad_geometry_is_correct():
    """重构修正：rot=90 的 4×1 焊盘必须呈现为 1 宽 × 4 高（原实现忽略旋转）。"""
    from ame.geometry.primitives import pad_polygon

    poly = pad_polygon({"x": 10, "y": 20, "w": 4.0, "h": 1.0,
                        "shape": "rect", "rot": 90})
    minx, miny, maxx, maxy = poly.bounds
    assert abs((maxx - minx) - 1.0) < 0.01
    assert abs((maxy - miny) - 4.0) < 0.01
    assert abs(poly.area - 4.0) < 0.01


def test_pad_inflate_matches_extend_semantics():
    """切片留坑用 inflate：外扩后面积必须严格大于原面积，且圆形同样生效。"""
    from ame.geometry.primitives import pad_polygon

    for shape in ("rect", "circle"):
        p = {"x": 0, "y": 0, "w": 2.0, "h": 2.0, "shape": shape, "rot": 0}
        assert pad_polygon(p, inflate=0.3).area > pad_polygon(p).area


def test_ui_never_writes_database_directly():
    """AGT-009：界面不得绕过命令/服务层直接写库（人与 Agent 必须同源）。"""
    import re
    from pathlib import Path as _P
    ui = (_P(__file__).resolve().parent.parent / "ame" / "ui" / "app.py").read_text("utf-8")
    bad = re.findall(r"store\.execute\(\s*[\"'][^\"']*(?:INSERT|UPDATE|DELETE)", ui, re.I)
    assert not bad, f"界面存在直写库: {bad}"


# ================================================================ 数据保留策略（推演优化）
def test_slice_retention_prevents_unbounded_growth(data_root):
    """DAT-004：反复切片不应让工程库无限膨胀——只保留最近 N 份切片。"""
    from ame.ledger.services import SLICE_KEEP, do_slice, latest_slice

    ws = build_project(data_root, name="切片保留")
    ws.commit("导入")
    for _ in range(SLICE_KEEP + 7):
        do_slice(ws)
    n_slices = ws.store.one("SELECT COUNT(*) c FROM slice_set")["c"]
    assert n_slices <= SLICE_KEEP, f"切片未按上限清理: {n_slices} > {SLICE_KEEP}"
    # 最新切片仍可用
    assert latest_slice(ws) is not None
    # 保留的切片仍带完整路径
    n_path = ws.store.one("SELECT COUNT(*) c FROM path")["c"]
    assert n_path > 0
    # 版本历史不受影响（切片清理不允许删版本）
    assert len(ws.history()) >= 1


def test_job_retention_prevents_unbounded_growth(data_root):
    """PRT-010：作业历史保留上限，避免无限增长拖慢查询。"""
    from ame.ledger.services import JOB_KEEP, do_slice, job_history, run_print

    ws = build_project(data_root, name="作业保留")
    ws.commit("导入")
    do_slice(ws)
    for _ in range(JOB_KEEP + 8):
        run_print(ws)
    n_jobs = ws.store.one("SELECT COUNT(*) c FROM print_job")["c"]
    assert n_jobs <= JOB_KEEP, f"作业未按上限清理: {n_jobs} > {JOB_KEEP}"
    assert len(job_history(ws)) == n_jobs


def test_path_simplification_reduces_storage(data_root):
    """SLC-004：路径点做几何简化——不留 buffer 圆弧产生的大量近似共线点。"""
    from ame.slicing.slicer import SIMPLIFY_TOL, simplify_pts

    # 圆弧上密集采样的点到弦的偏差应被消除
    import math
    arc = [[round(10 + 5 * math.cos(t / 40 * math.pi), 4),
            round(10 + 5 * math.sin(t / 40 * math.pi), 4)] for t in range(41)]
    simple = simplify_pts(arc)
    # 0.02 mm 容差下圆弧仍保留一定点数（曲率限制），但必须有实质削减
    assert len(simple) < len(arc) * 0.8, f"简化不足: {len(simple)} / {len(arc)}"
    assert simple[0] == arc[0] and simple[-1] == arc[-1], "首尾点必须保留"
    assert SIMPLIFY_TOL <= 0.05
    # 两点线不应被改动
    assert simplify_pts([[0, 0], [10, 10]]) == [[0, 0], [10, 10]]


def test_simplified_paths_still_slice_and_connect(data_root):
    """SLC-004/SLC-007：路径简化后切片与连通性检查仍通过。"""
    from ame.ledger.services import do_slice

    ws = build_project(data_root, name="简化后切片")
    ws.commit("导入")
    sl = do_slice(ws)
    assert sl["passes"] == 7
    assert sl["gcode_hash"]


# ================================================================ SLC-001 总厚校验
def test_slc001_total_thickness_no_warning_after_decision(data_root):
    """SLC-001/SLC-012：v1.2 裁决后（§10.5），默认 0.44 mm 落在 0.40–0.45 内，不告警。"""
    from ame.ledger.services import do_slice

    ws = build_project(data_root, name="总厚")
    ws.commit("导入")
    sl = do_slice(ws)                       # 默认 3×0.08 + 0.10 + 0.10 = 0.44
    assert sl["total_thickness_mm"] == 0.44
    assert not [w for w in sl["warnings"] if "总厚" in w],         "0.44 在裁决后的目标区间 0.40–0.45 内，不应告警"


def test_slc001_total_thickness_warning_when_out_of_range(data_root):
    """SLC-001/SLC-012：总厚超出裁决区间时仍必须提示（带偏差与原因）。"""
    from ame.ledger.services import do_slice

    ws = build_project(data_root, name="总厚越界")
    ws.commit("导入")
    ws.cmd_set_params(layer_height_mm=0.20)      # 3×0.08+0.20+0.20 = 0.64
    sl = do_slice(ws)
    assert sl["total_thickness_mm"] > 0.45
    thick_warn = [w for w in sl["warnings"] if "总厚" in w]
    assert thick_warn, "超出 0.40–0.45 必须给出总厚提示"
    assert "0.640" in thick_warn[0] and "0.40" in thick_warn[0]


def test_slc001_no_warning_within_target(data_root):
    """SLC-001：总厚落在裁决后的目标区间 0.40–0.45 内时不应报警。"""
    from ame.ledger.services import do_slice

    ws = build_project(data_root, name="总厚合规")
    ws.commit("导入")
    ws.cmd_set_params(layer_height_mm=0.085)     # 3×0.08 + 0.085 + 0.085 = 0.41
    sl = do_slice(ws)
    assert 0.40 <= sl["total_thickness_mm"] <= 0.45
    assert not [w for w in sl["warnings"] if "总厚" in w]


def test_slc001_check_total_thickness_function():
    """SLC-001：总厚校验函数的边界行为（含浮点容差）。"""
    from ame.slicing.slicer import check_total_thickness

    # v1.2 裁决：目标区间 0.40–0.45（§10.5）
    assert check_total_thickness(0.40) == ""
    assert check_total_thickness(0.44) == ""      # 默认配置，裁决后合规
    assert check_total_thickness(0.45) == ""
    assert check_total_thickness(0.425) == ""
    assert check_total_thickness(0.35) != ""      # 旧目标区间下缘，现已越界
    assert check_total_thickness(0.36) != ""
    assert check_total_thickness(0.50) != ""
    assert "0.400" in check_total_thickness(0.35) or "0.40" in check_total_thickness(0.35)


# ================================================================ SLC-001 各层厚度可设定
def test_slc001_bottom_layer_configurable(data_root):
    """SLC-001「支持设定各层厚度」：下保护层厚与道次数必须可配置，且影响总厚。"""
    from ame.ledger.services import do_slice

    ws = build_project(data_root, name="层厚可配")
    ws.commit("导入")
    p = ws.params()
    assert p["bottom_layer_mm"] == 0.08          # 需求 §1.7 基线
    assert p["bottom_passes"] == 3               # 典型 3 道
    sl = do_slice(ws)
    assert sl["passes"] == 7
    assert sl["total_thickness_mm"] == 0.44

    # 改成 2 道 → 道次数与总厚同步变化（不是硬编码）
    ws.cmd_set_params(bottom_passes=2)
    sl2 = do_slice(ws)
    assert sl2["passes"] == 6, "下保护 2 道应为 6 道次总数"
    assert abs(sl2["total_thickness_mm"] - 0.36) < 0.01
    # 离开目标区间应提示
    assert [w for w in sl2["warnings"] if "总厚" in w]

    # 改单层厚 → 总厚变化
    ws.cmd_set_params(bottom_passes=3, bottom_layer_mm=0.10)
    assert abs(do_slice(ws)["total_thickness_mm"] - 0.50) < 0.01


def test_slc001_bottom_layer_validation(data_root):
    """SLC-012：下保护层厚/道次数的非法值必须被拒绝。"""
    ws = build_project(data_root, name="层厚校验")
    for k, v in (("bottom_passes", 0), ("bottom_passes", 20),
                 ("bottom_layer_mm", -1), ("bottom_layer_mm", 5.0)):
        with pytest.raises(CmdError) as ei:
            ws.cmd_set_params(**{k: v})
        assert "越界" in str(ei.value)


def test_migration_v4_adds_bottom_layer_columns(tmp_path):
    """DAT-006：v3 旧库升级到 v4 时补上 bottom_layer_mm / bottom_passes 列。"""
    import sqlite3
    from ame.db.store import CURRENT_SCHEMA_VERSION, Store

    db = tmp_path / "projects"
    db.mkdir(parents=True)
    f = db / "v3.db"
    conn = sqlite3.connect(str(f))
    conn.executescript("""
        CREATE TABLE project(id TEXT PRIMARY KEY, name TEXT, schema_version INTEGER,
          head_commit_id TEXT, unit TEXT, bed_x_mm REAL, bed_y_mm REAL, created_at TEXT);
        INSERT INTO project VALUES('p3','v3工程',3,NULL,'mm',200,200,'2026-10-01');
        CREATE TABLE process_params(id TEXT PRIMARY KEY, layer_height_mm REAL NOT NULL);
        INSERT INTO process_params VALUES('PP-DEFAULT', 0.10);
    """)
    conn.commit()
    conn.close()
    st = Store(f)
    assert CURRENT_SCHEMA_VERSION >= 4
    cols = {r["name"] for r in st.query("PRAGMA table_info(process_params)")}
    assert "bottom_layer_mm" in cols and "bottom_passes" in cols
    row = st.one("SELECT bottom_layer_mm, bottom_passes FROM process_params")
    assert row["bottom_layer_mm"] == 0.08 and row["bottom_passes"] == 3
    st.close()
    # 幂等
    st2 = Store(f)
    assert st2.migrated == []
    st2.close()


# ================================================================ STR-007 板框并集 / CIR-011 闭合环
def test_str007_split_top_face_uses_union():
    """STR-007：顶面被拆成多片共面矩形时必须取并集。

    原实现取"最大单面"，会把 100×60 的板识别成 50×60 且**不报错**——
    静默少算一半，切片只覆盖半块板。
    """
    from ame.geometry.analysis import find_board_outline
    from ame.geometry.stepio import Face, StepModel, polygon_area_3d

    m = StepModel()

    def add(fid, loop, n):
        xs = [p[0] for p in loop]
        ys = [p[1] for p in loop]
        zs = [p[2] for p in loop]
        m.faces.append(Face(fid, "plane", n, polygon_area_3d(loop, n),
                            (sum(xs) / 4, sum(ys) / 4, sum(zs) / 4), loop))

    add("F1", [(0, 0, 0), (100, 0, 0), (100, 60, 0), (0, 60, 0)], (0, 0, -1))
    add("F2", [(0, 0, 2), (50, 0, 2), (50, 60, 2), (0, 60, 2)], (0, 0, 1))
    add("F3", [(50, 0, 2), (100, 0, 2), (100, 60, 2), (50, 60, 2)], (0, 0, 1))
    m.bbox = (0, 0, 0, 100, 60, 2)

    pts, note = find_board_outline(m)
    assert pts, f"应识别出板框: {note}"
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert abs((max(xs) - min(xs)) - 100.0) < 0.01, f"板宽应为 100，实际 {max(xs)-min(xs)}"
    assert abs((max(ys) - min(ys)) - 60.0) < 0.01
    assert "并集" in note


def test_str007_vertical_slab_rejected():
    """STR-007：竖直薄板（30×2×30）的竖立面不能当板框。"""
    from conftest import SAMPLES

    from ame.geometry.analysis import find_board_outline
    from ame.geometry.stepio import read_step

    m = read_step((SAMPLES / "no_outline.step").read_text("utf-8"))
    pts, note = find_board_outline(m)
    assert pts is None, f"竖直薄板不应识别为板框，实际得到 {pts}"
    assert "板框" in note


def test_cir011_closed_loop_route_allowed():
    """CIR-011：闭合环走线（天线线圈/地环）应允许，首尾相接不算自交。"""
    from ame.circuit.connectivity import validate_route

    outline = [(0, 0), (100, 0), (100, 60), (0, 60)]
    ok, why = validate_route([[30, 20], [70, 20], [70, 40], [30, 40], [30, 20]],
                             0.6, outline)
    assert ok, f"闭合环被误拒: {why}"
    # 真自交仍要拒绝
    ok2, _ = validate_route([[10, 10], [50, 40], [50, 10], [10, 40]], 0.6, outline)
    assert not ok2, "蝴蝶结自交必须被拒绝"


def test_closed_loop_route_gets_inner_dam(data_root):
    """SLC-004：闭合环走线的内侧介电坝不能丢（否则银浆漏到下层）。"""
    from samples.gen_samples import kicad_board, two_pin

    from conftest import SAMPLES
    from ame.ledger.services import do_slice
    from ame.ledger.workspace import create_project

    ws = create_project(data_root, "闭合环")
    ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"), "p.step")
    comps = [two_pin("U1", 50, 30, "L1", "L2")]
    text = kicad_board([(0, 0), (100, 0), (100, 60), (0, 60)], comps,
                       [(1, "L1"), (2, "L2")])
    ws.cmd_import_board(text)
    ws.commit("导入")
    ws.cmd_add_route("L2", [[30, 20], [70, 20], [70, 40], [30, 40], [30, 20]], 0.6)
    sl = do_slice(ws)
    dam = next(p for p in sl["pass_details"]
               if p["kind"] == "circuit" and p["mat"] == "B")
    # 板框坝 + 外坝 + 内坝 >= 3 条
    assert len(dam["paths"]) >= 3, f"闭合环应有内外两条坝，实际 {len(dam['paths'])} 条"

    def closed(pts):
        return (len(pts) >= 3
                and abs(pts[0][0] - pts[-1][0]) < 1e-3
                and abs(pts[0][1] - pts[-1][1]) < 1e-3)

    assert sum(1 for p in dam["paths"] if closed(p["pts"])) >= 3


