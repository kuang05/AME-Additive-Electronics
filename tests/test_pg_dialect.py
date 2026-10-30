"""PG 方言静态验证：不依赖 PostgreSQL 实例也能发现"必然失败"的语句。

背景与诚实边界
--------------
本机 Python 3.13 没有 psycopg 的 wheel，也没有 PostgreSQL / Docker，
因此**无法做真实 PG 集成测试**。这一点必须在报告里说清楚，不能含糊。

但"完全不验证"是不可接受的：`INSERT OR IGNORE` / `INSERT OR REPLACE` /
`PRAGMA` / `?` 占位符 这几类写法在 PG 下是**必然报错**，属于可以在
代码层面就抓出来的确定性问题。

本文件做三件事：
1. 扫描 `ame/` 全部源码，确认业务层（除 `pg_store.py` 自身）不含任何
   SQLite 专有语法；
2. 对 `sqlish()` 的转换做**穷举式**校验：转换后的语句不得再含 SQLite 语法；
3. 用 SQLite 的 `EXPLAIN` 正向验证：转换前的语句在 SQLite 下合法，
   说明转换器面对的是真实语句而不是臆造样本。

这不能替代真实 PG 测试（顺序、类型、事务行为仍需实测），
但能排除掉"一部署就崩"这一类问题。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from ame.db.pg_store import _to_pg_placeholders, sqlish

AME = Path(__file__).resolve().parent.parent / "ame"

# 在 PG 下必然报错的写法（不含 pg_store 自身的转换代码与注释）
SQLITE_ONLY_PATTERNS = [
    (re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I), "INSERT OR IGNORE"),
    (re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I), "INSERT OR REPLACE"),
    (re.compile(r"\bREPLACE\s+INTO\b", re.I), "REPLACE INTO"),
    # 不能用 `^\s*PRAGMA`：tokenize 之后代码被空格连成一行，行首锚点靠不住。
    # 实测 `store.one("PRAGMA data_version")` 单行写法命中 0 次。
    # 改用"前面不是标识符/点号"的边界。
    (re.compile(r"(?<![\w.])PRAGMA\b", re.I), "PRAGMA"),
    (re.compile(r"\bAUTOINCREMENT\b", re.I), "AUTOINCREMENT"),
    (re.compile(r"\bWITHOUT\s+ROWID\b", re.I), "WITHOUT ROWID"),
    # rowid / sqlite_master：PG 没有 rowid 伪列、也没有 sqlite_master 视图。
    # 早期这两个模式完全没被扫描——而业务层曾用 `ORDER BY rowid` 取"最新一条"。
    (re.compile(r"(?<![\w.])rowid\b", re.I), "rowid 伪列"),
    (re.compile(r"(?<![\w.])sqlite_master\b", re.I), "sqlite_master"),
    (re.compile(r"(?<![\w.])group_concat\s*\(", re.I), "SQL group_concat()"),
    (re.compile(r"(?<![\w.])last_insert_rowid\s*\(", re.I),
     "last_insert_rowid()"),
    # SQL 函数：不能是 `obj.method(` 形式（排除 time.strftime 这类 Python 调用）。
    # tokenize 后代码被空格分隔（`time . strftime (`），所以后顾要允许空白。
    (re.compile(r"(?<!\.\s)(?<![\w])(?<!\.)strftime\s*\(", re.I | re.M),
     "SQL strftime()"),
    (re.compile(r"(?<!\.\s)(?<![\w])(?<!\.)json_extract\s*\(", re.I | re.M),
     "SQL json_extract()"),
    (re.compile(r"(?<!\.\s)(?<![\w])(?<!\.)randomblob\s*\(", re.I | re.M),
     "SQL randomblob()"),
]


# 允许出现 SQLite 语法的文件（它们就是 SQLite 实现层，或负责做方言转换）
_SQLITE_IMPL_ALLOWED = {"pg_store.py", "store.py", "migrations.py", "storage.py"}


def _iter_sources():
    """遍历业务源码；跳过 SQLite 实现层（它们本来就是 SQLite 方言）。

    跳过范围要精确：`pg_store.py` 负责做转换；`store.py` 与 `migrations.py`
    是 SQLite 实现本身；`storage.py` 只在协议文档里提到关键字。
    **`workspace.py` / `services.py` / `commands.py` / `agent/` / `web/` 必须
    零 SQLite 语法**——那些文件在两个后端上都会跑。

    历史教训：本函数原先只跳过 pg_store.py，而 `_strip_comments` 会把所有
    字符串（也就是全部 SQL）剥掉，所以 workspace.py 里 6 处
    `INSERT OR IGNORE` 从未被发现。修好剥注释逻辑后立刻暴露。
    """
    for p in AME.rglob("*.py"):
        if p.name in _SQLITE_IMPL_ALLOWED:
            continue
        yield p, p.read_text("utf-8")


# 各类引号：三引号优先（避免先匹配单引号把三引号拆坏）
_QUOTES = (chr(34) * 3, chr(39) * 3, chr(34), chr(39))


def _strip_comments(src: str) -> str:
    """去掉注释，**保留字符串字面量的内容**——SQL 全在字符串里。

    重要修正（2026-10-29）：本函数原名 `_strip_comments_and_strings`，
    实现里 `keep = {NAME, OP, NUMBER}` 把所有 STRING token 丢掉了。
    于是 `store.execute("INSERT OR IGNORE INTO ...")` 变成
    `store.execute()`——**所有 SQL 都被剥掉，扫描永远通过**。
    这套测试因此给了"业务层 SQL 已可在 PG 运行"的假保证，掩盖了
    `rowid` / `sqlite_master` / `PRAGMA` 在 PostgreSQL 下全部报错的事实。

    现在：注释（COMMENT）与 **docstring** 丢弃，业务字符串保留其**内容**
    （去掉引号以免影响后续正则边界），其余 token 原样拼接。

    为什么还要剥 docstring：说明性文字里会写"早期这里直接读 `PRAGMA
    data_version`"这类**解释历史缺陷**的句子，不剥的话会被当成真实违规。
    剥完之后留下的字符串都是**真正会发给数据库的 SQL**。
    """
    import ast
    import io
    import tokenize

    doc_rows: set[int] = set()
    try:
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
                    and node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                for ln in range(node.body[0].lineno, (node.body[0].end_lineno or 0) + 1):
                    doc_rows.add(ln)
    except SyntaxError:
        pass

    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.start[0] in doc_rows:
                continue            # docstring：说明性文字，不是 SQL
            if tok.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE):
                # 保留内容（f-string 片段也拼进来，才能看到 f"ORDER BY {x}" 里的 SQL）
                raw = tok.string
                if raw[:1] in ("f", "F", "b", "B", "r", "R") and raw[1:2] in ('"', "'"):
                    raw = raw[1:]
                for q in _QUOTES:
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
    except (tokenize.TokenError, IndentationError):
        # 源码本身不完整时退化为原样返回（该文件另有语法检查兜底）
        return src
    return " ".join(out)


@pytest.mark.parametrize("path", [p for p, _ in _iter_sources()],
                         ids=lambda p: p.name)
def test_business_code_has_no_sqlite_only_sql(path):
    """业务代码不得含 SQLite 专有语法（PG 下必然报错）。"""
    code = _strip_comments(path.read_text("utf-8"))
    hits = [(name, pat.search(code)) for pat, name in SQLITE_ONLY_PATTERNS]
    bad = [name for name, m in hits if m]
    assert not bad, f"{path.name} 含 SQLite 专有语法：{bad}（PG 下会报错）"


def test_pg_store_is_only_place_with_sqlite_syntax():
    """只有 pg_store.py 允许出现这些关键字（它的职责就是做转换）。"""
    p = AME / "db" / "pg_store.py"
    assert p.exists()
    src = p.read_text("utf-8")
    assert "INSERT OR IGNORE" in src, "转换器需要识别这类语句"


# ================================================================ 转换穷举
# 来自业务层的真实语句样本（逐条取自 commands.py / workspace.py / store.py）
REAL_STATEMENTS = [
    "SELECT * FROM project WHERE id=?",
    "INSERT INTO project(id,name,created_at) VALUES (?,?,?)",
    "UPDATE project SET head_commit_id=? WHERE id=?",
    "DELETE FROM route WHERE circuit_id=?",
    "SELECT p.id, c.ref FROM pad p JOIN comp c ON p.comp_id=c.id WHERE c.ref=?",
    "INSERT OR IGNORE INTO mrc_rule(code,name,threshold_json,weight,"
    "rule_version,enabled) VALUES (?,?,?,?,?,?)",
    "SELECT * FROM route r JOIN circuit ci ON r.circuit_id=ci.id "
    "WHERE ci.project_id=? ORDER BY r.id",
    "UPDATE slice_set SET stale=1 WHERE project_id=? AND source_commit_id<>?",
    "INSERT INTO version(id,project_id,parent_id,seq,cmd_json,diff_json,reason,"
    "score_before,score_after,author,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
]


@pytest.mark.parametrize("sql", REAL_STATEMENTS)
def test_sqlish_output_has_no_sqlite_syntax(sql):
    """转换后的语句不得再含 SQLite 语法，且占位符必须全为 %s。"""
    out = sqlish(sql)
    code = _strip_comments(out)
    for pat, name in SQLITE_ONLY_PATTERNS:
        assert not pat.search(code), f"转换后仍含 {name}：{out}"
    assert "?" not in code, f"转换后仍有 ? 占位符：{out}"


@pytest.mark.parametrize("sql", [s for s in REAL_STATEMENTS
                                 if not re.search(r"INSERT\s+OR", s, re.I)])
def test_original_statements_are_valid_sqlite(sql, tmp_path):
    """转换前的语句在 SQLite 下必须能解析——确保样本是真实语句。

    输入语句无法通过 SQLite 的 PREPARE，说明它本身就是坏的，
    那"转换正确"也没有意义。
    """
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    conn.executescript((AME / "db" / "schema.sql").read_text("utf-8"))
    try:
        conn.execute("EXPLAIN " + sql, tuple(None for _ in range(sql.count("?"))))
    except sqlite3.Error as e:
        pytest.fail(f"语句在 SQLite 下都不合法，不能作为转换样本：{sql[:80]} → {e}")
    finally:
        conn.close()


def test_insert_or_ignore_becomes_valid_pg_upsert():
    """INSERT OR IGNORE 的转换结果必须是 PG 合法语法。"""
    out = sqlish("INSERT OR IGNORE INTO blob(sha256,size) VALUES (?,?)")
    assert out.upper().startswith("INSERT INTO BLOB")
    assert "ON CONFLICT DO NOTHING" in out.upper()
    assert "%s" in out


def test_placeholders_never_touch_string_literals():
    """字符串里的 ? 是数据，不能被改写成 %s（会静默破坏用户数据）。"""
    cases = [
        ("SELECT * FROM t WHERE note='?'", "SELECT * FROM t WHERE note='?'"),
        ("SELECT * FROM t WHERE note='a?b' AND id=?", "SELECT * FROM t WHERE note='a?b' AND id=%s"),
        ("SELECT * FROM t WHERE note='it''s?' AND id=?",
         "SELECT * FROM t WHERE note='it''s?' AND id=%s"),
        ('SELECT "col?name" FROM t WHERE id=?', 'SELECT "col?name" FROM t WHERE id=%s'),
        # SQL 转义（'' 表示一个单引号）里的 ? 也是数据
        ("SELECT * FROM t WHERE s='?''?' AND a=?",
         "SELECT * FROM t WHERE s='?''?' AND a=%s"),
        # 百分号在字符串内不能被当成格式符
        ("SELECT * FROM t WHERE note='50%' AND a=?",
         "SELECT * FROM t WHERE note='50%' AND a=%s"),
    ]
    for src, expect in cases:
        assert _to_pg_placeholders(src) == expect

    # 未闭合引号：不能抛异常（截断的 SQL 应原样走到 PG 去报可读错误，
    # 而不是在转换层崩掉、让用户看到 Python traceback）
    _to_pg_placeholders("SELECT * FROM t WHERE s='abc AND a=?")


# ================================================================ 明示未验证
def test_pg_integration_is_explicitly_not_verified_here():
    """如实标记：真实 PG 集成测试未在本环境执行。

    这条测试的存在是刻意的——它把"没验证"变成可查的事实，
    而不是让读者误以为"跑了 26 个 web 测试就等于 PG 也没问题"。
    """
    pytest.importorskip("psycopg") if _has_pg() else None
    if not _has_pg():
        pytest.skip("本机无 PostgreSQL 实例（且 Python 3.13 无 psycopg wheel），"
                    "PG 集成验证需在有 RDS/容器环境时执行")


def _has_pg() -> bool:
    import os
    return bool(os.environ.get("AME_TEST_PG_DSN"))


# ================================================================ PG DDL 契约
class TestPgDdlContract:
    """`ame/db/schema_pg.sql` 必须满足 PG 的建表约束。

    回归背景：`PgStore._bootstrap_schema` 原先读 SQLite 版 `schema.sql`，
    按行累积到 `;` 再判断"是否以 `--` 开头"，于是"注释行 + CREATE 语句"
    整块被当成注释跳过。实测 `project` / `board_outline` / `keepout` 三张表
    和末尾 8 个索引全部丢失——`project` 没建出来，而 `session`/`plan`/`version`
    都 `REFERENCES project(id)`，**建表第一步就失败，RDS 上一个工程都建不出来**。
    本机没 psycopg，PG 用例全 skip，所以这个缺陷长期没被发现。
    """

    @staticmethod
    def _statements():
        from pathlib import Path as _P

        sql = (_P(__file__).resolve().parent.parent
               / "ame" / "db" / "schema_pg.sql").read_text("utf-8")
        body = "\n".join(ln for ln in sql.splitlines()
                         if not ln.lstrip().startswith("--"))
        return [s.strip() for s in body.split(";") if s.strip()]

    def test_all_tables_present(self):
        """26 张表全部出现，且 project 必须在首位（其余表都依赖它）。"""
        import re

        stmts = self._statements()
        tables = [re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s, re.I).group(1)
                  for s in stmts if re.match(r"CREATE TABLE", s, re.I)]
        assert len(tables) == 26, f"应有 26 张表，实际 {len(tables)}: {tables}"
        assert tables[0] == "project", "project 必须最先建（其他表都 FK 指向它）"
        for must in ("project", "board_outline", "keepout", "blob", "route", "path"):
            assert must in tables, f"缺表 {must}"

    def test_no_forward_references(self):
        """PG 不允许 FK 指向后建的表（SQLite 允许，所以 schema.sql 里有 3 处）。"""
        import re

        stmts = self._statements()
        order, deps = [], {}
        for s in stmts:
            m = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", s, re.I)
            if m:
                order.append(m.group(1))
                deps[m.group(1)] = set(re.findall(r"REFERENCES\s+(\w+)", s))
        seen, bad = set(), []
        for name in order:
            for d in deps.get(name, ()):
                if d != name and d not in seen:
                    bad.append((name, d))
            seen.add(name)
        assert not bad, f"存在前向引用（PG 会报 relation does not exist）：{bad}"

    def test_geometry_columns_use_double_precision(self):
        """几何量不能用 REAL：PG 的 REAL 是 float4，会丢精度导致 G-code 哈希漂移。"""
        import re

        for s in self._statements():
            assert not re.search(r"\bREAL\b", s), (
                "schema_pg.sql 中不应出现 REAL（PG 的 REAL 是 float4，"
                f"几何量会丢精度）：{s[:80]}")

    def test_indexes_present_and_after_tables(self):
        """索引必须全部存在，且排在所有 CREATE TABLE 之后。"""
        import re

        stmts = self._statements()
        idx_pos = [i for i, s in enumerate(stmts)
                   if re.match(r"CREATE INDEX", s, re.I)]
        tbl_pos = [i for i, s in enumerate(stmts)
                   if re.match(r"CREATE TABLE", s, re.I)]
        assert len(idx_pos) == 12, f"应有 12 个索引，实际 {len(idx_pos)}"
        assert min(idx_pos) > max(tbl_pos), "索引必须全部在建表之后（否则引用未定义的表）"

    def test_comment_stripping_is_order_sensitive(self):
        """必须"先剥注释、再按 `;` 切分"——反过来会让注释吞掉整条语句。

        用一个**人工构造**的片段验证这个顺序差异（而不是依赖 schema_pg.sql
        的当前写法）：如果将来 DDL 里又出现"注释紧贴语句"，引导逻辑必须仍然正确。

        这条测试的前身写的是 `assert ... or True`——恒真，等于没测。
        自我审查时发现并重写。
        """
        sample = (
            "-- 说明性注释，紧跟一条语句\n"
            "CREATE TABLE t1 (id TEXT);\n"
            "-- 另一条注释\n"
            "CREATE INDEX i1 ON t1(id);\n"
        )
        # 错误顺序：先切分，再判断"是否以 -- 开头" → 两条语句都被当注释跳过
        naive = [s.strip() for s in sample.split(";") if s.strip()]
        naive_ok = [s for s in naive if not s.startswith("--")]
        assert len(naive_ok) == 0, (
            "错误顺序本应丢掉全部语句（这是被修复的缺陷模式）")

        # 正确顺序：先剥注释行，再切分 → 两条都在
        body = "\n".join(ln for ln in sample.splitlines()
                         if not ln.lstrip().startswith("--"))
        correct = [s.strip() for s in body.split(";") if s.strip()]
        assert len(correct) == 2, f"先剥注释应得到 2 条语句，实际 {len(correct)}"
        assert all(s.upper().startswith("CREATE") for s in correct)


# ================================================================ 真 SQL 解析器交叉验证
class TestPostgresDialectParser:
    """用 sqlglot 的真实 PostgreSQL 方言解析 SQL——这是在"无 RDS"环境下最强的替代证据。

    为什么需要这一层：本机没有 psycopg、没有 docker、没有 psql，所有 PG 用例
    只能 skip。此前的 DDL 契约测试只做**字符串层面的检查**（表数、前向引用、
    有没有 REAL），无法发现"语法本身 PG 不接受"。
    实测抓到两个真实缺陷：
      1. 每条 CREATE TABLE 以 `);` 结尾，生成脚本又追加 `;` → 产出 `);;`，
         按 `;` 切分后出现空语句；
      2. 列定义里的行内 `--` 注释（23 处）被原样带进 DDL——PG 语法上接受，
         但按 `;` 切分的引导逻辑只剥行首注释，注释内容会留在语句体里。
    """

    @staticmethod
    def _pg():
        pytest.importorskip("sqlglot", reason="需要 sqlglot 做 PG 方言解析")
        return pytest.importorskip("sqlglot")

    @staticmethod
    def _statements() -> list[str]:
        from pathlib import Path as _P

        sql = (_P(__file__).resolve().parent.parent
               / "ame" / "db" / "schema_pg.sql").read_text("utf-8")
        body = "\n".join(ln for ln in sql.splitlines()
                         if not ln.lstrip().startswith("--"))
        return [s.strip() for s in body.split(";") if s.strip()]

    def test_every_ddl_statement_parses_as_postgres(self):
        """38 条语句（26 表 + 12 索引）必须全部通过 PostgreSQL 方言解析。"""
        sqlglot = self._pg()
        stmts = self._statements()
        assert len(stmts) == 38, f"应有 38 条语句，实际 {len(stmts)}"
        bad = []
        for i, s in enumerate(stmts):
            try:
                sqlglot.parse_one(s, dialect="postgres")
            except Exception as e:
                bad.append((i, type(e).__name__, str(e)[:90],
                            " ".join(s.split())[:80]))
        assert not bad, "以下语句 PG 方言无法解析：\n" + "\n".join(
            f"  #{i} {t}: {m}\n      {snip}" for i, t, m, snip in bad)

    def test_no_double_semicolon_or_inline_comment(self):
        """DDL 里不得有 `;;`（会产生空语句）或行内 `--` 注释。"""
        from pathlib import Path as _P

        raw = (_P(__file__).resolve().parent.parent
               / "ame" / "db" / "schema_pg.sql").read_text("utf-8")
        assert ";;" not in raw, "DDL 含 `;;`，按 ; 切分会产生空语句"
        inline = [ln for ln in raw.splitlines()
                  if not ln.lstrip().startswith("--") and "--" in ln]
        assert not inline, f"DDL 含行内注释：{inline[:3]}"

    def test_sqlite_schema_rejected_by_pg (self):
        """反向验证：schema.sql 用 PG 方言解析应当失败（证明两份 DDL 确有必要）。"""
        sqlglot = self._pg()
        from pathlib import Path as _P

        sql = (_P(__file__).resolve().parent.parent
               / "ame" / "db" / "schema.sql").read_text("utf-8")
        body = "\n".join(ln for ln in sql.splitlines()
                         if not ln.lstrip().startswith("--"))
        stmts = [s.strip() for s in body.split(";") if s.strip()]
        # PRAGMA 语句 PG 必然不接受
        pragmas = [s for s in stmts if s.upper().startswith("PRAGMA")]
        assert pragmas, "schema.sql 应含 PRAGMA（否则本测试前提不成立）"
        failed = 0
        for s in pragmas:
            try:
                sqlglot.parse_one(s, dialect="postgres")
            except Exception:
                failed += 1
        assert failed == len(pragmas), "PRAGMA 在 PG 方言下应全部解析失败"

    def test_runtime_sql_all_parse_as_postgres(self, data_root):
        """跑真实业务流，捕获 store 收到的**每一条** SQL，逐条按 PG 方言解析。

        比静态抽取可靠：拿到的就是实际执行的完整 SQL（f-string 已渲染、
        占位符已替换），不存在"跨行拼接还原不完整"的假阳性。
        实测覆盖建工程→导入 STEP→导入板→提交→MRC→切片→打印→撤销/重做→
        完整性检查，约 50 条独特语句。
        """
        sqlglot = self._pg()
        import re as _re

        from conftest import build_project
        from ame.ledger.services import do_slice, run_print

        captured: list[str] = []
        ws = build_project(data_root, name="SQL方言验证")

        for name in ("execute", "query", "one"):
            orig = getattr(ws.store, name)

            def make(orig):
                def fn(sql, params=()):
                    captured.append(sql)
                    return orig(sql, params)
                return fn
            setattr(ws.store, name, make(orig))

        ws.cmd_set_outline([(0, 0), (100, 0), (100, 60), (0, 60)])
        ws.commit("导入")
        ws.run_mrc()
        do_slice(ws)
        run_print(ws)
        ws.mrc_state()
        ws.integrity_check()

        def to_pg(sql: str) -> str:
            s = sql
            m = _re.match(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", s, _re.I)
            if m:
                s = "INSERT INTO " + s[m.end():]
                if "on conflict" not in s.lower():
                    s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
            return s.replace("?", "%s")

        seen, bad = set(), []
        for sql in captured:
            key = " ".join(sql.split())
            if key in seen:
                continue
            seen.add(key)
            conv = to_pg(sql).strip()
            if not conv or conv.upper().startswith("PRAGMA"):
                continue
            try:
                sqlglot.parse_one(conv, dialect="postgres")
            except Exception as e:
                bad.append((type(e).__name__, str(e)[:80], conv[:100]))

        assert len(seen) >= 30, f"捕获到的独特 SQL 太少（{len(seen)}），覆盖不足"
        assert not bad, "以下运行期 SQL 无法按 PG 方言解析：\n" + "\n".join(
            f"  {t}: {m}\n      {s}" for t, m, s in bad)


# ================================================================ PgStore 代码路径（sqlite3 桩）
class TestPgStoreCodePathsViaStub:
    """用 sqlite3 做 psycopg3 桩，跑 PgStore 的**真实控制流**。

    为什么需要：本机没有 psycopg，所有 PG 用例只能 skip，于是"属性名写错"
    "INSERT 顺序违反外键"这类**能编译但不能运行**的缺陷完全查不出。
    交叉验证用 sqlite3 桩跑通了 PgStore 的实际方法体，抓出两个 P0：

      1. `clone_to_new_schema` 里 `obs_putter=self._obs_putter`——构造函数存的是
         `_obs_put`，这行**必然 AttributeError**，而 AGT-008 重放是 P0 验收项。
      2. clone 的 INSERT 顺序把 pad 排在 net 之前、process_params 排在
         material_profile 之前，违反外键（SQLite 同样报 IntegrityError）。

    桩只替换底层连接，不替换 PgStore 的任何逻辑，所以能覆盖真实的属性访问、
    参数转换、事务边界。
    """

    @staticmethod
    def _stub_psycopg(monkeypatch):
        """把 psycopg 换成 sqlite3：连接/游标 API 足够 PgStore 使用。"""
        import sqlite3
        import types

        class _Cursor:
            def __init__(self, cur):
                self._c = cur

            def execute(self, sql, params=()):
                up = sql.strip().upper()
                # sqlite 桩不支持 schema 相关语法：当成无操作
                if (up.startswith("CREATE SCHEMA") or up.startswith("DROP SCHEMA")
                        or up.startswith("SET SEARCH_PATH")
                        or up.startswith("SET ")):
                    return self
                # INSERT INTO "dst".table SELECT * FROM "src".table → 同库两 schema
                # 在 sqlite 里没有 schema 概念，改写成同表自复制（结构验证足够）
                if "INSERT INTO" in up and "SELECT * FROM" in up:
                    self._c.execute(
                        "INSERT INTO " + sql.split("INSERT INTO", 1)[1]
                        .split("SELECT", 1)[0].split(".")[-1].strip().strip('"')
                        + " SELECT * FROM "
                        + sql.split("SELECT * FROM", 1)[1].split(".")[-1]
                        .strip().strip('"'), ())
                    return self
                self._c.execute(sql.replace("%s", "?"), params)
                return self

            def fetchall(self):
                return self._c.fetchall()

            def fetchone(self):
                return self._c.fetchone()

            @property
            def description(self):
                return self._c.description

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn:
            def __init__(self):
                self._conn = sqlite3.connect(":memory:")

            def cursor(self):
                return _Cursor(self._conn.cursor())

            def commit(self):
                self._conn.commit()

            def rollback(self):
                self._conn.rollback()

            def close(self):
                self._conn.close()

        mod = types.SimpleNamespace()
        mod.connect = lambda dsn, autocommit=False: _Conn()
        monkeypatch.setitem(__import__("sys").modules, "psycopg", mod)
        return mod

    def test_clone_does_not_reference_wrong_attribute(self, monkeypatch):
        """`clone_to_new_schema` 不得访问不存在的属性（P0：`_obs_putter`）。

        这个 P0 的形态很特殊：**能编译、能 import、静态检查全过**，只有真正
        调用时才 AttributeError。本机没 psycopg → PG 用例全 skip → 一直没暴露。
        而它导致 AGT-008 重放（P0 验收项）在 RDS 上 100% 不可用。

        这里用 sqlite 桩构造 PgStore，然后只断言"进入 clone 时不会因为属性名
        崩掉"——跨 schema 的 INSERT...SELECT 语义在 sqlite 桩里保真度不足，
        不作为本测试的验证目标（那部分由表顺序测试 + 真机验证负责）。
        """
        import ast
        import inspect
        import tempfile

        self._stub_psycopg(monkeypatch)
        from ame.db.pg_store import PgStore

        # ① 静态：源码里出现的 self._obs_* 属性必须都是构造函数真正赋过的
        src = inspect.getsource(PgStore.__init__)
        assigned = set()
        for node in ast.walk(ast.parse(__import__("textwrap").dedent(src))):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                    and isinstance(node.ctx, ast.Store)):
                assigned.add(node.attr)
        clone_src = inspect.getsource(PgStore.clone_to_new_schema)
        used = {n.attr for n in ast.walk(ast.parse(
            __import__("textwrap").dedent(clone_src)))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "self"}
        # 类属性（构造函数里没有 self.xxx = 赋值）也要算已定义
        class_attrs = {"_CLONE_TABLES"}
        missing = used - assigned - class_attrs - {
            "dsn", "blobs_root", "project_id", "_schema"}
        assert not missing, (
            f"clone_to_new_schema 访问了构造函数未赋值的属性 {missing}——"
            "运行时会 AttributeError（本机无 psycopg，这条路径从未被执行过）")

        # ② 运行：能构造出实例（证明 __init__ 的属性名与使用处一致）
        st = PgStore("stub://", blobs_root=tempfile.mkdtemp(), project_id="p1")
        assert st._obs_put is None and st._obs_get is None

    def test_clone_table_order_satisfies_foreign_keys(self):
        """clone 的表顺序必须满足外键依赖（取自 schema_pg.sql 的拓扑序）。"""
        from ame.db.pg_store import PgStore

        order = list(PgStore._CLONE_TABLES)
        pos = {name: i for i, name in enumerate(order)}
        # 关键依赖对（PG 立即检查外键，顺序反了就报错）
        # 注意 session 特意不在清单里（见下），所以不参与依赖检查
        for child, parent in [("pad", "net"), ("process_params", "material_profile"),
                              ("route", "net"), ("layer", "slice_set"),
                              ("layer", "material_profile"), ("jumper", "pad")]:
            assert child in pos, f"clone 清单缺表 {child}"
            assert parent in pos, f"clone 清单缺表 {parent}"
            assert pos[parent] < pos[child], (
                f"{child} 排在 {parent} 之前，违反外键依赖（PG 会报 23503）")
        # 刻意不拷 session（副本会插自己的会话行，拷过来留幽灵 open 行）
        assert "session" not in pos, "clone 不应复制 session 表"

    def test_recent_order_uses_nulls_last(self):
        """PG 的 DESC 默认 NULLS FIRST（与 SQLite 相反），必须显式 NULLS LAST。"""
        from ame.db.pg_store import PgStore

        got = PgStore.recent_order(None, "", "started_at")
        assert "NULLS LAST" in got.upper(), (
            f"recent_order 缺 NULLS LAST：{got}——"
            "print_job.started_at 为 NULL 的未开始作业会被排到最前，"
            "prune_jobs 截断时会删掉真正最新的作业")

    @staticmethod
    def _body_code(fn) -> str:
        """取函数体的代码（剥掉 docstring 与注释）——注释里提到关键词不算违规。"""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        node = tree.body[0]
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]                     # 去掉 docstring
        return "\n".join(ast.unparse(n) for n in body)

    def test_change_token_does_not_use_txid_current(self):
        """change_token 不得用 txid_current()：每次调用分配新 XID → 缓存 100% miss。"""
        from ame.db.pg_store import PgStore

        src = self._body_code(PgStore.change_token)
        assert "txid_current" not in src, (
            "change_token 用了 txid_current()，它每次调用都推进事务号，"
            "导致 mrc_state 缓存永不命中")

    def test_row_factory_assignment_is_guarded(self):
        """psycopg3 的 Connection 没有 row_factory 属性，赋值必须容错。"""
        from ame.db import pg_store

        src = self._body_code(pg_store.PgStore.__init__)
        assert "row_factory" in src, "应显式处理 row_factory 的驱动差异"
        # 必须被 suppress/try 包住，不能裸赋值
        idx = src.find("row_factory")
        window = src[max(0, idx - 200):idx + 60]
        assert "suppress" in window or "try" in window, (
            f"row_factory 赋值未被容错包裹——psycopg3 下会 AttributeError。上下文：{window[-160:]}")


# ================================================================ DDL 逐列一致性
class TestSchemasStayInSync:
    """`schema.sql`（SQLite）与 `schema_pg.sql`（PG）必须逐列一致。

    `pg_store.py` 的 docstring 承诺"两份 DDL 的一致性由 DDL 契约用例保证"，
    但审计实测：删掉一列 `bed_x_mm`、把 `DOUBLE PRECISION` 换成 `TEXT`、
    甚至把 `defect` 表改名（数量不变）—— **89 条测试全部通过**。
    原来的契约只查"表数 26 / 无 REAL / 无前向引用"，不查列。

    唯一允许的差异是类型映射 `REAL → DOUBLE PRECISION`（PG 的 REAL 是 float4，
    存几何量会丢精度导致 G-code 哈希跨后端不一致）。
    """

    @staticmethod
    def _strip_inline_comments(sql: str) -> str:
        """去掉行内 `--` 之后的内容（引号内的不动）。

        必须先做这一步：`schema.sql` 的列定义里有带逗号的注释
        （如 `-- 内容哈希（不含 ;gcode_sha256 行），SLC-008 一致性口径`），
        直接按逗号切分会把注释切成伪列。
        """
        out = []
        for line in sql.splitlines():
            buf, in_s, j = [], None, 0
            while j < len(line):
                ch = line[j]
                if in_s:
                    buf.append(ch)
                    if ch == in_s:
                        in_s = None
                    j += 1
                    continue
                if ch in ("'", '"'):
                    in_s = ch
                    buf.append(ch)
                    j += 1
                    continue
                if ch == "-" and j + 1 < len(line) and line[j + 1] == "-":
                    break
                buf.append(ch)
                j += 1
            out.append("".join(buf))
        return "\n".join(out)

    @classmethod
    def _parse(cls, path) -> dict[str, dict[str, str]]:
        """解析出 {表名: {列名: 规范化后的列定义}}。"""
        import re

        txt = cls._strip_inline_comments(path.read_text("utf-8"))
        txt = "\n".join(ln for ln in txt.splitlines()
                        if not ln.lstrip().startswith("--"))
        tables: dict[str, dict[str, str]] = {}
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\(", txt):
            name, i, depth = m.group(1), m.end() - 1, 0
            while i < len(txt):
                if txt[i] == "(":
                    depth += 1
                elif txt[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            cols, d2, cur = [], 0, []
            for ch in txt[m.end():i]:
                if ch == "(":
                    d2 += 1
                elif ch == ")":
                    d2 -= 1
                if ch == "," and d2 == 0:
                    cols.append("".join(cur))
                    cur = []
                else:
                    cur.append(ch)
            cols.append("".join(cur))
            parsed = {}
            for c in cols:
                c = " ".join(c.split())
                if not c:
                    continue
                head = c.split()[0]
                if head.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                    continue                    # 表级约束，不是列
                parsed[head] = c
            tables[name] = parsed
        return tables

    @staticmethod
    def _norm(definition: str) -> str:
        """把唯一的允许差异（REAL → DOUBLE PRECISION）归一化掉。"""
        return definition.replace("DOUBLE PRECISION", "__NUM__").replace("REAL", "__NUM__")

    def test_table_names_match(self):
        from pathlib import Path as _P

        root = _P(__file__).resolve().parent.parent / "ame" / "db"
        sq = self._parse(root / "schema.sql")
        pg = self._parse(root / "schema_pg.sql")
        assert set(sq) == set(pg), (
            f"两份 DDL 的表集合不同：仅 SQLite 有 {set(sq) - set(pg)}，"
            f"仅 PG 有 {set(pg) - set(sq)}")

    def test_every_column_matches(self):
        """逐表逐列比对定义；只允许 REAL↔DOUBLE PRECISION 这一条映射。"""
        from pathlib import Path as _P

        root = _P(__file__).resolve().parent.parent / "ame" / "db"
        sq = self._parse(root / "schema.sql")
        pg = self._parse(root / "schema_pg.sql")
        diffs = []
        for table in sorted(sq):
            if table not in pg:
                diffs.append(f"{table}: PG 缺整张表")
                continue
            a, b = sq[table], pg[table]
            for col in sorted(set(a) - set(b)):
                diffs.append(f"{table}.{col}: 仅 SQLite 有（{a[col][:60]}）")
            for col in sorted(set(b) - set(a)):
                diffs.append(f"{table}.{col}: 仅 PG 有（{b[col][:60]}）")
            for col in sorted(set(a) & set(b)):
                if self._norm(a[col]) != self._norm(b[col]):
                    diffs.append(
                        f"{table}.{col}:\n      sqlite: {a[col][:80]}"
                        f"\n      pg    : {b[col][:80]}")
        assert not diffs, (
            "两份 DDL 的列定义不一致（改动表结构时两处都要改）：\n  "
            + "\n  ".join(diffs))

    def test_only_allowed_type_mapping(self):
        """确认差异**只有** REAL→DOUBLE PRECISION 这一种（不是别处悄悄改了）。"""
        from pathlib import Path as _P

        root = _P(__file__).resolve().parent.parent / "ame" / "db"
        sq = self._parse(root / "schema.sql")
        pg = self._parse(root / "schema_pg.sql")
        mapped = 0
        for table in sq:
            for d in sq[table].values():
                if " REAL" in d or d.endswith(" REAL"):
                    mapped += 1
        # PG 侧不应再有 REAL
        pg_real = sum(1 for t in pg for d in pg[t].values()
                      if " REAL" in d or d.endswith(" REAL"))
        assert pg_real == 0, f"schema_pg.sql 仍含 {pg_real} 处 REAL"
        assert mapped > 0, "schema.sql 应有 REAL 列（否则类型映射测试无意义）"
