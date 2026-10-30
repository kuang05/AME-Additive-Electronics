"""Redis 加速层验收：缓存命中/失效、工程锁、无 Redis 降级。

设计前提：**Redis 是可选加速，不是功能依赖**。
因此这里重点验证"没有 Redis 时行为完全正确，只是少了缓存"——
这条性质一旦破坏，就等于把核心功能绑到了网络组件上（违反 NFR-007）。
"""
from __future__ import annotations

import threading
import time

import pytest

from ame.db import cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AME_REDIS_URL", raising=False)
    cache.reset_for_tests()
    yield
    cache.reset_for_tests()


# ================================================================ 降级行为
def test_no_redis_degrades_to_noop_cache():
    """没有 Redis 时缓存不报错，且能命中进程内缓存（不算功能缺失）。"""
    assert cache.available() is False
    key = "ame:slice:p:abc"
    assert cache.cache_get(key) is None
    cache.cache_set(key, {"gcode_hash": "deadbeef", "passes": 7})
    assert cache.cache_get(key) == {"gcode_hash": "deadbeef", "passes": 7}


def test_unreachable_redis_does_not_raise(monkeypatch):
    """配了 Redis 但连不上：不得抛异常，必须静默降级（NFR-007）。"""
    monkeypatch.setenv("AME_REDIS_URL", "redis://127.0.0.1:6399/0")
    cache.reset_for_tests()
    # 连接失败后 available() 为 False，且不抛异常
    assert cache.available() is False
    cache.cache_set("k", {"v": 1})
    assert cache.cache_get("k") == {"v": 1}      # 落到进程内缓存


def test_local_cache_respects_capacity():
    """进程内缓存有上限，防止长跑进程内存无限增长。"""
    for i in range(cache._LOCAL_CACHE_MAX + 50):
        cache.cache_set(f"k{i}", {"i": i})
    assert len(cache._LOCAL_CACHE) <= cache._LOCAL_CACHE_MAX


# ================================================================ 缓存键
def test_cache_key_depends_on_design_and_params_only():
    """键只由设计摘要与参数摘要决定：同设计同参数 → 同键（否则永不命中）。"""
    a = cache.key_for("p1", "designA", "paramsA")
    b = cache.key_for("p1", "designA", "paramsA")
    assert a == b, "相同输入必须得到相同键"

    assert a != cache.key_for("p1", "designB", "paramsA"), "设计变了要换键"
    assert a != cache.key_for("p1", "designA", "paramsB"), "参数变了要换键"
    assert a != cache.key_for("p2", "designA", "paramsA"), "工程不同要换键"


def test_cache_key_has_no_unstable_parts():
    """键里不能含时间戳等易变部分（那会导致缓存永不命中）。"""
    k1 = cache.key_for("p", "d", "x")
    time.sleep(0.01)
    k2 = cache.key_for("p", "d", "x")
    assert k1 == k2


# ================================================================ 工程锁
def test_project_lock_is_exclusive_within_process():
    """同一工程的锁互斥；不同工程互不影响。"""
    order: list[str] = []

    def worker(tag: str, pid: str):
        with cache.project_lock(pid) as got:
            assert got, "进程内锁应当总能获取"
            order.append(f"{tag}-enter")
            time.sleep(0.15)
            order.append(f"{tag}-exit")

    t1 = threading.Thread(target=worker, args=("A", "same"))
    t2 = threading.Thread(target=worker, args=("B", "same"))
    t1.start(); t2.start(); t1.join(); t2.join()

    # 互斥：必须是 enter/exit 成对，不能交叉
    assert order in (["A-enter", "A-exit", "B-enter", "B-exit"],
                     ["B-enter", "B-exit", "A-enter", "A-exit"]), order


def test_project_lock_different_projects_run_concurrently():
    """不同工程不互相阻塞（否则多用户会被无谓串行化）。"""
    elapsed: list[float] = []

    def worker(pid: str):
        t0 = time.time()
        with cache.project_lock(pid):
            time.sleep(0.2)
        elapsed.append(time.time() - t0)

    threads = [threading.Thread(target=worker, args=(f"p{i}",)) for i in range(3)]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    total = time.time() - t0
    assert total < 0.5, f"不同工程应并发执行，实际耗时 {total:.2f}s"


