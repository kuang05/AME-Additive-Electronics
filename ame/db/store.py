"""存储层：SQLite 工程 + 内容寻址大文件（blob）。

目录布局（DAT-004，数据与安装目录分离）：
  <data_dir>/projects/<project_id>.db   每工程一个库（22+2 表）
  <data_dir>/blobs/ab/cd/<sha256>       内容寻址：STEP / 证据 / G-code / 快照
  <data_dir>/settings.json              软件设置（PRJ-009）
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
import os
import shutil
import sqlite3
import time
from pathlib import Path

from .. import sha256_bytes


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_project_id(pid) -> str:
    """校验工程 id 可安全用作文件名（NFR-005：拒绝路径穿越）。

    工程 id 会拼成 ``projects/<id>.db``。若不校验，包内写
    ``"id": "../../escaped"`` 就能把数据库写到数据目录之外。
    只允许字母数字与 ``._-``，且不得为 ``.`` / ``..``。
    """
    if not isinstance(pid, str) or not pid:
        raise ValueError(f"工程包缺少合法 id：{pid!r}（NFR-005）")
    if pid in (".", "..") or not _SAFE_ID_RE.match(pid):
        raise ValueError(
            f"工程包内的工程 id 非法：{pid!r}。只允许字母、数字与 . _ -（NFR-005）")
    return pid


def resource_path(rel: str) -> Path:
    """定位随包资源（samples/ 等）。开发态相对仓库，打包态在 _MEIPASS 下。"""
    import sys as _sys
    base = getattr(_sys, "_MEIPASS", None)
    if base:
        cand = Path(base) / rel
        if cand.exists():
            return cand
    # 开发态：从本文件向上找仓库根（ame/db/store.py → 仓库根）
    root = Path(__file__).resolve().parent.parent.parent
    return root / rel


def _resource_path(rel: str) -> Path:
    """定位随包资源（schema.sql 等）。

    开发态：相对源码目录。
    打包态（PyInstaller onefile/onedir）：资源被解到 sys._MEIPASS。
    """
    import sys as _sys
    base = getattr(_sys, "_MEIPASS", None)
    if base:
        cand = Path(base) / "ame" / "db" / rel
        if cand.exists():
            return cand
        cand2 = Path(base) / rel
        if cand2.exists():
            return cand2
    return Path(__file__).with_name(rel)


SCHEMA_PATH = _resource_path("schema.sql")

# 工程库结构版本（DAT-006：数据带版本号，新版本能打开旧工程，迁移前自动备份）
#   v1: 初版（pad 无 w_mm/h_mm；slice_set 无 gcode_blob；无 board_outline 表）
#   v2: + pad.w_mm / pad.h_mm
#   v3: + slice_set.gcode_blob；+ board_outline 表；route.strategy 支持 'imported'
#   v4: + process_params.bottom_layer_mm / bottom_passes（层厚可设定，SLC-001）
#   v5: + structure.holes_json / board_outline.holes_json（板框内孔）
from .migrations import CURRENT_SCHEMA_VERSION  # noqa: E402


class IntegrityError(RuntimeError):
    """内容寻址文件与记录的哈希不符（DAT-005：外部修改/损坏）。"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class Store:
    """一个工程库的读写入口（SQLite 实现，满足 db/storage.py 的 Storage 协议）。

    换存储后端时，只需另写一个满足同一协议的类，业务层（ledger/agent/web）
    不需要改动——它们只调用 execute/query/one/batch/put_blob/get_blob/
    verify_blob/close 这 8 个方法。
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.data_dir = self.db_path.parent.parent  # projects/<id>.db -> data_dir
        self.blobs_dir = self.data_dir / "blobs"
        # check_same_thread=False：允许后台线程读取/写入（界面长任务在线程池执行）。
        # 写入通过 self._lock 串行化，避免并发写冲突（SQLite 单写者模型）。
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.RLock()
        self._batch_depth = 0
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.migrated = self._migrate()
        self.conn.commit()

    # ---------- 结构迁移（DAT-006：迁移前自动备份，迁移可重入） ----------
    def _table_exists(self, name: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone())

    def clone_db_file(self, tag: str = "clone") -> Path:
        """把当前库文件整份拷到一个新路径（重放用的快照）。

        WAL 模式下必须先 checkpoint，否则拷贝的文件可能缺最近提交的事务。
        这是 SQLite 特有的准备工作，放在实现层，业务层（agent/loop）
        只表达"要一个快照"。
        """
        self.conn.execute("PRAGMA wal_checkpoint(FULL)")
        dst = Path(self.db_path).with_name(f"{self.db_path.stem}-{tag}.db")
        shutil.copy2(self.db_path, dst)
        return dst

    def vacuum(self) -> None:
        """回收磁盘空间（SQLite 的 VACUUM）。

        PG 实现不提供（`PgStore` 没有这个方法）：PG 的 VACUUM 不能在事务里跑、
        且通常由 autovacuum 自动处理，业务层无需显式调用。
        调用方用 `getattr(store, "vacuum", None)` 探测。
        """
        self.conn.execute("VACUUM")

    # ---- Storage 协议的方言方法（业务层不再直接写 SQLite 语法）----
    def has_table(self, name: str) -> bool:
        """表是否存在（`_table_exists` 的公开版本，供业务层调用）。"""
        return self._table_exists(name)

    def change_token(self) -> tuple:
        """库变更指纹：外部连接的写入(PRAGMA data_version) + 本连接写入计数。"""
        row = self.conn.execute("PRAGMA data_version").fetchone()
        return (int(row[0]) if row else 0, self.conn.total_changes)

    def recent_order(self, alias: str = "", time_col: str = "created_at") -> str:
        """`<时间列> DESC, rowid DESC`：SQLite 有隐式 rowid 伪列，可直接用。"""
        p = f"{alias}." if alias else ""
        return f"{p}{time_col} DESC, {p}rowid DESC"

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _schema_version(self) -> int:
        if not self._table_exists("project"):
            return CURRENT_SCHEMA_VERSION      # 空库，按当前版处理
        row = self.conn.execute("SELECT schema_version FROM project LIMIT 1").fetchone()
        if row is None or not row[0]:
            # project 表存在但还没有行（新建工程进行中）——不能当作旧版本触发迁移
            return CURRENT_SCHEMA_VERSION
        return int(row[0])

    def _migrate(self) -> list[str]:
        """逐级迁移到 CURRENT（实现在 db/migrations.py，见该模块说明）。"""
        from .migrations import migrate
        return migrate(self)

    # ---------- 通用 ----------
    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            if self._batch_depth == 0:
                self.conn.commit()  # 单条命令立即落盘（PRJ-003 自动保存）
            return cur

    @contextlib.contextmanager
    def batch(self) -> None:
        """批量写入：整个 with 块只 commit 一次（切片落库 800+ 条时快 10 倍以上）。

        退出时统一提交；异常则回滚，保证不会留下半套数据（NFR-002 原子性）。
        """
        self._lock.acquire()
        self._batch_depth += 1
        try:
            yield self
        except Exception:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.conn.rollback()
            self._lock.release()
            raise
        else:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.conn.commit()
            self._lock.release()

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        self.conn.close()

    def _backup_to(self, dst_path: Path):
        """SQLite 在线备份到目标路径（可覆盖以便测试注入故障）。"""
        dst_conn = sqlite3.connect(str(dst_path))
        try:
            self.conn.backup(dst_conn)
        finally:
            dst_conn.close()

    # ---------- 内容寻址 blob（DAT-004：大文件按内容哈希存储） ----------
    def put_blob(self, data: bytes) -> tuple[str, str]:
        h = sha256_bytes(data)
        rel = f"blobs/{h[:2]}/{h[2:4]}/{h}"
        path = self.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        self.conn.execute(
            "INSERT OR IGNORE INTO blob(sha256,size,relpath) VALUES (?,?,?)",
            (h, len(data), rel.replace("\\", "/")),
        )
        self.conn.commit()
        return h, rel.replace("\\", "/")

    def get_blob(self, sha: str) -> bytes:
        row = self.one("SELECT relpath FROM blob WHERE sha256=?", (sha,))
        if not row:
            raise KeyError(f"blob 不存在: {sha[:12]}")
        return (self.data_dir / row["relpath"]).read_bytes()

    def verify_blob(self, sha: str) -> tuple[bool, str]:
        """校验内容寻址文件未被外部修改（DAT-005）。

        返回 (是否一致, 说明)。文件缺失也算不一致——用户可能删了 blobs/ 目录。
        """
        row = self.one("SELECT relpath FROM blob WHERE sha256=?", (sha,))
        if not row:
            return False, f"索引缺失（blob {sha[:12]} 未登记）"
        p = self.data_dir / row["relpath"]
        if not p.exists():
            return False, f"文件缺失：{row['relpath']}"
        try:
            data = p.read_bytes()
        except OSError as e:
            return False, f"读取失败：{e}"
        got = sha256_bytes(data)
        if got != sha:
            return False, (f"内容与记录不符（记录 {sha[:12]}…，实际 {got[:12]}…），"
                           f"文件可能被外部程序修改过")
        return True, "一致"

    def blob_path(self, rel_or_sha: str) -> Path:
        if (self.data_dir / rel_or_sha).exists():
            return self.data_dir / rel_or_sha
        row = self.one("SELECT relpath FROM blob WHERE sha256=?", (rel_or_sha,))
        if not row:
            raise KeyError(f"blob 不存在: {rel_or_sha[:12]}")
        return self.data_dir / row["relpath"]


class DataRoot:
    """数据目录级服务：工程列表、设置、备份（PRJ-002/006/009，NFR-008）。

    ``store_factory``：可选，把 "打开一个工程库" 的行为外置。
    默认返回 SQLite ``Store``；云上由 `web` 层注入 PostgreSQL 实现，
    业务层（ledger/agent）完全不需要感知后端差异。
    签名：``factory(project_id) -> Storage``。
    """

    def __init__(self, data_dir: str | Path | None = None,
                 store_factory=None):
        self._store_factory = store_factory
        base = os.environ.get("AME_DATA_DIR")
        if data_dir is None:
            data_dir = base if base else str(Path.home() / ".ame")
        self.data_dir = Path(data_dir)
        (self.data_dir / "projects").mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self._settings: dict | None = None

    # ---------- 设置（PRJ-009） ----------
    DEFAULT_SETTINGS = {
        "unit": "mm",
        "autosave_interval_s": 60,
        "cloud_enabled": False,          # 产品版默认关；比赛版可开（§5.4）
        "log_level": "info",
        "data_dir": None,
    }

    @property
    def settings(self) -> dict:
        if self._settings is None:
            if self.settings_path.exists():
                self._settings = {**self.DEFAULT_SETTINGS,
                                  **json.loads(self.settings_path.read_text("utf-8"))}
            else:
                self._settings = dict(self.DEFAULT_SETTINGS)
        return self._settings

    def save_settings(self, **kv):
        self.settings.update(kv)
        self.settings_path.write_text(
            json.dumps(self.settings, ensure_ascii=False, indent=2), "utf-8")

    # ---------- 工程列表 ----------
    def list_projects(self) -> list[dict]:
        """列出全部工程。

        两种后端走不同来源：

        - **SQLite**：扫 `projects/*.db`，逐个读 project 表首行（原有路径）。
        - **PostgreSQL**：工程分布在各自 schema 里，没有文件可扫。查
          `ame_catalog` 目录表（由 `ensure_catalog` 维护），一次查询搞定。
          早期这里只扫 *.db，**PG 模式下工程列表恒为空**——云上 `GET /api/projects`
          永远返回 []，前端进不去任何工程，`/api/health` 的 projects 计数也恒为 0。
        """
        if self._store_factory is not None:
            return self._list_projects_pg()
        out = []
        for f in sorted((self.data_dir / "projects").glob("*.db")):
            try:
                conn = sqlite3.connect(str(f))
                row = conn.execute(
                    "SELECT id,name,created_at,head_commit_id FROM project LIMIT 1"
                ).fetchone()
                conn.close()
                if row:
                    out.append({"id": row[0], "name": row[1], "created_at": row[2],
                                "head_commit_id": row[3], "file": f.name})
            except sqlite3.Error:
                continue
        out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        return out

    def _list_projects_pg(self) -> list[dict]:
        """PG：从 `ame_catalog` 目录表读工程列表。"""
        try:
            st = self.open_project("__catalog__")
        except Exception:
            return []
        try:
            rows = st.query("SELECT project_id AS pid, name, created_at, "
                            "head_commit_id FROM ame_catalog "
                            "ORDER BY created_at DESC")
        except Exception:
            rows = []
        finally:
            with contextlib.suppress(Exception):
                st.close()
        return [{"id": r["pid"], "name": r["name"], "created_at": r["created_at"],
                 "head_commit_id": r["head_commit_id"]} for r in rows]

    def open_project(self, project_id: str):
        """打开一个工程库（返回满足 Storage 协议的实例）。

        `project_id` 直接参与文件名拼接，必须先校验（NFR-005）。
        未校验时 `GET /api/projects/..%5C..%5Cvictim/state` 这类请求会在
        数据目录之外创建/覆盖 SQLite 文件并写入 22 张表——`_safe_project_id`
        本来就是为此写的，早期只接在工程包导入路径上，Web 主链路漏掉了。

        **注意**：本方法会按需**创建**库（`create_project` 依赖这个行为）。
        只读场景（Web 请求）请用 `open_existing_project`，否则请求一个不存在的
        id 就会在磁盘上留下 `.db`/`-wal`/`-shm` 三件套——公网暴露时一个扫描器
        就能把磁盘写满。
        """
        pid = _safe_project_id(project_id)
        if self._store_factory is not None:
            return self._store_factory(pid)
        return Store(self.data_dir / "projects" / f"{pid}.db")

    def open_existing_project(self, project_id: str):
        """打开**已存在**的工程库；不存在则抛 FileNotFoundError，不建新库。

        Web 的每个请求都走这里：先查文件是否存在，再决定要不要构造 Store。
        `Store.__init__` 会执行 schema 建表，所以"先构造再检查"等于必然落盘。
        """
        pid = _safe_project_id(project_id)
        if self._store_factory is not None:
            # PG：构造 PgStore 会 CREATE SCHEMA + 建 26 张表，所以必须先查目录表。
            # 早期这里直接 return factory(pid)，等于"打开不存在的工程就建一个新
            # schema"——把 SQLite 版用 path.exists() 挡住的"扫描器刷爆磁盘"
            # 变成了"刷爆 RDS schema 数量"（每个 8 位 uuid 一个 schema，永不回收）。
            if pid != "__catalog__" and not self._pg_project_exists(pid):
                raise FileNotFoundError(f"工程 {pid} 不存在")
            return self._store_factory(pid)
        path = self.data_dir / "projects" / f"{pid}.db"
        if not path.exists():
            raise FileNotFoundError(f"工程 {pid} 不存在")
        return Store(path)

    def _pg_project_exists(self, pid: str) -> bool:
        """在 `ame_catalog` 目录表里查工程是否存在。"""
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return False
        try:
            row = cat.one("SELECT 1 AS x FROM ame_catalog WHERE project_id=?", (pid,))
            return bool(row)
        except Exception:
            return False
        finally:
            with contextlib.suppress(Exception):
                cat.close()

    # ---------- 工程复制 / 重命名 / 删除（PRJ-007，P1） ----------
    TRASH_DIR = "trash"

    def _project_file(self, project_id: str) -> Path:
        f = self.data_dir / "projects" / f"{_safe_project_id(project_id)}.db"
        if not f.exists():
            raise FileNotFoundError(f"工程 {project_id} 不存在")
        return f

    def rename_project(self, project_id: str, new_name: str) -> str:
        """重命名工程（只改名称，不影响 id 与历史）。"""
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("工程名称不能为空")
        st = self.open_existing_project(project_id)
        st.execute("UPDATE project SET name=? WHERE id=?", (new_name, project_id))
        st.close()
        # PG：目录表是工程列表的唯一来源，改名必须同步，否则列表里还是旧名
        self._pg_catalog_update(project_id, name=new_name)
        return new_name

    def _pg_catalog_update(self, pid: str, name: str | None = None,
                           head: str | None = None) -> None:
        """更新 `ame_catalog` 目录行（SQLite 模式无操作）。"""
        if self._store_factory is None:
            return
        sets, params = [], []
        if name is not None:
            sets.append("name=?")
            params.append(name)
        if head is not None:
            sets.append("head_commit_id=?")
            params.append(head)
        if not sets:
            return
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return
        try:
            cat.execute(f"UPDATE ame_catalog SET {', '.join(sets)} "
                        "WHERE project_id=?", (*params, pid))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                cat.close()

    def _pg_catalog_remove(self, pid: str) -> None:
        if self._store_factory is None:
            return
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return
        try:
            cat.execute("DELETE FROM ame_catalog WHERE project_id=?", (pid,))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                cat.close()

    def copy_project(self, project_id: str, new_name: str | None = None) -> dict:
        """复制工程（PRJ-007：复制后的工程与原工程互不影响）。

        用 SQLite 在线备份 API（Connection.backup）复制库文件——手工 copy2
        在 WAL 模式下会拿到不含最新事务的主文件，时序不可靠。
        复制后生成新 id，blob 文件逐个落到新工程可访问的位置。
        """
        import uuid
        src = self.open_existing_project(project_id)
        if self._store_factory is not None:
            # PG：没有文件可备份，走 schema 复制
            return self._pg_copy_project(src, project_id, new_name)
        src_meta = src.one("SELECT * FROM project")
        if not src_meta:
            raise FileNotFoundError(f"工程 {project_id} 不存在或已损坏")
        new_id = str(uuid.uuid4())
        dst_name = (new_name or "").strip() or f"{src_meta['name']} 副本"
        dst_path = self.data_dir / "projects" / f"{new_id}.db"
        # 在线备份：把源库完整复制到新路径（含 schema 与全部数据）
        src._backup_to(dst_path)

        dst = Store(dst_path)
        # 改主键 id：按 SQLite 惯例先关外键（事务内 PRAGMA 无效，须在事务外）
        dst.conn.execute("PRAGMA foreign_keys = OFF")
        with dst.batch():
            dst.execute("UPDATE project SET id=?, name=? WHERE id=?",
                        (new_id, dst_name, project_id))
            # 子表引用同步指向新 id（FK OFF 期间手动维护引用完整性）
            for table in ("version", "session", "structure", "circuit", "slice_set",
                          "plan", "board_outline"):
                if dst._table_exists(table):
                    dst.execute(f"UPDATE {table} SET project_id=? WHERE project_id=?",
                                (new_id, project_id))
            # 会话属于原工程，不随副本复制
            dst.execute("DELETE FROM session")
            # blob 文件按 relpath 落到本 data_dir（内容寻址，同目录时跳过）
            for b in dst.query("SELECT relpath FROM blob"):
                src_file = self.data_dir / b["relpath"]
                if src_file.exists():
                    dst_file = dst.data_dir / b["relpath"]
                    if not dst_file.exists():
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst_file)
        dst.conn.execute("PRAGMA foreign_keys = ON")
        n_routes = dst.one("SELECT COUNT(*) c FROM route")["c"]
        n_versions = dst.one("SELECT COUNT(*) c FROM version")["c"]
        dst.close()
        src.close()
        # 闭环自检：重开副本确认 project 行在（防 WAL checkpoint 边缘场景）
        check = Store(dst_path)
        ok = check.one("SELECT id FROM project") is not None
        check.close()
        if not ok:
            raise RuntimeError("工程复制自检失败：副本中缺少 project 行，请重试")
        return {"id": new_id, "name": dst_name, "routes": n_routes,
                "versions": n_versions}

    def _pg_copy_project(self, src, project_id: str, new_name: str | None) -> dict:
        """PG 版工程复制：clone_to_new_schema + 登记目录。"""
        import uuid

        meta = src.one("SELECT * FROM project")
        if not meta:
            raise FileNotFoundError(f"工程 {project_id} 不存在或已损坏")
        new_pid = str(uuid.uuid4())
        dst_name = (new_name or "").strip() or f"{meta['name']} 副本"
        clone = src.clone_to_new_schema("copy")
        n_routes = len(clone.query("SELECT id FROM route"))
        n_versions = len(clone.query("SELECT id FROM version"))
        # clone 出来的 schema 名是 "<原id>-copy-xxxx"，主键要改成新 pid
        clone.execute("UPDATE project SET id=?, name=?", (new_pid, dst_name))
        self._register_in_catalog_direct(new_pid, dst_name)
        return {"id": new_pid, "name": dst_name, "routes": n_routes,
                "versions": n_versions}

    def _register_in_catalog_direct(self, pid: str, name: str) -> None:
        if self._store_factory is None:
            return
        try:
            cat = self._store_factory("__catalog__")
        except Exception:
            return
        try:
            cat.execute("INSERT INTO ame_catalog(project_id,name,created_at,"
                        "head_commit_id) VALUES (?,?,?,NULL)",
                        (pid, name, time.strftime("%Y-%m-%dT%H:%M:%S")))
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                cat.close()

    def delete_project(self, project_id: str) -> dict:
        """删除工程（PRJ-007：需二次确认——由 UI 层做；先移入回收站，可恢复）。

        PG 模式没有文件可移，改为 `DROP SCHEMA ... CASCADE` 并清理目录表。
        早期 PG 下这里第一步 `_project_file()` 就抛 FileNotFoundError——
        **云上根本没有"删除工程"的能力**。
        """
        if self._store_factory is not None:
            pid = _safe_project_id(project_id)
            if not self._pg_project_exists(pid):
                raise FileNotFoundError(f"工程 {pid} 不存在")
            st = self._store_factory(pid)
            try:
                st.drop_schema()
            finally:
                with contextlib.suppress(Exception):
                    st.close()
            self._pg_catalog_remove(pid)
            return {"dropped_schema": pid, "restore_hint":
                    "PostgreSQL 模式不支持回收站恢复，请事先导出工程包备份"}
        f = self._project_file(project_id)
        trash = self.data_dir / self.TRASH_DIR
        trash.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = trash / f"{f.stem}-{stamp}.db"
        self.conn_closed_guard(project_id)
        try:
            shutil.move(str(f), str(target))
        except PermissionError as e:
            # Windows 上文件被打开的连接占用时 move 会失败；给出可操作的提示，
            # 而不是让用户看到裸的 WinError 32
            raise PermissionError(
                f"工程仍被占用，无法移动（{e.__class__.__name__}）。"
                "请先在软件中切换到其他工程或关闭该工程，再重试删除。") from e
        return {"trashed_to": str(target), "restore_hint":
                f"将文件移回 projects/ 并改名为 {f.name} 即可恢复"}

    @staticmethod
    def conn_closed_guard(project_id: str):
        """提示层：删除前工程连接应已关闭（由调用方保证）。"""

    def list_trash(self) -> list[dict]:
        out = []
        for f in sorted((self.data_dir / self.TRASH_DIR).glob("*.db")):
            try:
                conn = sqlite3.connect(str(f))
                row = conn.execute("SELECT id,name,created_at FROM project LIMIT 1"
                                   ).fetchone()
                conn.close()
                out.append({"file": f.name, "id": row[0] if row else "",
                            "name": row[1] if row else f.stem,
                            "created_at": row[2] if row else ""})
            except sqlite3.Error:
                continue
        return out

    def restore_project(self, trash_filename: str) -> str:
        """从回收站恢复工程：放回 projects/ 并分配新 id 避免冲突。"""
        import uuid
        src = self.data_dir / self.TRASH_DIR / trash_filename
        if not src.exists():
            raise FileNotFoundError(f"回收站中没有 {trash_filename}")
        new_id = str(uuid.uuid4())
        shutil.copy2(src, self.data_dir / "projects" / f"{new_id}.db")
        st = Store(self.data_dir / "projects" / f"{new_id}.db")
        old_id = st.one("SELECT id FROM project")["id"]
        st.conn.execute("PRAGMA foreign_keys = OFF")
        with st.batch():
            st.execute("UPDATE project SET id=? WHERE id=?", (new_id, old_id))
            for table in ("version", "session", "structure", "circuit", "slice_set",
                          "plan", "board_outline"):
                if st._table_exists(table):
                    st.execute(f"UPDATE {table} SET project_id=? WHERE project_id=?",
                               (new_id, old_id))
        st.conn.execute("PRAGMA foreign_keys = ON")
        st.close()
        return new_id

    # ---------- 工程包（PRJ-006：导出一个文件，可跨机器导入） ----------
    def export_project(self, store: Store, out_path: str | Path):
        import zipfile
        store.conn.execute("PRAGMA wal_checkpoint(FULL)")
        meta = {
            "format": "ame-project",
            "schema_version": 1,
            "exported_at": now_iso(),
            "project": store.one("SELECT * FROM project"),
        }
        blobs = store.query("SELECT sha256,relpath FROM blob")
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(meta, ensure_ascii=False, indent=2))
            z.write(store.db_path, "project.db")
            for b in blobs:
                p = store.data_dir / b["relpath"]
                if p.exists():
                    z.write(p, f"blobs/{b['sha256']}")
        return Path(out_path)

    def import_project(self, pkg_path: str | Path, as_copy: bool = False) -> Store:
        """导入工程包（PRJ-006 / NFR-008）。

        as_copy=False（默认）：保持原工程 id，跨机迁移语义；
                               同机重复导入会因 id 冲突被拒（防误覆盖）。
        as_copy=True：分配新 id 与「(副本)」名称，用于同机再导入一份做对比。
        """
        import uuid
        import zipfile
        with zipfile.ZipFile(pkg_path) as z:
            meta = json.loads(z.read("manifest.json").decode("utf-8"))
            # 包内 id 直接参与数据库文件名拼接，必须校验（NFR-005）：
            # 未校验时 id="../../escaped" 会把 .db/-wal/-shm 写到数据目录之外
            pid = _safe_project_id(meta.get("project", {}).get("id"))
            db_path = self.data_dir / "projects" / f"{pid}.db"
            if db_path.exists() and not as_copy:
                raise FileExistsError(
                    f"工程 {pid} 已存在。可先删除/改名，或选择「导入为副本」（as_copy=True）")
            target_id = pid
            if db_path.exists() and as_copy:
                target_id = str(uuid.uuid4())
                db_path = self.data_dir / "projects" / f"{target_id}.db"
            # 防御性再校验一次最终路径确实落在 projects/ 之内
            projects_dir = (self.data_dir / "projects").resolve()
            if projects_dir not in db_path.resolve().parents:
                raise ValueError(f"工程包内的工程 id 非法：{pid!r}（NFR-005）")
            db_path.write_bytes(z.read("project.db"))
            store = Store(db_path)
            if target_id != pid:
                # 改 id：按 SQLite 惯例先关外键（PRAGMA 在事务内无效）
                store.conn.execute("PRAGMA foreign_keys = OFF")
                with store.batch():
                    store.execute("UPDATE project SET id=?, name=? WHERE id=?",
                                  (target_id, f"{meta['project'].get('name', '工程')} (副本)", pid))
                    for table in ("version", "session", "structure", "circuit",
                                  "slice_set", "plan", "board_outline"):
                        if store._table_exists(table):
                            store.execute(
                                f"UPDATE {table} SET project_id=? WHERE project_id=?",
                                (target_id, pid))
                    store.execute("DELETE FROM session")
                store.conn.execute("PRAGMA foreign_keys = ON")
            count = 0
            for name in z.namelist():
                if name.startswith("blobs/"):
                    sha = name.split("/", 1)[1]
                    data = z.read(name)
                    got = sha256_bytes(data)
                    if got != sha:
                        raise ValueError(f"包内文件哈希不符: {name}")
                    store.put_blob(data)  # 重新落盘并登记（DAT-005 完整性）
                    count += 1
        return store
