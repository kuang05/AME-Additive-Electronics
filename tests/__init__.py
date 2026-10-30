"""测试包标记。

为什么需要有这个空文件（不是可有可无的形式主义）
------------------------------------------------
测试里大量使用 `from tests.conftest import build_project`（绝对导入）。
没有 `__init__.py` 时 `tests` 只是**命名空间包**，而命名空间包的优先级
低于任何已安装的同名普通包——910B 节点（华为 ModelArts 镜像）的
site-packages 里恰好有名为 `tests` 的模块，于是 4 个测试文件在收集阶段
直接 `ModuleNotFoundError: No module named 'tests.conftest'`，
而同样的代码在本地 Windows 环境（CWD 在 sys.path 最前）却能通过。

加上本文件后 `tests` 成为普通包，导入行为与环境无关，本地与 910B
两边跑出同一结果——这是"可复现"的一部分，不是洁癖。
"""
