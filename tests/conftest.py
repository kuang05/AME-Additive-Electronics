"""测试公共夹具：临时数据目录 + 工程构建助手（T01–T20 共用）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ame.db.store import DataRoot  # noqa: E402
from ame.ledger.services import do_slice, run_print  # noqa: E402
from ame.ledger.workspace import Workspace, create_project  # noqa: E402

SAMPLES = ROOT / "samples" / "data"


@pytest.fixture()
def data_root(tmp_path) -> DataRoot:
    return DataRoot(tmp_path / "data")


@pytest.fixture()
def sample(name="T19A"):
    def _load(fname: str) -> str:
        return (SAMPLES / fname).read_text("utf-8")
    return _load


def build_project(data_root: DataRoot, name="测试工程", step="plate_100x60.step",
                  board="board_T19A.kicad_pcb", routes=None) -> Workspace:
    """导入结构 + 板文件（+ 可选追加走线），返回未提交的工作区。"""
    ws = create_project(data_root, name)
    if step:
        ws.cmd_import_step((SAMPLES / step).read_text("utf-8"), step)
    if board:
        ws.cmd_import_board((SAMPLES / board).read_text("utf-8"))
    if routes:
        for r in routes:
            ws.cmd_add_route(r["net"], r["pts"], r["width"])
    return ws


def routes_of(fname: str) -> list[dict]:
    return json.loads((SAMPLES / fname).read_text("utf-8"))


__all__ = ["SAMPLES", "build_project", "routes_of", "do_slice", "run_print",
           "create_project", "DataRoot"]


def wait_for(app, cond, timeout=10.0, interval=0.02):
    """等待 Tk 应用中的异步任务完成（UI-005 后台任务用）。

    后台任务通过 after() 轮询回到主线程，测试里必须驱动事件循环。
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if cond():
            return True
        time.sleep(interval)
    app.update()
    return cond()


__all__ += ["wait_for"]


# ---------------------------------------------------------------- Web 层共享夹具
@pytest.fixture()
def client(tmp_path, monkeypatch):
    """FastAPI TestClient：每用例独立数据目录，SQLite 后端。"""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import ame.web.app as webapp

    monkeypatch.setenv("AME_DATA_DIR", str(tmp_path / "cloud"))
    monkeypatch.delenv("AME_PG_DSN", raising=False)
    monkeypatch.delenv("AME_REDIS_URL", raising=False)
    monkeypatch.delenv("AME_MAAS_API_KEY", raising=False)
    # 清空进程内缓存与 MaaS 单例：它们是模块级全局状态，不清理会让
    # 上一个用例的切片结果被下一个用例命中，表现为"改了设计还返回旧结果"
    # 这种看起来像业务 bug 的假阳性。
    #
    # 注意还要清 _SESSIONS：Workspace 持有 mrc_state 缓存，跨用例复用会让
    # 上一个工程的状态泄漏到下一个（表现为随机的 KeyError/字段缺失）。
    from ame.ai import maas as _maas
    from ame.db import cache as _cache
    _cache.reset_for_tests()
    _maas.reset_for_tests()
    for _ws in list(webapp._SESSIONS.values()):
        try:
            _ws.store.close()
        except Exception:
            pass
    webapp.ROOT = webapp._build_root()
    webapp._SESSIONS.clear()
    with TestClient(webapp.app) as c:
        yield c
    webapp._SESSIONS.clear()
    _cache.reset_for_tests()
    _maas.reset_for_tests()
