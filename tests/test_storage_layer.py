"""存储抽象层验收：协议契约 + SQL 方言转换 + 双后端等价性。

覆盖三类风险：
1. **协议漂移**：`Store`（SQLite）与 `PgStore`（PG）都实现了 8 个方法；
2. **占位符转换错误**：`_to_pg_placeholders` 必须跳过引号内的问号
   （``WHERE note='真的?'`` 之类），否则会静默改写用户数据；
3. **UPSERT 语义**：``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``。

PostgreSQL 相关用例在未安装 psycopg / 无 PG 实例时跳过，
但**纯字符串转换部分永远运行**——那是最容易写错、也最不需要数据库的部分。
"""
from __future__ import annotations

import pytest

from ame.db.pg_store import _to_pg_placeholders, sqlish
from ame.db.storage import Storage, StorageError, assert_implements


# ================================================================ 协议
def test_sqlite_store_implements_protocol(data_root):
    """SQLite Store 满足 Storage 协议（漏方法会在启动自检直接报错）。"""
    from ame.ledger.workspace import create_project
    ws = create_project(data_root, "协议")
    store = ws.store
    assert_implements(store)
    for m in ("execute", "query", "one", "batch", "put_blob", "get_blob",
              "verify_blob", "close"):
        assert callable(getattr(store, m))
    assert isinstance(store, Storage), "Store 应可被 Storage 协议识别"
    ws.store.close()


def test_assert_implements_rejects_incomplete():
    """缺方法的实现必须被拒绝（不能等到某个功能偶然被点到才炸）。"""
    class Half:
        def execute(self, sql, params=()):
            pass

    with pytest.raises(StorageError) as ei:
        assert_implements(Half())
    assert "query" in str(ei.value)


# ================================================================ 占位符转换
@pytest.mark.parametrize("src,expect", [
    ("SELECT * FROM t WHERE id=?", "SELECT * FROM t WHERE id=%s"),
    ("SELECT * FROM t WHERE a=? AND b=?", "SELECT * FROM t WHERE a=%s AND b=%s"),
    ("INSERT INTO t(a,b) VALUES(?,?)", "INSERT INTO t(a,b) VALUES(%s,%s)"),
    # 引号内的问号是数据，不是占位符
    ("SELECT * FROM t WHERE note='真的?'", "SELECT * FROM t WHERE note='真的?'"),
    ("SELECT * FROM t WHERE note='a?b' AND id=?",
     "SELECT * FROM t WHERE note='a?b' AND id=%s"),
    # SQL 里的 '' 是转义单引号，不结束字面量
    ("SELECT * FROM t WHERE note='it''s?' AND id=?",
     "SELECT * FROM t WHERE note='it''s?' AND id=%s"),
    # 双引号标识符内同理
    ('SELECT "a?b" FROM t WHERE id=?', 'SELECT "a?b" FROM t WHERE id=%s'),
    # 没有占位符时不改动
    ("SELECT 1", "SELECT 1"),
])
def test_placeholder_conversion(src, expect):
    """占位符转换必须只在**字面量之外**把 ? 换成 %s。"""
    assert _to_pg_placeholders(src) == expect


def test_placeholder_conversion_handles_utf8_and_percent():
    """含中文与 % 的语句：中文保留，% 不应被误当格式符处理。"""
    got = _to_pg_placeholders("SELECT * FROM t WHERE reason LIKE ? AND note='50%'")
    assert got == "SELECT * FROM t WHERE reason LIKE %s AND note='50%'"


# ================================================================ UPSERT
def test_insert_or_ignore_becomes_on_conflict():
    """INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING。"""
    got = sqlish("INSERT OR IGNORE INTO blob(sha256,size) VALUES (?,?)")
    assert got.startswith("INSERT INTO blob")
    assert "ON CONFLICT DO NOTHING" in got.upper()
    assert "%s" in got


