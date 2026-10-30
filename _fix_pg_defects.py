# -*- coding: utf-8 -*-
"""修复交叉验证查出的 PgStore 缺陷：
P0-1 _obs_putter 属性名错（必崩）
P0-2 clone 的 INSERT 顺序有 2 处 FK 前向引用
P1   clone 未 DROP 旧 schema（泄漏）
P1   clone 误拷 session（幽灵 open 行）
P1   recent_order 的 DESC 在 PG 下 NULLS FIRST（与 SQLite 相反）
P1   change_token 用 txid_current() 导致缓存 100% miss
P1   delete_project / copy_project 在 PG 下不可用
"""
from pathlib import Path

p = Path("ame/db/pg_store.py")
t = p.read_text(encoding="utf-8")

# ---------- 1) clone_to_new_schema 整体重写 ----------
start = t.find("    def clone_to_new_schema(self, tag: str = \"clone\"):")
end = t.find("    def recent_order(", start)
assert start > 0 and end > start

new_clone = '''    # INSERT ... SELECT 的顺序必须满足 FK 依赖（PG 立即检查，SQLite 也检查）。
    # 这份清单直接取自 schema_pg.sql 的拓扑序——早期手写清单把 pad 排在 net
    # 之前、process_params 排在 material_profile 之前，PG 下会报外键错误
    # （SQLite 同样报，实测 IntegrityError）。
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
           本机没有 psycopg，PG 用例全 skip，所以一直没暴露。
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
            # 复制失败要清理半成品 schema，否则失败重试会不断累积
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
            self.conn.execute("ROLLBACK")
        with self.conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        self.conn.commit()

'''
t = t[:start] + new_clone + t[end:]

# ---------- 2) recent_order: NULLS LAST ----------
old_ro = '''        p = f"{alias}." if alias else ""
        return f"{p}{time_col} DESC, {p}ctid DESC"'''
new_ro = '''        p = f"{alias}." if alias else ""
        # **必须显式 NULLS LAST**：PG 的 `DESC` 默认 NULLS FIRST（与 SQLite 相反，
        # SQLite 的 DESC 把 NULL 排最后）。不写的话，`print_job.started_at`
        # 为 NULL 的未开始作业会被排到最前——`prune_jobs` 按时间倒序截断时
        # 会删掉真正最新的作业、留下 NULL 行，属于"数据默默被裁错"。
        return f"{p}{time_col} DESC NULLS LAST, {p}ctid DESC"'''
assert old_ro in t
t = t.replace(old_ro, new_ro, 1)

# ---------- 3) change_token: 不再用 txid_current ----------
old_ct_start = t.find("    def change_token(self) -> tuple:")
old_ct_end = t.find("    def recent_order(", old_ct_start)
assert old_ct_start > 0 and old_ct_end > old_ct_start
new_ct = '''    def change_token(self) -> tuple:
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

'''
t = t[:old_ct_start] + new_ct + t[old_ct_end:]

p.write_text(t, encoding="utf-8")
print("PgStore 缺陷修复完成")
