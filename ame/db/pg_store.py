"""PostgreSQL 存储实现（华为云 RDS for PostgreSQL）。

与 `store.Store`（SQLite）实现同一套 `db/storage.py` 协议，
因此 `ledger/commands.py`、`ledger/workspace.py`、`agent/loop.py` 一行都不用改。

方言差异只有三处，全部在本文件内消化：

1. **占位符**：业务层写 ``?``（SQLite 风格），PG 要 ``%s``。
   ``_to_pg()`` 逐字符扫描替换，跳过引号内的字面量。
2. **UPSERT**：SQLite 的 ``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``；
   ``INSERT OR REPLACE`` → ``ON CONFLICT ... DO UPDATE``。按语句内容改写。
3. **布尔值**：schema 里用 INTEGER 0/1 存布尔，PG 里保持 INTEGER，
   不做 BOOLEAN 转换——这样两边读出来的值类型一致，业务层不用分支。

设计取舍：**不做 SQL 抽象层（ORM）**。理由是业务层已有 193 条手写 SQL，
引入 ORM 等于全部重写且丢失语义；改成"实现层做字符串转换"是最小改动路径。

本文件在未安装 psycopg 时不导入失败（延迟到连接时才报错），
便于在只有 SQLite 的开发机上跑测试。
"""
from __future__ import annotations

import contextlib
import hashlib
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from .storage import BlobMissing, StorageError, assert_implements

# 语句级改写规则（顺序敏感：先处理更具体的）
_RE_INSERT_OR_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.I)
_RE_INSERT_OR_REPLACE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+", re.I)
_RE_SCHEMA_PRAGMA = re.compile(r"^\s*PRAGMA\s+", re.I)


