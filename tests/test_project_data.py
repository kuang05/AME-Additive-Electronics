"""工程与版本/数据域验收测试（PRJ-001/002/005/006/009；DAT-002/004/005；NFR-002；T20）。

覆盖：
- PRJ-001：新建工程 → 初始版本 seq=0、画布为空、默认幅面 200×200；
- PRJ-002：关闭再打开 → 结构、走线、分数一致；
- PRJ-005：连续 10 步操作撤销再重做 → 状态一致，撤销不产生版本，op_log 有记录；
- DAT-002：每次 commit 产生新版本（前后分数/原因），旧版本记录不被覆盖；
- DAT-004/005：STEP 哈希入库（内容寻址），put_blob/get_blob 往返与 blob 表登记，
  哈希索引与文件内容分离（篡改文件后 get_blob 返回被改内容）；
- PRJ-006：工程包导出→删除→导入一致；导入到已有 id 拒绝；
- PRJ-009：软件设置修改后持久化（重启仍生效）；
- NFR-002：Store 打开不存在的 .db 不崩溃；乱码 STEP 导入抛 CmdError 且工程仍可用；
- T20：导入→打分→提交→切片→虚拟打印→重复切片哈希一致，全链无异常。
"""
import hashlib
from pathlib import Path

import pytest

from conftest import SAMPLES, DataRoot, build_project, create_project, do_slice, run_print
from ame.db.store import Store
from ame.ledger.services import gcode_of, latest_slice
from ame.ledger.workspace import CmdError, Workspace


# ---------------------------------------------------------------- PRJ-001
def test_prj001_new_project_empty_canvas_and_defaults(data_root):
    """PRJ-001：新建工程生成空工程与初始版本（seq=0）；
    画布为空（结构/电路均无）；默认单位 mm、默认幅面 200×200。"""
    ws = create_project(data_root, "PRJ-001 初始状态工程")
    h = ws.history()
    assert len(h) == 1 and h[0]["seq"] == 0, "应有且仅有 1 个初始版本（seq=0）"
    assert h[0]["reason"] == "新建工程"
    assert ws.structure() is None, "画布为空：尚无结构"
    assert ws.routes() == [] and ws.comps() == [] and ws.nets() == [], \
        "画布为空：无走线/器件/网络"
    assert ws.project["unit"] == "mm"
    assert ws.project["bed_x_mm"] == 200.0 and ws.project["bed_y_mm"] == 200.0


# ---------------------------------------------------------------- PRJ-002
def test_prj002_close_and_reopen_consistency(data_root):
    """PRJ-002：创建→导入→commit→关闭→重开，结构、走线、分数与关闭前一致。"""
    ws = build_project(data_root)
    ws.commit("导入结构与板文件")
    score0 = ws.run_mrc()["score"]
    n_routes0 = len(ws.routes())
    step_hash0 = ws.structure()["step_hash"]
    n_hist0 = len(ws.history())
    ws.close()
    # 重开（模拟关闭软件后再打开）
    store2 = data_root.open_project(ws.project_id)
    ws2 = Workspace(store2, ws.project_id)
    assert ws2.structure()["step_hash"] == step_hash0, "结构（STEP 哈希）一致"
    assert len(ws2.routes()) == n_routes0, "走线数量一致"
    assert ws2.run_mrc()["score"] == score0, "可打性分数一致"
    assert len(ws2.history()) == n_hist0, "版本历史完整"


# ---------------------------------------------------------------- PRJ-005
def test_prj005_undo_redo_10_steps_state_restored(data_root):
    """PRJ-005：连续 10 步操作（5 次布线 + 5 次移动器件）→ 撤销 10 步回到操作前，
    重做 10 步恢复到撤销前；撤销/重做过程版本数不变；op_log 记录了操作。"""
    ws = build_project(data_root)
    digest0 = ws.design_digest()
    n0 = len(ws.routes())
    v0 = len(ws.history())
    comp = ws.comps()[0]

    # 连续 10 步有效操作：布线（add_route）与移动器件（move_comp）交替
    for i in range(5):
        ws.cmd_add_route("TP1", [[30.0 + i, 8.0], [60.0 + i, 8.0]], 0.6)
        ws.cmd_move_comp(comp["id"], comp["x_mm"] + 1 + i, comp["y_mm"])
    digest1 = ws.design_digest()
    n1 = len(ws.routes())
    assert n1 == n0 + 5
    assert len(ws.history()) == v0, "会话内操作不产生版本（撤销通过新版本实现的只有 commit）"
    assert len(ws.op_log) >= 10 and all(e.get("cmd") for e in ws.op_log), \
        "操作日志应记录每步命令"

    # 连续撤销 10 步 → 状态与操作前一致
    for _ in range(10):
        assert ws.undo().startswith("已撤销")
    assert len(ws.undo_stack) == 2, "撤销栈仅剩 build_project 的两条导入操作"
    assert len(ws.routes()) == n0
    assert ws.design_digest() == digest0
    assert len(ws.history()) == v0, "撤销过程中版本数不变（PRJ-005）"

    # 连续重做 10 步 → 恢复到撤销前状态（同一对象、同 id 同内容）
    for _ in range(10):
        assert ws.redo().startswith("已重做")
    assert len(ws.routes()) == n1
    assert ws.design_digest() == digest1
    assert len(ws.history()) == v0


