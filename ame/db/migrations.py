"""工程库结构迁移（DAT-006）：逐级升级 + 迁移前自动备份。

从 `db/store.py` 拆出的独立子系统。为什么单独成文件：

- 这是全项目**唯一会改写用户既有数据文件**的代码，必须能单独审阅与测试；
- 迁移步骤只增不减（v1→v10），随版本累积会持续膨胀，混在 Store 里
  会把"日常读写"的代码淹没；
- 每步迁移都对应一次历史缺陷（例如 v9 补索引：缺索引时 slice_set/path
  全表扫描），独立成模块后这些因果关系有地方写清楚。

约定：`migrate(store)` 接收 `Store` 实例，直接操作其 ``conn``；
返回实际执行的步骤名列表（空列表表示已是最新）。调用方负责 commit。

结构：每个版本步是 `_vN_*(store, steps)` 函数，登记在 `_STEPS` 表里。
migrate 本体只做"备份 → 按版本号逐级执行 → 更新版本号"，不再随版本
增长复杂度（此前是一个 130 行的平铺 if 链，CC 24；新函数 2 步起、
每步一个函数，单函数 CC ≤ 6）。
"""
from __future__ import annotations

import shutil
import sqlite3
import time

# 工程库结构当前版本号。定义在迁移模块（唯一的版本消费方），
# store.py 反过来从本模块导入，避免循环依赖。
CURRENT_SCHEMA_VERSION = 10

def _v2_pad_dims(store, steps: list[str]) -> None:
    """v2: pad 增加长宽（早期焊盘只有中心点，无法做占位面积判定）。"""
    cols = store._columns("pad") if store._table_exists("pad") else set()
    if "w_mm" not in cols:
        store.conn.execute(
            "ALTER TABLE pad ADD COLUMN w_mm REAL NOT NULL DEFAULT 1.5")
        steps.append("pad+w_mm")
    if "h_mm" not in cols:
        store.conn.execute(
            "ALTER TABLE pad ADD COLUMN h_mm REAL NOT NULL DEFAULT 1.0")
        steps.append("pad+h_mm")


def _v3_route_strategy(store, steps: list[str]) -> None:
    """v3: slice_set 加 gcode_blob；route.strategy 放宽 CHECK 加 'imported'。"""
    # slice_set.gcode_blob（可空列，直接加）
    if store._table_exists("slice_set") and \
            "gcode_blob" not in store._columns("slice_set"):
        store.conn.execute("ALTER TABLE slice_set ADD COLUMN gcode_blob TEXT")
        steps.append("slice_set+gcode_blob")
    # board_outline 表由 schema.sql 的 CREATE IF NOT EXISTS 已建好
    # route.strategy 的 CHECK 需要重建表才能放宽（SQLite 不支持改约束）
    if not store._table_exists("route"):
        return
    old_cols = store._columns("route")
    # 表重建期间关闭外键检查（SQLite 官方推荐的 12 步流程）；
    # 旧库可能存在孤儿行，迁移不应因历史脏数据失败
    store.conn.execute("PRAGMA foreign_keys = OFF")
    store.conn.execute("ALTER TABLE route RENAME TO route_mig_old")
    store.conn.executescript("""
        CREATE TABLE route (
          id            TEXT PRIMARY KEY,
          circuit_id    TEXT NOT NULL REFERENCES circuit(id),
          net_id        TEXT NOT NULL REFERENCES net(id),
          pts_json      TEXT NOT NULL,
          width_mm      REAL NOT NULL,
          surface       TEXT NOT NULL DEFAULT 'top' CHECK (surface IN ('top','bottom')),
          layer_index   INTEGER NOT NULL DEFAULT 0,
          strategy      TEXT NOT NULL DEFAULT 'manual'
                        CHECK (strategy IN ('manual','auto','jumper','imported'))
        );
    """)
    cols = ",".join(sorted(old_cols & {
        "id", "circuit_id", "net_id", "pts_json", "width_mm",
        "surface", "layer_index", "strategy"}))
    store.conn.execute(
        f"INSERT INTO route({cols}) SELECT {cols} FROM route_mig_old")
    store.conn.execute("DROP TABLE route_mig_old")
    store.conn.execute("CREATE INDEX IF NOT EXISTS idx_route_net ON route(net_id)")
    store.conn.execute("PRAGMA foreign_keys = ON")
    steps.append("route.strategy+imported")


def _v4_process_params_layers(store, steps: list[str]) -> None:
    """v4: process_params 增加下保护层厚与道次数（SLC-001 要求可设定各层厚度）。"""
    if not store._table_exists("process_params"):
        return
    pp_cols = store._columns("process_params")
    if "bottom_layer_mm" not in pp_cols:
        store.conn.execute(
            "ALTER TABLE process_params ADD COLUMN bottom_layer_mm "
            "REAL NOT NULL DEFAULT 0.08")
        steps.append("process_params+bottom_layer_mm")
    if "bottom_passes" not in pp_cols:
        store.conn.execute(
            "ALTER TABLE process_params ADD COLUMN bottom_passes "
            "INTEGER NOT NULL DEFAULT 3")
        steps.append("process_params+bottom_passes")


