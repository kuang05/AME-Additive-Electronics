"""存储接口（Storage Protocol / 抽象基类）。

为什么要有这一层
----------------
本项目原本只有一种存储：本地 SQLite 文件。上云后需要 PostgreSQL（RDS），
但**不能因此改动业务逻辑**——26 个写命令、只读视图、版本账本都只依赖
下面这 8 个方法：

    execute / query / one / batch / put_blob / get_blob / verify_blob / close

把这 8 个方法定义成协议后，`ledger/`、`agent/`、`web/` 全部只面向协议编程，
换实现不需要碰任何业务代码。这是云原生化能成立的前提。

设计约束
--------
1. **占位符必须统一**：业务层现在用 SQLite 的 ``?`` 风格。PostgreSQL 用 ``%s``。
   实现层负责转换（见 `pg_store.sqlish`），业务层不感知。
2. **``execute`` 不返回游标语义**：只保证"写已生效"。SQLite 实现返回 cursor
   是为了兼容旧调用点，新代码不应依赖它的返回值。
3. **blob 是内容寻址**：键是 sha256，值是字节。云上可落本地卷或对象存储，
   接口不变。
4. **事务边界由 ``batch()`` 显式声明**：不用隐式自动提交，避免两种实现的
   提交时机不同导致行为漂移。
"""
from __future__ import annotations

import contextlib
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Storage(Protocol):
    """工程库存储接口（8 个方法）。"""

    def execute(self, sql: str, params=()) -> Any:
        """执行一条写语句（或 DDL）。返回实现相关的游标对象，业务层不应依赖。"""
        ...

    def query(self, sql: str, params=()) -> list[dict]:
        """查询多行，返回 dict 列表（键为列名）。"""
        ...

    def one(self, sql: str, params=()) -> dict | None:
        """查询单行，无结果返回 None。"""
        ...

    def batch(self) -> contextlib.AbstractContextManager:
        """批量写入上下文：块内只提交一次，异常整体回滚。"""
        ...

    def put_blob(self, data: bytes) -> tuple[str, str]:
        """按内容哈希存储大文件，返回 (sha256, 相对路径)。"""
        ...

    def get_blob(self, sha: str) -> bytes:
        """按 sha256 取回内容；不存在抛 KeyError。"""
        ...

    def verify_blob(self, sha: str) -> tuple[bool, str]:
        """校验内容是否被外部改动，返回 (一致?, 说明)。"""
        ...

    def close(self) -> None:
        """释放连接。"""
        ...

    # ---------------------------------------------------------------- 方言适配
    # 下面三个方法存在的唯一理由：业务层（workspace/services）早期直接写了
    # SQLite 专有语法，在 PostgreSQL 上会全部报错。把"怎么做"压到实现层，
    # 业务层只表达"要什么"。

    def has_table(self, name: str) -> bool:
        """表是否存在。

        业务层原写法 `SELECT name FROM sqlite_master WHERE type='table'`，
        PG 没有 sqlite_master（应查 information_schema.tables）。
        """
        ...

    def change_token(self) -> tuple:
        """库变更指纹，用于缓存失效判定。

        业务层原写法读 `PRAGMA data_version` + `conn.total_changes`，
        PG 两者都没有（psycopg 的 Connection 无 total_changes 属性）。
        返回可比较的元组即可，语义只要"库变了就不同"。
        """
        ...

    def recent_order(self, alias: str = "", time_col: str = "created_at") -> str:
        """返回"时间倒序 + 稳定次级键"的 ORDER BY 子句（不含 ORDER BY 关键字）。

        业务层原写法 `ORDER BY created_at DESC, rowid DESC`：SQLite 有隐式
        rowid 伪列，PG 没有。实现层返回各自可用的次级键
        （SQLite 用 rowid，PG 用主键或 ctid）。
        """
        ...


class StorageError(RuntimeError):
    """存储层错误基类（便于上层区分"数据库问题"与"业务问题"）。"""


class BlobMissing(StorageError, KeyError):
    """内容寻址文件不存在。"""


def assert_implements(obj: object) -> None:
    """启动时自检：确认实现类提供了全部 8 个方法。

    放在 `Storage` 协议旁边而不是各处 import 时检查，是为了让
    "实现漏了方法"在启动阶段就暴露，而不是等到某个功能偶然被点到。
    """
    required = ("execute", "query", "one", "batch", "put_blob", "get_blob",
                "verify_blob", "close", "has_table", "change_token", "recent_order")
    missing = [m for m in required if not callable(getattr(obj, m, None))]
    if missing:
        raise StorageError(
            f"{type(obj).__name__} 未实现存储接口方法：{', '.join(missing)}")


__all__ = ["Storage", "StorageError", "BlobMissing", "assert_implements"]