# ---------------------------------------------------------------- DAT-002
def test_dat002_versions_append_only_with_scores(data_root):
    """DAT-002：每次 commit 产生新版本，记录 score_before/score_after/reason；
    历史只追加，旧版本记录不被覆盖。"""
    ws = build_project(data_root)
    c1 = ws.commit("DAT-002 v1 导入完成")
    h1 = ws.history()
    assert [r["seq"] for r in h1] == [0, 1]
    v1 = h1[1]
    assert v1["reason"] == "DAT-002 v1 导入完成"
    assert v1["score_before"] is None, "初始版本（空工程）无前分数"
    assert v1["score_after"] == ws.run_mrc()["score"]

    # 修改 → 再提交：前后分数链式衔接
    thin = next(r for r in ws.routes() if r["width_mm"] < 0.5)
    ws.cmd_change_width(thin["id"], 0.6)
    c2 = ws.commit("DAT-002 v2 加宽走线")
    h2 = ws.history()
    assert [r["seq"] for r in h2] == [0, 1, 2]
    v2 = h2[2]
    assert v2["score_before"] == v1["score_after"]
    assert v2["score_after"] > v2["score_before"], "加宽消除 R1 违规，分数应上升"
    parent = ws.store.one("SELECT parent_id FROM version WHERE id=?", (c2["id"],))
    assert parent["parent_id"] == c1["id"], "版本链 parent 指向上一版本"

    # 旧版本记录不被覆盖（只追加）
    comp = ws.comps()[0]
    ws.cmd_move_comp(comp["id"], comp["x_mm"] + 0.5, comp["y_mm"])
    ws.commit("DAT-002 v3")
    h3 = ws.history()
    assert [r["seq"] for r in h3] == [0, 1, 2, 3]
    assert h3[1]["reason"] == "DAT-002 v1 导入完成", "v1 记录原样保留"
    assert h3[1]["score_after"] == v1["score_after"]


# ---------------------------------------------------------------- DAT-004/005
def test_dat004_dat005_blob_content_addressing(data_root):
    """DAT-004/005：STEP 哈希入库且按内容寻址；put_blob/get_blob 往返与 blob 表登记；
    哈希索引与文件内容分离——篡改 blob 文件后 get_blob 返回被改内容，还原后工程可用。"""
    ws = build_project(data_root)
    step_hash = ws.structure()["step_hash"]
    data = ws.store.get_blob(step_hash)
    assert hashlib.sha256(data).hexdigest() == step_hash, \
        "STEP 文件哈希入库：存储内容与登记哈希一致（DAT-005）"
    row = ws.store.one("SELECT * FROM blob WHERE sha256=?", (step_hash,))
    assert row is not None, "blob 表应登记该内容"
    assert row["size"] == len(data)
    blob_file = ws.store.data_dir / row["relpath"]
    assert blob_file.exists()

    # put_blob/get_blob 往返 + 同内容去重
    payload = b"AME-BLOB-ROUNDTRIP-\x01\x02"
    h1, _rel = ws.store.put_blob(payload)
    h2, _rel2 = ws.store.put_blob(payload)
    assert h1 == h2, "同内容同哈希（内容寻址去重）"
    assert ws.store.get_blob(h1) == payload
    assert ws.store.one("SELECT COUNT(*) AS c FROM blob WHERE sha256=?",
                        (h1,))["c"] == 1

    # 哈希索引与文件内容分离：直接篡改文件，get_blob 按登记路径返回被改内容
    original = blob_file.read_bytes()
    blob_file.write_bytes(b"TAMPERED")
    assert ws.store.get_blob(step_hash) == b"TAMPERED"
    # 还原文件 → 工程继续可用
    blob_file.write_bytes(original)
    assert ws.store.get_blob(step_hash) == original
    assert ws.run_mrc()["score"] >= 0