def test_insert_or_replace_requires_explicit_conflict():
    """INSERT OR REPLACE 语义不明确时必须报错，不能静默改成别的行为。"""
    with pytest.raises(StorageError) as ei:
        sqlish("INSERT OR REPLACE INTO project(id,name) VALUES (?,?)")
    assert "ON CONFLICT" in str(ei.value)


def test_plain_select_untouched():
    """普通查询除占位符外不得被改写。"""
    assert sqlish("SELECT * FROM route WHERE id=?") == \
        "SELECT * FROM route WHERE id=%s"


# ================================================================ 双后端等价性
@pytest.mark.parametrize("dsn_env", ["AME_TEST_PG_DSN"])
def test_pg_backend_matches_sqlite(monkeypatch, dsn_env, tmp_path):
    """装了 psycopg 且有测试库时：同一套操作在 PG 与 SQLite 下结果一致。

    没有 PG 环境时跳过——但上面的字符串转换用例仍然覆盖了最容易错的部分。
    """
    pytest.importorskip("psycopg")
    import os

    dsn = os.environ.get(dsn_env)
    if not dsn:
        pytest.skip(f"未设置 {dsn_env}，跳过 PG 实测")

    from ame.db.pg_store import PgStore
    from ame.db.store import SCHEMA_PATH

    pg = PgStore(dsn, blobs_root=tmp_path / "blobs")
    try:
        # 建表（PG 方言：TEXT 主键、INTEGER 布尔都兼容，PRAGMA 要剔除）
        schema = SCHEMA_PATH.read_text("utf-8")
        schema = "\n".join(ln for ln in schema.splitlines()
                           if not ln.strip().upper().startswith("PRAGMA"))
        for stmt in [s.strip() for s in schema.split(";") if s.strip()]:
            if stmt.upper().startswith("CREATE"):
                pg.conn.execute(stmt)
        pg.conn.commit()

        # 写入 + 读回
        pg.execute("INSERT INTO project(id,name,created_at) VALUES (?,?,?)",
                   ("p1", "测试工程", "2026-01-01T00:00:00"))
        row = pg.one("SELECT id,name FROM project WHERE id=?", ("p1",))
        assert row == {"id": "p1", "name": "测试工程"}

        # blob 往返
        sha, rel = pg.put_blob(b"hello ame")
        assert pg.get_blob(sha) == b"hello ame"
        ok, _ = pg.verify_blob(sha)
        assert ok

        # UPSERT：重复插入同主键不报错、不覆盖
        pg.execute("INSERT OR IGNORE INTO project(id,name,created_at) VALUES (?,?,?)",
                   ("p1", "被忽略", "2026-01-02T00:00:00"))
        row = pg.one("SELECT name FROM project WHERE id=?", ("p1",))
        assert row["name"] == "测试工程", "OR IGNORE 不应覆盖已有行"
    finally:
        pg.close()


# ================================================================ 方言抽象（P0-2）
def _code_only(src: str) -> str:
    """剥掉注释与 docstring，保留字符串**内容**（SQL 就在字符串里）。"""
    import io
    import tokenize

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError):
        return src
    # 标记 docstring：模块/类/函数体开头的第一个 STRING
    docstring_idx = set()
    prev = None
    for i, tok in enumerate(toks):
        if tok.type == tokenize.STRING and prev in (tokenize.NEWLINE,
                                                    tokenize.INDENT,
                                                    tokenize.NL, None):
            docstring_idx.add(i)
        if tok.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT):
            prev = tok.type
    out: list[str] = []
    for i, tok in enumerate(toks):
        if tok.type == tokenize.COMMENT or i in docstring_idx:
            continue
        if tok.type == tokenize.STRING:
            raw = tok.string
            if raw[:1] in ("f", "F", "b", "B", "r", "R"):
                raw = raw[1:]
            for q in (chr(34) * 3, chr(39) * 3, chr(34), chr(39)):
                if raw.startswith(q) and raw.endswith(q) and len(raw) >= 2 * len(q):
                    raw = raw[len(q):-len(q)]
                    break
            out.append(f" {raw} ")
            continue
        if tok.type in (tokenize.NEWLINE, tokenize.NL):
            out.append("\n")
            continue
        if tok.type == tokenize.ENCODING:
            continue
        out.append(tok.string)
    return " ".join(out)


