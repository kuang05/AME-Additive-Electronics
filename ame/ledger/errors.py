"""命令行错误类型（被 commands / workspace / services / UI 共用）。

从 `workspace.py` 拆出：`commands.py` 需要抛 `CmdError`，而 `workspace.py`
需要从 `commands.py` 导入 mixin——两者放在同一文件会形成循环导入。
错误类型本身没有依赖，放在最底层的独立模块是唯一无环的选择。

语义（CIR-012 / STR-009）：命令失败必须**不改变任何状态**，
且消息是给用户看的中文可读原因，不是内部堆栈。
"""
from __future__ import annotations


class CmdError(Exception):
    """命令失败：带用户可读原因；失败不改变状态（CIR-012/STR-009）。"""


__all__ = ["CmdError"]