def _v5_outline_holes(store, steps: list[str]) -> None:
    """v5: 板框支持内孔（STR-004/SLC-004：螺丝孔等内腔不应被填实）。"""
    for table in ("structure", "board_outline"):
        if store._table_exists(table) and \
                "holes_json" not in store._columns(table):
            store.conn.execute(
                f"ALTER TABLE {table} ADD COLUMN holes_json "
                "TEXT NOT NULL DEFAULT '[]'")
            steps.append(f"{table}+holes_json")


def _v6_safe_point(store, steps: list[str]) -> None:
    """v6: 断电续打的安全点必须落库（PRT-014）。

    原先只存在内存 JobState 里，进程重启后无法定位安全点，
    "重开软件继续打"根本走不通。
    """
    if store._table_exists("print_job") and \
            "safe_point_json" not in store._columns("print_job"):
        store.conn.execute(
            "ALTER TABLE print_job ADD COLUMN safe_point_json TEXT")
        steps.append("print_job+safe_point_json")


def _v7_layer_height(store, steps: list[str]) -> None:
    """v7: layer 增加 h_mm（MRC-005 体积遮挡判定需要层厚）。

    早期只存 z（顶面），无法判断"某条走线的高度区间是否被已打印材料占据"。
    """
    if store._table_exists("layer") and "h_mm" not in store._columns("layer"):
        store.conn.execute(
            "ALTER TABLE layer ADD COLUMN h_mm REAL NOT NULL DEFAULT 0")
        steps.append("layer+h_mm")


def _v8_keepout(store, steps: list[str]) -> None:
    """v8: 禁布区表（CIR-006：自动布线与手动落线都要避开）。"""
    if not store._table_exists("keepout"):
        store.conn.execute(
            "CREATE TABLE IF NOT EXISTS keepout ("
            "id TEXT PRIMARY KEY,"
            "project_id TEXT NOT NULL REFERENCES project(id),"
            "name TEXT NOT NULL DEFAULT '',"
            "pts_json TEXT NOT NULL,"
            "note TEXT NOT NULL DEFAULT '')")
        steps.append("keepout")


def _v9_query_indexes(store, steps: list[str]) -> None:
    """v9: 补高频查询索引（实测缺索引时 slice_set/path/mrc_issue 全表扫描）。"""
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_slice_latest ON slice_set(project_id, stale, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_path_layer ON path(layer_id, seq)",
        "CREATE INDEX IF NOT EXISTS idx_issue_commit ON mrc_issue(commit_id, severity)",
        "CREATE INDEX IF NOT EXISTS idx_pad_ref ON pad(comp_id, pad_no)",
        "CREATE INDEX IF NOT EXISTS idx_route_circuit ON route(circuit_id)",
        "CREATE INDEX IF NOT EXISTS idx_job_started ON print_job(started_at, id)",
        "CREATE INDEX IF NOT EXISTS idx_comp_circuit ON comp(circuit_id)",
        "CREATE INDEX IF NOT EXISTS idx_keepout_project ON keepout(project_id)",
    ):
        try:
            store.conn.execute(ddl)
        except sqlite3.OperationalError:
            pass          # 表可能不存在（老库缺表），跳过
    steps.append("query-indexes")


def _v10_skip_log(store, steps: list[str]) -> None:
    """v10: print_job 增加跳层记录列（PRT-012：跳层必须留痕可查）。"""
    if store._table_exists("print_job") and             "skip_log_json" not in store._columns("print_job"):
        store.conn.execute("ALTER TABLE print_job ADD COLUMN skip_log_json TEXT")
        steps.append("print_job+skip_log_json")


# (目标版本, 迁移函数)。逐级语义：库版本 v 执行所有 threshold > v 的步骤。
# 各步只碰自己的表/列、都带存在性守卫，互不依赖，因此按版本号升序执行。
_STEPS: tuple[tuple[int, object], ...] = (
    (2, _v2_pad_dims),
    (3, _v3_route_strategy),
    (4, _v4_process_params_layers),
    (5, _v5_outline_holes),
    (6, _v6_safe_point),
    (7, _v7_layer_height),
    (8, _v8_keepout),
    (9, _v9_query_indexes),
    (10, _v10_skip_log),
)


def _backup(store, v: int, steps: list[str]) -> None:
    """迁移前自动备份（DAT-006）：整库拷贝到 backups/。"""
    backup_dir = store.data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{store.db_path.stem}-v{v}-{stamp}.db"
    store.conn.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy2(store.db_path, backup)
    steps.append(f"backup:{backup.name}")


def migrate(store) -> list[str]:
    """把旧版本库逐级迁移到 CURRENT；返回实际执行的迁移步骤。"""
    v = store._schema_version()
    if v >= CURRENT_SCHEMA_VERSION:
        return []
    steps: list[str] = []
    _backup(store, v, steps)
    for threshold, apply_step in _STEPS:
        if v < threshold:
            apply_step(store, steps)
    # 只更新版本号，不改其他字段
    store.conn.execute("UPDATE project SET schema_version=?",
                       (CURRENT_SCHEMA_VERSION,))
    store.conn.commit()
    steps.append(f"version->{CURRENT_SCHEMA_VERSION}")
    return steps