def test_project_lock_releases_after_exception():
    """持锁期间抛异常必须释放，否则该工程后续永久阻塞。"""
    with pytest.raises(ValueError), cache.project_lock("pid-x"):
        raise ValueError("boom")
    # 还能再次获取
    with cache.project_lock("pid-x") as got:
        assert got


# ================================================================ 端到端（HTTP）
def test_slice_endpoint_reports_cache_flag(client):
    """切片接口返回 cached 字段：首次 false，同条件再次请求 true。"""
    pid = client.post("/api/projects/sample").json()["id"]
    first = client.post(f"/api/projects/{pid}/slice").json()
    assert first["cached"] is False
    assert first["gcode_hash"]

    second = client.post(f"/api/projects/{pid}/slice").json()
    assert second["cached"] is True, "同设计同参数第二次应命中缓存"
    assert second["gcode_hash"] == first["gcode_hash"], "缓存不得改变结果"


def test_cache_invalidated_when_design_changes(client):
    """设计变了缓存必须失效——否则会返回与当前设计不符的 G-code。

    **不能随便挑一条走线**：实测把某条走线改宽到 0.9 mm 会让它和相邻异网
    走线的导体区合并，触发 SLC-007 连通性阻断（这是引擎的正确行为，
    不是 bug）。早期用 ``routes[0]``，而走线顺序随 uuid 变，于是这个用例
    有约 30% 概率挑到"改宽就短路"的那一条，表现为间歇性 KeyError。

    正确做法：把改宽做成**幂等的小幅调整**（0.60 → 0.65），
    只验证"设计变了 → 缓存失效"，不引入拓扑变化。
    """
    pid = client.post("/api/projects/sample").json()["id"]
    first = client.post(f"/api/projects/{pid}/slice").json()
    assert first["cached"] is False

    routes = client.get(f"/api/projects/{pid}/state").json()["routes"]
    # 选一条宽度 >= 0.6 的：小幅加到 0.65 不会让它与异网导体合并
    target = next((r for r in routes if r["width"] >= 0.6), routes[0])
    new_width = round(target["width"] + 0.05, 3)
    r = client.post(f"/api/projects/{pid}/routes/{target['id']}/width",
                    json={"width": new_width})
    assert r.status_code == 200, r.text

    after = client.post(f"/api/projects/{pid}/slice")
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["cached"] is False, "设计变更后必须重算"
    assert body["gcode_hash"] != first["gcode_hash"], "设计变了哈希应当不同"


def test_health_reports_redis_honestly(client, monkeypatch):
    """健康检查必须如实说明 Redis 状态（未配置 ≠ 可用）。"""
    h = client.get("/api/health").json()
    assert "redis" in h
    assert h["redis"]["configured"] is False
    assert h["redis"]["available"] is False

    monkeypatch.setenv("AME_REDIS_URL", "redis://127.0.0.1:6399/0")
    cache.reset_for_tests()
    h2 = client.get("/api/health").json()
    assert h2["redis"]["configured"] is True
    assert h2["redis"]["available"] is False, "连不上就不能报可用"


def test_health_storage_reports_unavailable_when_pg_misconfigured(
        client, monkeypatch):
    """配了 PG 但连不上：health 必须报 unavailable + 错误原因。

    这是上一版的一个假报告缺陷：只看环境变量存在就报 postgresql，
    会让部署排障和验收得出完全错误的结论。
    """
    monkeypatch.setenv("AME_PG_DSN", "postgresql://nobody:nobody@127.0.0.1:1/none")
    import ame.web.app as webapp
    webapp.ROOT = webapp._build_root()
    webapp._SESSIONS.clear()
    h = client.get("/api/health").json()
    assert h["ok"] is False
    assert h["storage"] == "unavailable"
    assert h["storage_configured"] == "postgresql"
    assert h["error"], "必须给出失败原因"


