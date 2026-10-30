"""华为云 MaaS 接入验收：请求构造、降级、真实 HTTP 行为、隐私边界。

测试策略
--------
没有真实 API Key 也能验证**除网络往返之外**的全部逻辑：

- 用 monkeypatch 替换 `MaaSClient._post`，断言请求体与响应解析；
- 用本地临时 HTTP 服务验证 `_post` 真的会发请求（不需要外部网络）；
- 未配置密钥时必须是**纯降级**（不发任何请求）。

同时守住三条边界：
1. **隐私**：上传内容不含几何坐标、工程文件名（DAT-007）；
2. **离线**：未配置时零网络调用（NFR-007）；
3. **诚实**：失败时必须报 ok=False 并带原因，不得假装成功。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ame.ai.maas import (DEFAULT_ENDPOINT, MaaSClient, explain_with_maas,
                         rank_with_maas, reset_for_tests)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("AME_MAAS_API_KEY", raising=False)
    monkeypatch.delenv("AME_MAAS_ENDPOINT", raising=False)
    monkeypatch.delenv("AME_MAAS_MODEL", raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


# ================================================================ 未配置 → 纯降级
def test_unconfigured_makes_no_request(monkeypatch):
    """未配置密钥时不得发起任何网络请求（NFR-007 离线）。"""
    called = []
    monkeypatch.setattr(MaaSClient, "_post",
                        lambda self, p: called.append(p) or "{}")
    c = MaaSClient()
    assert c.available is False
    res = c.chat("你好")
    assert res.ok is False
    assert "AME_MAAS_API_KEY" in (res.error or "")
    assert called == [], "未配置时不得发请求"


def test_unconfigured_explain_falls_back(monkeypatch):
    """解释接口降级：返回 ok=False，调用方用规则引擎原文。"""
    monkeypatch.setattr(MaaSClient, "_post", lambda self, p: "{}")
    res = explain_with_maas({"rule_code": "R1", "reason": "线宽 0.3 < 0.5",
                             "severity": 3, "penalty": 5.0,
                             "target_type": "route", "suggestion": "加宽到 0.6"},
                            {})
    assert res.ok is False
    assert res.error


# ================================================================ 请求构造
def test_request_payload_shape(monkeypatch):
    """请求体必须是 OpenAI 兼容形状，且 temperature=0（可复现）。"""
    captured = {}

    def fake_post(self, payload):
        captured.update(payload)
        return json.dumps({
            "choices": [{"message": {"content": "把线宽加到 0.6 mm"}}],
            "usage": {"prompt_tokens": 42, "completion_tokens": 12},
        })

    monkeypatch.setattr(MaaSClient, "_post", fake_post)
    c = MaaSClient(api_key="test-key")
    res = c.chat("测试", system="你是工程师")

    assert res.ok is True
    assert res.text == "把线宽加到 0.6 mm"
    assert captured["model"]
    assert captured["temperature"] == 0.0
    assert captured["stream"] is False
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    # token 统计要累计（验收时要能报数）
    assert c.stats["prompt_tokens"] == 42
    assert c.stats["completion_tokens"] == 12


def test_stats_accumulate_across_calls(monkeypatch):
    """多次调用的 token 要累加——这是"真的调了几次"的证据来源。"""
    monkeypatch.setattr(MaaSClient, "_post", lambda self, p: json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }))
    c = MaaSClient(api_key="k")
    for _ in range(3):
        c.chat("x")
    d = c.describe()
    assert d["calls"] == 3
    assert d["prompt_tokens"] == 30
    assert d["completion_tokens"] == 15


# ================================================================ 失败与降级
def test_http_error_degrades_with_reason(monkeypatch):
    """HTTP 4xx/5xx 必须降级并给出原因，不抛异常。"""
    def boom(self, payload):
        raise RuntimeError("HTTP 401：invalid api key")

    monkeypatch.setattr(MaaSClient, "_post", boom)
    c = MaaSClient(api_key="bad")
    res = c.chat("x")
    assert res.ok is False
    assert "401" in res.error
    assert c.stats["failures"] == 1
    assert c.stats["calls"] == 1


@pytest.mark.parametrize("bad_body", [
    "not json",
    json.dumps({}),
    json.dumps({"choices": []}),
    json.dumps({"choices": [{}]}),
])
def test_malformed_response_degrades(monkeypatch, bad_body):
    """畸形响应不得当成功用（会污染下游）。"""
    monkeypatch.setattr(MaaSClient, "_post", lambda self, p: bad_body)
    c = MaaSClient(api_key="k")
    res = c.chat("x")
    assert res.ok is False
    assert res.error


def test_timeout_degrades(monkeypatch):
    """超时必须降级（演示场景不能卡住界面）。"""
    def slow(self, payload):
        raise TimeoutError("timed out")

    monkeypatch.setattr(MaaSClient, "_post", slow)
    c = MaaSClient(api_key="k", timeout_s=0.1)
    res = c.chat("x")
    assert res.ok is False
    assert "Timeout" in (res.error or "")


# ================================================================ 真实 HTTP（本地服务）
class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []
    status = 200
    body = json.dumps({"choices": [{"message": {"content": "好的"}}],
                       "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        type(self).received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(raw.decode("utf-8")),
        })
        payload = type(self).body.encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):     # 静默，不污染测试输出
        pass


@pytest.fixture()
def local_maas():
    """起一个本地 HTTP 服务模拟 MaaS 端点（不访问外网）。"""
    _Handler.received = []
    _Handler.status = 200
    # 必须重置 body：否则上一个用例（例如 401）的响应体会被下个用例继承，
    # 表现为"解析失败"这种看起来像代码 bug 的假阳性。
    _Handler.body = json.dumps({"choices": [{"message": {"content": "好的"}}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    yield f"http://127.0.0.1:{port}/v1/chat/completions"
    srv.shutdown()


def test_real_http_roundtrip(local_maas):
    """真实发一次 HTTP：验证 _post 的报文头、路径、JSON 编码都对。"""
    c = MaaSClient(api_key="secret-key", endpoint=local_maas)
    res = c.chat("线宽太小怎么办", system="你是工程师")

    assert res.ok is True, res.error
    assert res.text == "好的"
    assert res.latency_ms is not None

    req = _Handler.received[-1]
    assert req["path"] == "/v1/chat/completions"
    assert req["auth"] == "Bearer secret-key"
    assert req["body"]["messages"][-1]["content"] == "线宽太小怎么办"
    assert req["body"]["messages"][0]["role"] == "system"


def test_real_http_error_status(local_maas):
    """服务返回 401 时降级并带原因。"""
    _Handler.status = 401
    _Handler.body = json.dumps({"error": "invalid token"})
    c = MaaSClient(api_key="bad", endpoint=local_maas)
    res = c.chat("x")
    assert res.ok is False
    assert "401" in (res.error or "")


# ================================================================ 隐私边界
def test_explain_prompt_has_no_geometry_or_filenames(local_maas):
    """上传内容只含规则与数值，不含坐标、文件名（DAT-007）。"""
    c = MaaSClient(api_key="k", endpoint=local_maas)
    issue = {"rule_code": "R2", "reason": "走线 N6 与 焊盘C3.3 间距 0.42 mm 低于下限 0.50 mm",
             "severity": 2, "penalty": 3.3, "target_type": "net",
             "suggestion": "错开走线", "rule_name": "间距"}
    res = explain_with_maas(issue, {"routes": [
        {"id": "abc-123", "pts": [[17.2, 15.0], [40, 30]], "net": "N6"}]}, client=c)
    assert res.ok is True

    sent = json.dumps(_Handler.received[-1]["body"], ensure_ascii=False)
    # 不得出现坐标数值（17.2 / 40,30）或工程内部 id
    for leak in ("17.2", "abc-123", "[[17.2"):
        assert leak not in sent, f"上传内容泄漏了几何信息：{leak}"
    # 规则与数值是允许且必要的
    assert "R2" in sent


def test_rank_prompt_is_bounded(local_maas):
    """排序请求只发前 20 条（控制 token 成本），且不含坐标。"""
    c = MaaSClient(api_key="k", endpoint=local_maas)
    _Handler.body = json.dumps({
        "choices": [{"message": {"content": '{"order":[0],"reason":"先修严重度高的"}'}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5}})
    issues = [{"rule_code": "R1", "severity": 3, "penalty": 5.0,
               "reason": f"第{i}条"} for i in range(30)]
    res = rank_with_maas(issues, {}, client=c)
    assert res.ok is True
    sent = json.dumps(_Handler.received[-1]["body"], ensure_ascii=False)
    assert "第25条" not in sent, "超过 20 条的候选不应上传"


# ================================================================ 端点集成
def test_web_explain_endpoint_degrades_without_key(client):
    """未配置密钥时，解释接口如实返回降级状态而非 500。"""
    pid = client.post("/api/projects/sample").json()["id"]
    r = client.post(f"/api/projects/{pid}/explain/ai",
                    json={"rule_code": "R1", "reason": "线宽 0.3 < 0.5",
                          "severity": 3, "penalty": 5.0,
                          "suggestion": "加宽到 0.6"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["error"]
    assert d["fallback"] == "加宽到 0.6", "降级时应返回规则引擎原文"


def test_web_rank_endpoint_degrades_without_key(client):
    pid = client.post("/api/projects/sample").json()["id"]
    r = client.get(f"/api/projects/{pid}/ai/rank/maas")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert d["order"] == []


def test_health_reports_maas_state(client):
    """/api/health 必须报告 MaaS 配置与调用统计（赛题证据点）。"""
    h = client.get("/api/health").json()
    assert "maas" in h
    m = h["maas"]
    assert m["configured"] is False
    assert m["calls"] == 0
    assert m["model"]


def test_default_endpoint_is_huawei_cloud():
    """默认端点必须指向华为云（防止误配到别家）。"""
    assert "myhuaweicloud.com" in DEFAULT_ENDPOINT or "modelarts" in DEFAULT_ENDPOINT