class TestStorageDialectMethods:
    """Storage 协议必须提供三个方言方法，业务层不得直接写 SQLite 语法。

    回归背景：`workspace.py` / `services.py` 早期直接写了 SQLite 专有语法——
    `PRAGMA data_version`、`SELECT ... FROM sqlite_master`、`ORDER BY rowid`。
    这些在 PostgreSQL 下全部报错，而它们都在主链路上：
    `mrc_state()` 走缓存时调 `_db_version()`，`board_outline()` 每个 /state
    请求都查 `sqlite_master`，`latest_slice()` 用 `rowid` 排序。
    等于云上 /state、/mrc、/slice、/agent 全部不可用。
    本机没 psycopg、PG 用例全 skip，所以长期没被发现。
    """

    def test_protocol_has_dialect_methods(self, data_root):
        """两个实现都要提供 has_table / change_token / recent_order。"""
        from ame.db.storage import Storage, assert_implements
        from ame.ledger.workspace import create_project

        ws = create_project(data_root, "方言")
        assert_implements(ws.store)          # 漏方法会在启动自检就炸
        assert isinstance(ws.store, Storage)
        for m in ("has_table", "change_token", "recent_order"):
            assert callable(getattr(ws.store, m, None)), f"缺方法 {m}"

    def test_has_table_matches_reality(self, data_root):
        from ame.ledger.workspace import create_project

        ws = create_project(data_root, "表探测")
        assert ws.store.has_table("project") is True
        assert ws.store.has_table("board_outline") is True
        assert ws.store.has_table("不存在的表") is False

    def test_change_token_changes_on_write(self, data_root):
        """写入后指纹必须变化——这是缓存失效的唯一依据。"""
        from ame.ledger.workspace import create_project

        ws = create_project(data_root, "指纹")
        t0 = ws.store.change_token()
        ws.commit("基线", author="user")
        t1 = ws.store.change_token()
        assert t1 != t0, "写入后 change_token 必须变化，否则缓存永不失效"

    def test_recent_order_is_dialect_neutral_output(self, data_root):
        """返回的子句不含 rowid 字面量？——SQLite 实现允许用 rowid，
        但业务层拿到的必须是**由实现生成的**字符串，不是业务层自己拼的。"""
        from ame.ledger.workspace import create_project

        ws = create_project(data_root, "排序")
        order = ws.store.recent_order()
        assert "DESC" in order.upper()
        assert "created_at" in order
        # 指定时间列
        order2 = ws.store.recent_order(time_col="started_at")
        assert "started_at" in order2
        # alias
        order3 = ws.store.recent_order("j", time_col="started_at")
        assert order3.startswith("j.")

    def test_business_layer_has_no_rowid_or_pragma(self):
        """业务层（workspace/services/commands/agent）不得出现 SQLite 专有标识。

        用 tokenize 剥注释与 docstring、保留字符串内容——不能只跳过以 `#`
        开头的行，否则 docstring 里的说明性提及会被误判，而真正的 SQL
        （在字符串里）反而可能被漏掉。
        """
        import re
        from pathlib import Path as _P

        root = _P(__file__).resolve().parent.parent / "ame"
        targets = ["ledger/workspace.py", "ledger/services.py",
                   "ledger/commands.py", "agent/loop.py"]
        banned = re.compile(r"\browid\b|\bsqlite_master\b|\bPRAGMA\b")
        bad = []
        for rel in targets:
            code = _code_only((root / rel).read_text("utf-8"))
            for m in banned.finditer(code):
                bad.append(f"{rel}: …{code[max(0, m.start() - 40):m.end() + 40]}…")
        assert not bad, "业务层含 SQLite 专有语法（PG 下会报错）：\n" + "\n".join(bad)