def test_widening_route_can_legitimately_block_slicing(client):
    """把走线改宽到 0.9 mm 可能造成异网短路 → 切片被 SLC-007 正确阻断。

    这条测试把一个"看起来像 bug"的现象固定为**预期行为**：

    样例板上有若干相邻的异网走线，其中某些在 0.6 mm 时刚好不接触，
    改宽到 0.9 mm 后导体区合并 → 连通性检查发现短路 → 阻断切片。

    这是引擎的正确设计（SLC-007：连通性失败必须阻断导出，不能打出废板），
    不是缺陷。记录在这里是为了防止将来有人把它当 bug "修掉"。
    """
    pid = client.post("/api/projects/sample").json()["id"]
    client.post(f"/api/projects/{pid}/slice")

    routes = client.get(f"/api/projects/{pid}/state").json()["routes"]
    blocked = []
    for r in routes:
        # 每条单独试（新工程，避免互相影响）
        p2 = client.post("/api/projects/sample").json()["id"]
        client.post(f"/api/projects/{p2}/slice")
        client.post(f"/api/projects/{p2}/routes/{r['id']}/width",
                    json={"width": 0.9})
        resp = client.post(f"/api/projects/{p2}/slice")
        if resp.status_code != 200:
            blocked.append((r["net"], resp.json().get("detail", "")[:60]))

    # 不强制一定有条目（不同板型可能都安全），但若有，必须是连通性阻断
    for net, detail in blocked:
        assert "连通性" in detail or "阻断" in detail, \
            f"{net} 改宽后失败原因应指向连通性，实际：{detail}"


# ================================================================ 会话 LRU
def test_open_project_sessions_are_bounded(client):
    """打开大量工程时，服务端会话数必须有上限。

    每个 Workspace 持有一个 SQLite/PG 连接 + 视图缓存。早期实现 ``_SESSIONS``
    只增不减——实测开 20 个工程就有 20 个连接常驻，长跑进程会耗尽
    连接池（PG 默认 100 个连接，几十个用户就顶到上限）。
    """
    import ame.web.app as webapp

    pids = []
    for i in range(webapp.MAX_OPEN_PROJECTS + 6):
        pid = client.post("/api/projects", json={"name": f"LRU{i}"}).json()["id"]
        client.get(f"/api/projects/{pid}/state")
        pids.append(pid)

    assert len(webapp._SESSIONS) <= webapp.MAX_OPEN_PROJECTS, \
        f"会话数 {len(webapp._SESSIONS)} 超过上限 {webapp.MAX_OPEN_PROJECTS}"


def test_evicted_project_still_accessible(client):
    """被 LRU 淘汰的工程必须能重新打开（只关连接，不删数据）。"""
    import ame.web.app as webapp

    pids = []
    for i in range(webapp.MAX_OPEN_PROJECTS + 3):
        pid = client.post("/api/projects", json={"name": f"EV{i}"}).json()["id"]
        client.get(f"/api/projects/{pid}/state")
        pids.append(pid)

    oldest = pids[0]
    assert oldest not in webapp._SESSIONS, "最早的应已被淘汰"
    r = client.get(f"/api/projects/{oldest}/state")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "EV0"


def test_lru_refreshes_on_access(client):
    """访问过的工程刷新其 LRU 位置，不会被优先淘汰。"""
    import ame.web.app as webapp

    pids = []
    for i in range(webapp.MAX_OPEN_PROJECTS):
        pid = client.post("/api/projects", json={"name": f"R{i}"}).json()["id"]
        client.get(f"/api/projects/{pid}/state")
        pids.append(pid)

    # 把第一个"再用一次"，它应该变成最新的
    client.get(f"/api/projects/{pids[0]}/state")
    # 再开一个新工程，应该淘汰 pids[1] 而不是 pids[0]
    new_pid = client.post("/api/projects", json={"name": "NEW"}).json()["id"]
    client.get(f"/api/projects/{new_pid}/state")

    assert pids[0] in webapp._SESSIONS, "刚访问过的工程不该被淘汰"
    assert pids[1] not in webapp._SESSIONS, "最久未用的应被淘汰"