# ---------------------------------------------------------------- PRJ-006
def test_prj006_export_delete_import_roundtrip(data_root, tmp_path):
    """PRJ-006：导出工程包 → 删除本工程 → 导入，版本数/分数/走线数一致；
    导入到已存在的工程 id → FileExistsError。"""
    ws = build_project(data_root)
    ws.commit("导出前提交")
    pid = ws.project_id
    score0 = ws.run_mrc()["score"]
    n_routes0 = len(ws.routes())
    n_hist0 = len(ws.history())
    step_hash0 = ws.structure()["step_hash"]

    pkg = tmp_path / "prj006.amepkg"
    data_root.export_project(ws.store, pkg)
    assert pkg.exists()

    # 删除本工程（含 WAL 边车文件）
    db_path = ws.store.db_path
    ws.store.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    assert not db_path.exists()

    # 导入 → 数据一致
    store2 = data_root.import_project(pkg)
    ws2 = Workspace(store2, pid)
    assert ws2.structure()["step_hash"] == step_hash0
    assert len(ws2.routes()) == n_routes0
    assert ws2.run_mrc()["score"] == score0
    assert len(ws2.history()) == n_hist0

    # 再导入到已有 id → 拒绝
    with pytest.raises(FileExistsError):
        data_root.import_project(pkg)


# ---------------------------------------------------------------- PRJ-009
def test_prj009_settings_persist_across_restart(data_root):
    """PRJ-009：save_settings(unit='inch') 后重新读取数据目录，设置仍生效；
    未修改项保持默认。"""
    data_root.save_settings(unit="inch", log_level="debug")
    root2 = DataRoot(data_root.data_dir)      # 模拟重启：新建 DataRoot 重读设置文件
    assert root2.settings["unit"] == "inch"
    assert root2.settings["log_level"] == "debug"
    assert root2.settings["autosave_interval_s"] == 60, "未修改项保持默认值"


# ---------------------------------------------------------------- NFR-002
def test_nfr002_store_open_fresh_db_and_bad_step_robustness(data_root):
    """NFR-002：Store 打开不存在的合法 .db 不崩溃；乱码 STEP 导入抛 CmdError
    不崩溃，失败不改变状态，工程随后仍可正常导入。"""
    # 1) 打开不存在的 .db → 建库成功、可查询
    st = Store(data_root.data_dir / "projects" / "fresh_any.db")
    assert st.one("SELECT COUNT(*) AS c FROM project")["c"] == 0
    st.close()

    # 2) 坏 STEP（乱码）导入：CmdError、不崩溃、工程仍可用
    ws = create_project(data_root, "NFR-002 鲁棒性工程")
    garbage = "这不是STEP内容 \x01\x02 {{{broken}}"
    with pytest.raises(CmdError):
        ws.cmd_import_step(garbage, "broken.step")
    assert ws.structure() is None, "失败不改变状态（CIR-012/STR-009）"
    ok = ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                            "plate_100x60.step")
    assert ok["face_count"] > 0, "继续导入正常 STEP 应成功"
    assert ws.run_mrc()["score"] >= 0, "工程仍可用"