def _to_pg_placeholders(sql: str) -> str:
    """把 SQLite 的 ``?`` 占位符转成 PG 的 ``%s``。

    必须跳过字符串字面量与注释里的问号，否则 ``WHERE note='真的?'``
    会被错误替换。这里用一个小状态机而不是正则：正则处理引号嵌套
    （'' 转义、双引号标识符）容易漏。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    in_squote = in_dquote = False
    while i < n:
        ch = sql[i]
        if in_squote:
            out.append(ch)
            if ch == "'":
                # '' 是转义的单引号，不结束字面量
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if in_dquote:
            out.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if ch == "'":
            in_squote = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def sqlish(sql: str) -> str:
    """SQLite SQL → PostgreSQL SQL（占位符 + UPSERT 语法）。"""
    s = sql
    if _RE_INSERT_OR_IGNORE.match(s):
        s = _RE_INSERT_OR_IGNORE.sub("INSERT INTO ", s, count=1)
        # PG 要求 ON CONFLICT 指定目标；主键 id 是通用目标。
        # 若语句里有 (col,...) VALUES 列清单，用第一列作冲突目标最稳，
        # 但改成 ON CONFLICT DO NOTHING（不带目标）在 PG 里同样合法且更安全。
        if "on conflict" not in s.lower():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    elif _RE_INSERT_OR_REPLACE.match(s):
        # INSERT OR REPLACE 的语义是"按主键覆盖"。PG 用 ON CONFLICT (id) DO UPDATE。
        s = _RE_INSERT_OR_REPLACE.sub("INSERT INTO ", s, count=1)
        if "on conflict" not in s.lower():
            raise StorageError(
                "INSERT OR REPLACE 需要显式 ON CONFLICT 子句才能安全迁移到 PG；"
                f"请改写该语句：{sql[:120]}")
    return _to_pg_placeholders(s)


class PgStore:
    """PostgreSQL 工程库（满足 Storage 协议）。

    blob 存放位置：
      - 默认落本地卷（``blobs_root``），适合单机/CCE 挂盘；
      - 若给了 ``obs_putter/obs_getter``，则改为对象存储（OBS）。
    索引仍写在库表 ``blob`` 里，两种方式对业务层完全透明。
    """

    def __init__(self, dsn: str, blobs_root: Path | str = "ame-blobs",
                 obs_putter=None, obs_getter=None,
                 project_id: str | None = None):
        """project_id 给定时，本实例使用独立的 PG schema 隔离该工程的数据。

        为什么按工程隔离：一个 RDS 实例要承载多个工程，各工程的表名完全相同
        （project/route/pad…）。用 schema 隔离而不是加 project_id 前缀列，
        是因为业务层 193 条 SQL 都直接写表名——改成前缀列等于全部重写。
        """
        self.project_id = project_id
        try:
            import psycopg  # noqa: F401  (延迟导入：未装时给出可读错误)
        except ImportError as e:
            raise StorageError(
                "使用 PostgreSQL 存储需要安装 psycopg：pip install 'psycopg[binary]'"
            ) from e
        self.dsn = dsn
        self.blobs_root = Path(blobs_root)
        self.blobs_root.mkdir(parents=True, exist_ok=True)
        self._obs_put = obs_putter
        self._obs_get = obs_getter
        self._lock = threading.RLock()
        self._batch_depth = 0
        self.conn = psycopg.connect(dsn, autocommit=False)
        # psycopg3 的 row_factory 是 **cursor** 属性，Connection 上没有
        # （psycopg2 才有 conn.row_factory）。直接赋值 `self.conn.row_factory = None`
        # 在 psycopg3 下是 AttributeError——会在这里就崩，PgStore 连构造都过不去。
        # 用 try 兼容两代驱动：psycopg3 默认返回元组，本来就等价于"无 row_factory"。
        with contextlib.suppress(Exception):
            self.conn.row_factory = None
        # psycopg3 的 dict 行由 _run() 用 cur.description 自行组装（见 query/one）
        if self.project_id:
            # schema 名只允许 [a-z0-9_]，避免标识符注入（工程 id 是 uuid，
            # 去掉连字符即满足）
            schema = "ame_" + re.sub(r"[^a-z0-9]", "", self.project_id.lower())
            with self.conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                cur.execute(f'SET search_path TO "{schema}", public')
            self.conn.commit()
            self._schema = schema
            self._bootstrap_schema()
        assert_implements(self)

    def _bootstrap_schema(self) -> None:
        """在工程 schema 内建表（幂等）。

        用 **schema_pg.sql** 而不是 schema.sql。后者是 SQLite 版，有两处 PG 不兼容：

        1. 注释紧贴语句：原实现按行累积到 `;` 再检查 `body.startswith("--")`，
           于是"注释行 + CREATE 语句"整块被当成注释跳过。实测 `project` /
           `board_outline` / `keepout` 三张表和末尾 8 个索引全部丢失——
           `project` 没建出来，而 `session` / `plan` / `version` 都
           `REFERENCES project(id)`，**建表第一步就失败，任何工程都建不出来**。
           更隐蔽的是这不会在开发机暴露：本机没 psycopg，PG 用例全 skip。
        2. 前向引用：SQLite 允许 FK 指向后建的表，PG 不允许。schema.sql 里
           `session→version`、`pad→net`、`process_params→material_profile`
           都是前向引用。

        schema_pg.sql 由脚本从 schema.sql 抽取、按依赖拓扑排序、`REAL` 改
        `DOUBLE PRECISION`（PG 的 REAL 是 float4，存几何量会丢精度导致
        G-code 哈希跨后端不一致）。两份 DDL 的一致性由
        `tests/test_pg_dialect.py` 的 DDL 契约用例保证。
        """
        # `__catalog__` 是 DataRoot 的全局目录（工程分散在各自 schema，
        # 需要一个公共位置放工程列表）。它也是一次性元数据表，与其他工程表无关。
        if self.project_id == "__catalog__":
            try:
                with self.conn.cursor() as cur:
                    cur.execute('CREATE SCHEMA IF NOT EXISTS "ame_catalog"')
                    cur.execute('SET search_path TO "ame_catalog", public')
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS ame_catalog ("
                        "project_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                        "created_at TEXT NOT NULL, head_commit_id TEXT)")
                self.conn.commit()
                self._schema = "ame_catalog"
            except Exception as exc:
                raise StorageError(
                    f"PostgreSQL 目录表初始化失败（{type(exc).__name__}）：{exc}"
                ) from exc
            return
        ddl_path = Path(__file__).with_name("schema_pg.sql")
        sql = ddl_path.read_text("utf-8")
        # 剥掉整行注释后再按 ; 切分：不能反过来（否则注释会把语句一起吞掉）
        body = "\n".join(ln for ln in sql.splitlines()
                         if not ln.lstrip().startswith("--"))
        stmts = [s.strip() for s in body.split(";") if s.strip()]
        try:
            with self.conn.cursor() as cur:
                for s in stmts:
                    cur.execute(s)
            self.conn.commit()
        except Exception as exc:
            # 建表失败必须给可读中文，而不是让后续 SQL 报 relation does not exist
            raise StorageError(
                f"PostgreSQL 建表失败（{type(exc).__name__}）：{exc}。"
                "请确认 RDS 实例可用、当前用户对 search_path 中的 schema 有 CREATE 权限"
            ) from exc

    # ---------------------------------------------------------------- 基础
    def _cur(self):
        return self.conn.cursor()

    def _run(self, sql: str, params=()) -> Any:
        sql = sqlish(sql)
        cur = self._cur()
        cur.execute(sql, tuple(params))
        return cur

    def execute(self, sql: str, params=()) -> Any:
        with self._lock:
            cur = self._run(sql, params)
            if self._batch_depth == 0:
                self.conn.commit()
            return cur

    @contextlib.contextmanager
    def batch(self):
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
            cur = self._run(sql, params)
            cols = [d[0] for d in (cur.description or [])]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        with self._lock:
            cur = self._run(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in (cur.description or [])]
            return dict(zip(cols, row, strict=True))

    # ---- Storage 协议的方言方法 ----
    # INSERT ... SELECT 的顺序必须满足 FK 依赖（PG 立即检查）。
    # 这份清单取自 schema_pg.sql 的拓扑序——早期手写清单把 pad 排在 net 之前、
    # process_params 排在 material_profile 之前，PG 下会报外键错误（SQLite 同样会，
    # 实测 IntegrityError）。
    # session 刻意不拷：副本会插自己的 session 行，拷过来会留下 status='open'
    # 的幽灵行（与 SQLite 版 copy_project 显式 DELETE FROM session 一致）。
    _CLONE_TABLES = (
        "project", "version", "structure", "feature", "board_outline", "circuit",
        "net", "comp", "pad", "route", "keepout", "jumper", "material_profile",
        "process_params", "slice_set", "layer", "path", "device", "print_job",
        "defect", "mrc_rule", "mrc_issue", "tool_geometry", "blob", "plan",
    )

    def clone_to_new_schema(self, tag: str = "clone"):
        """复制当前 schema 的结构与数据到一个新 schema（重放用，AGT-008）。

        返回一个新的 `PgStore` 实例，指向独立 schema，与原实例互不影响。

        早期实现有三个缺陷（交叉验证查出）：

        1. `obs_putter=self._obs_putter` —— 属性名写错，构造函数存的是
           `_obs_put`。**AGT-008 重放（P0 验收项）在 RDS 上 100% AttributeError。**
           本机没有 psycopg、PG 用例全 skip，所以一直没暴露。
        2. 表插入顺序不满足外键依赖（pad 在 net 之前等），PG 会报外键错误。
        3. 没有回收旧 schema，每次重放都留一个 schema 常驻 RDS。
        """
        new_pid = f"{self.project_id}-{tag}-{uuid.uuid4().hex[:8]}"
        clone = PgStore(self.dsn, blobs_root=self.blobs_root, project_id=new_pid,
                        obs_putter=self._obs_put, obs_getter=self._obs_get)
        src, dst = self._schema, clone._schema
        try:
            with self.conn.cursor() as cur:
                for table in self._CLONE_TABLES:
                    cur.execute(f'INSERT INTO "{dst}".{table} '
                                f'SELECT * FROM "{src}".{table}')
            self.conn.commit()
        except Exception as exc:
            # 复制失败要清理半成品 schema，否则重试会不断累积
            with contextlib.suppress(Exception):
                with clone.conn.cursor() as cur:
                    cur.execute(f'DROP SCHEMA IF EXISTS "{dst}" CASCADE')
                clone.conn.commit()
            raise StorageError(
                f"复制工程 schema 失败（{type(exc).__name__}）：{exc}") from exc
        return clone

    def drop_schema(self) -> None:
        """删除本实例对应的 schema（含全部数据）。工程删除 / 克隆清理用。"""
        if not self._schema:
            return
        with contextlib.suppress(Exception):
            self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        self.conn.commit()

    def has_table(self, name: str) -> bool:
        """PG 没有 sqlite_master：查 information_schema（按当前 search_path 的 schema）。"""
        row = self.one(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?", (name,))
        return bool(row)

    def change_token(self) -> tuple:
        """库变更指纹，用于缓存失效判定。

        **不能用 `txid_current()`**：它在调用所在的当前事务里分配 XID（必要时
        推进），而 `Workspace.mrc_state()` 每次请求会调两次 → 两个不同事务
        → token 永不相等 → **MRC 与 STEP 解析缓存 100% miss**。功能正确
        （宁可多算），但云上 1800 面模型每次重绘都重解析。

        改用两条与"数据是否真的变了"相关的指标：
          - 本 schema 的行数总和（便宜、精确反映增删）
          - `pg_stat_user_tables` 的累计更新计数（反映 UPDATE）
        两者都来自统计视图，有异步刷新延迟（默认亚秒级）。为了不返回过期视图，
        这里**额外把 `pg_current_xact_id_if_assigned()` 排除在外**，改为用
        `pg_class.relfrozenxid` 无关；实际做法是接受"延迟 ≤ 一次 autovacuum 周期"，
        并在 `Workspace` 侧保证：写入路径主动 `invalidate()`，不依赖 token。
        """
        row = self.one(
            "SELECT COALESCE(SUM(n_tup_ins + n_tup_upd + n_tup_del), 0) AS n "
            "FROM pg_stat_user_tables WHERE schemaname = current_schema()")
        n = int(row["n"]) if row else 0
        # 行数从 pg_class 读（实时，不受统计延迟影响）：
        # reltuples 是估算值，这里用精确 count 的代价太高，只在表数少时可用。
        # 折中：把 relpages 一起带上——DDL/大量插入会改变它。
        pages = self.one(
            "SELECT COALESCE(SUM(relpages), 0) AS p FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind = 'r'")
        return (n, int(pages["p"]) if pages else 0)

    def recent_order(self, alias: str = "", time_col: str = "created_at") -> str:
        """PG 没有 rowid 伪列：用 ctid（物理行标识）做稳定次级键。

        ctid 会随 UPDATE/VACUUM 变化，但订单场景只要求"同一时刻排序稳定"，
        不要求跨事务不变——同一秒内多行的相对顺序能定住即可。
        """
        p = f"{alias}." if alias else ""
        # **必须显式 NULLS LAST**：PG 的 `DESC` 默认 NULLS FIRST（与 SQLite 相反，
        # SQLite 的 DESC 把 NULL 排最后）。不写的话，`print_job.started_at`
        # 为 NULL 的未开始作业会被排到最前——`prune_jobs` 按时间倒序截断时
        # 会删掉真正最新的作业、留下 NULL 行，属于"数据默默被裁错"。
        return f"{p}{time_col} DESC NULLS LAST, {p}ctid DESC"


    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- blob
    def _rel(self, sha: str) -> str:
        return f"{sha[:2]}/{sha[2:4]}/{sha}"

    def put_blob(self, data: bytes) -> tuple[str, str]:
        h = hashlib.sha256(data).hexdigest()
        rel = self._rel(h)
        if self._obs_put is not None:
            self._obs_put(rel, data)
        else:
            p = self.blobs_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                p.write_bytes(data)
        self.execute(
            "INSERT OR IGNORE INTO blob(sha256,size,relpath) VALUES (?,?,?)",
            (h, len(data), rel))
        return h, rel

    def get_blob(self, sha: str) -> bytes:
        row = self.one("SELECT relpath FROM blob WHERE sha256=?", (sha,))
        if not row:
            raise BlobMissing(f"blob 不存在: {sha[:12]}")
        rel = row["relpath"]
        if self._obs_get is not None:
            return self._obs_get(rel)
        p = self.blobs_root / rel
        if not p.exists():
            raise BlobMissing(f"文件缺失：{rel}")
        return p.read_bytes()

    def verify_blob(self, sha: str) -> tuple[bool, str]:
        try:
            data = self.get_blob(sha)
        except BlobMissing as e:
            return False, str(e)
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            return False, (f"内容与记录不符（记录 {sha[:12]}…，实际 {got[:12]}…），"
                           "文件可能被外部程序修改过")
        return True, "一致"

    def blob_path(self, rel_or_sha: str) -> Path:
        return self.blobs_root / rel_or_sha


__all__ = ["PgStore", "sqlish", "_to_pg_placeholders"]