# ---------------------------------------------------------------- T20
def test_t20_full_chain_import_mrc_commit_slice_print(data_root):
    """T20 全链（§7.1）：导入 → 面判定/打分 → 提交 → 切片 → G-code → 虚拟回放。

    1. 导入 STEP 结构与 T19-A 板文件（违规走线随板文件 segment 入库）；
    2. 可打性打分：同一模型重复检查分数一致（MRC-008）；
    3. 提交版本（DAT-002），切片绑定该版本（SLC-010）；
    4. 切片：三层 7 道次（§3.5），G-code 入库；
    5. 虚拟打印（无故障）：作业 done、无缺陷、耗时>0（PRT-006/T17）；
    6. 再次切片：G-code 哈希与首次一致（SLC-008/AGT-008 重放一致）；
    7. 切片记录中的 gcode_hash 为内容哈希，与 G-code 文本相符（DAT-005）。
    全程无异常即通过。
    """
    # 1. 导入
    ws = build_project(data_root, name="T20 全链工程")
    assert ws.structure() is not None and len(ws.routes()) == 8

    # 2. 打分（面判定/规则检查在 run_mrc 内完成）
    mrc1 = ws.run_mrc()
    mrc2 = ws.run_mrc()
    assert mrc1["score"] == mrc2["score"], "同一模型重复检查分数一致"
    assert 0 <= mrc1["score"] <= 100

    # 3. 提交版本
    c = ws.commit("T20：导入完成")
    assert c["seq"] == 1 and ws.project["head_commit_id"] == c["id"]

    # 4. 切片
    sl = do_slice(ws)
    assert sl["layer_count"] == 3
    assert sl["passes"] == 7, "默认工艺为典型 7 道次"
    assert sl["gcode_hash"]

    # 5. 虚拟打印（无故障）
    pr = run_print(ws)
    assert pr["state"] == "done"
    assert pr["defects"] == []
    assert pr["elapsed_s"] > 0
    job_row = ws.store.one("SELECT state FROM print_job WHERE id=?", (pr["job_id"],))
    assert job_row["state"] == "done"

    # 6. 重复切片：哈希一致（确定性/重放一致）
    sl2 = do_slice(ws)
    assert sl2["gcode_hash"] == sl["gcode_hash"]

    # 7. 入库哈希与 G-code 内容相符
    from ame.slicing.gcode import gcode_content_hash
    gcode = gcode_of(ws, sl["slice_set_id"])
    assert latest_slice(ws)["gcode_hash"] == gcode_content_hash(gcode)


# ---------------------------------------------------------------- 设计摘要：跨工程/跨主键稳定
def test_design_digest_stable_across_projects(tmp_path):
    """同一份设计在不同数据目录各建一次，design_digest 必须相同（SLC-008）。

    这是比"同一工程内重复切片哈希一致"更强的要求：uid 由每次导入重新生成，
    走线的 circuit_id/net_id、器件的 circuit_id 都会变。若摘要里混入任何
    主键/外键，同一块板在两个工程里就会算出两个 G-code 头哈希，
    "G-code 可复现"只在单个数据目录内成立，跨机器/跨目录就失效。
    """
    from ame.db.store import DataRoot
    from ame.ledger.workspace import create_project
    from tests.conftest import SAMPLES

    digests = []
    for i in range(2):
        root = DataRoot(tmp_path / f"d{i}")
        ws = create_project(root, f"跨工程-{i}")
        ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                           "plate_100x60.step")
        ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
        digests.append(ws.design_digest())
        ws.store.close()

    assert digests[0] == digests[1], "同一设计内容必须得到同一设计摘要"


def test_gcode_hashes_equal_across_projects_for_same_design(tmp_path):
    """端到端：同一设计在两个工程里切出的 G-code **运动内容**一致（SLC-008）。

    口径说明：G-code 头里的 ``;project``（工程名）与 ``;source_commit``（版本
    uuid）是**溯源信息**，跨工程本就应当不同——它们指向不同的工程记录。
    可复现性要求的是"设计相同 → 打印动作相同"，因此比较时剔除这两行与
    依赖它们计算的 ``;gcode_sha256``，其余逐字节一致。

    这个测试覆盖了原先没有的一层：`design_sha256` 跨工程稳定
    （主键/外键已从摘要里剥离），以及切片路径顺序不依赖 uuid。
    """
    from ame.db.store import DataRoot
    from ame.ledger.services import do_slice, gcode_of
    from ame.ledger.workspace import create_project
    from tests.conftest import SAMPLES

    def _motion_body(text: str) -> str:
        keep = []
        for line in text.splitlines():
            if (line.startswith(";project") or line.startswith(";source_commit")
                    or line.startswith(";gcode_sha256")):
                continue
            keep.append(line)
        return "\n".join(keep)

    bodies, designs = [], []
    for i in range(2):
        root = DataRoot(tmp_path / f"g{i}")
        ws = create_project(root, f"G跨工程-{i}")
        ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                           "plate_100x60.step")
        ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
        ws.commit("基线")
        sl = do_slice(ws)
        bodies.append(_motion_body(gcode_of(ws, sl["slice_set_id"])))
        designs.append(ws.design_digest())
        ws.store.close()

    assert designs[0] == designs[1], "设计摘要必须跨工程一致"
    assert bodies[0] == bodies[1], \
        "同一设计的 G-code 运动段应跨工程逐字节一致（否则跨机器无法复现）"
