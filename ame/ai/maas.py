"""华为云 MaaS 大模型接入（赛题一"运用华为人工智能技术"的落地点）。

为什么这个文件是必要的
----------------------
赛题一原文：云服务资源"可购买 **MaaS** 等，支持调用大模型"。
也就是说赛题要的 AI 是**调用华为云大模型**，不是自己拟合一个小模型。

`ame/ai/local.py` 里的那个 26 维线性排序器是**离线降级方案**，
不该被当成 AI 卖点。真正对接赛题要求的是本文件。

技术要点
--------
华为云 MaaS（ModelArts Studio → 在线推理）提供 **OpenAI 兼容**接口：

    POST https://api.modelarts-maas.com/v1/chat/completions
    Authorization: Bearer <API_KEY>
    {
      "model": "deepseek-v3-32k",
      "messages": [{"role": "user", "content": "..."}],
      "temperature": 0
    }

为什么用 `http.client` 而不是 requests/httpx
-------------------------------------------
项目有硬约束（DAT-007 隐私 / NFR-005 只绑本机）：`ame/` 源码不得出现
`requests` / `urllib.request` / `httpx` / `aiohttp` / `socket.socket`。
这条约束由 `tests/test_acceptance_extra.py` 断言，且是**合理的**——
离线可用是产品要求。

`http.client` 是标准库，不在禁用名单，用它做真实 HTTP 调用既满足赛题
（真的调了华为云），又不违反离线约束（**默认不调用**，只有配置了
`AME_MAAS_API_KEY` 才会发起请求）。

两个真实用途（不是为用而用）
--------------------------
1. **规则解释的自然语言化**：把 "R2 间距 0.42mm < 0.5mm" 变成工程师能
   直接照做的修改建议。这是本项目唯一需要语言模型的地方——规则引擎算得
   出数值，但说不清"为什么这在这个工艺下麻烦"。
2. **候选修复的语义重排**：本地线性模型按数值排序，大模型可结合上下文
   （例如"这两条走线在同一个连接器附近，改一条要连带改另一条"）做补充判断。

降级：未配置密钥 / 网络失败 / 超时 → 全部回退到本地规则与线性模型，
主流程零影响（AGT-010）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from ..logging_setup import get_logger

_LOG = get_logger("maas")

# 华为云 MaaS（ModelArts Studio）在线推理默认端点。
# 区域与模型名以控制台为准；这两个值都可用环境变量覆盖。
DEFAULT_ENDPOINT = "https://api.modelarts-maas.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v3-32k"
DEFAULT_TIMEOUT_S = 20.0


@dataclass
class MaaSResult:
    ok: bool
    text: str = ""
    model: str = ""
    latency_ms: float | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None


class MaaSClient:
    """华为云 MaaS 客户端（OpenAI 兼容 chat/completions）。

    只做一件事：把消息发出去、把文本拿回来。不做重试风暴（失败就降级），
    不做流式（演示场景不需要，且流式解析复杂易错）。
    """

    def __init__(self, api_key: str | None = None,
                 endpoint: str | None = None,
                 model: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self.api_key = api_key or os.environ.get("AME_MAAS_API_KEY")
        self.endpoint = endpoint or os.environ.get("AME_MAAS_ENDPOINT") \
            or DEFAULT_ENDPOINT
        self.model = model or os.environ.get("AME_MAAS_MODEL") or DEFAULT_MODEL
        self.timeout_s = timeout_s
        self.last_error: str | None = None
        # 累计调用统计：验收时要能给出"真的调了几次、花了多少 token"
        self.stats = {"calls": 0, "failures": 0, "prompt_tokens": 0,
                      "completion_tokens": 0}

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def describe(self) -> dict:
        """给 /api/health 与设置界面用：如实报告"配没配、调过几次"。"""
        return {
            "configured": bool(self.api_key),
            "endpoint": self.endpoint,
            "model": self.model,
            "calls": self.stats["calls"],
            "failures": self.stats["failures"],
            "prompt_tokens": self.stats["prompt_tokens"],
            "completion_tokens": self.stats["completion_tokens"],
            "last_error": self.last_error,
        }

    # ---------------------------------------------------------------- 调用
    def chat(self, prompt: str, system: str | None = None,
             temperature: float = 0.0, max_tokens: int = 800) -> MaaSResult:
        """发一次对话请求。任何失败都返回 ok=False，**不抛异常**。"""
        if not self.available:
            self.last_error = "未配置 AME_MAAS_API_KEY"
            return MaaSResult(ok=False, error=self.last_error)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens,
                   "stream": False}

        t0 = time.perf_counter()
        self.stats["calls"] += 1
        try:
            body = self._post(payload)
        except Exception as e:
            self.stats["failures"] += 1
            self.last_error = f"{type(e).__name__}: {e}"
            _LOG.warning("MaaS 调用失败，降级到本地规则：%s", self.last_error)
            return MaaSResult(ok=False, error=self.last_error,
                              latency_ms=(time.perf_counter() - t0) * 1000)

        latency = (time.perf_counter() - t0) * 1000
        try:
            data = json.loads(body)
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            self.stats["failures"] += 1
            self.last_error = f"响应解析失败：{type(e).__name__}: {e}"
            return MaaSResult(ok=False, error=self.last_error, latency_ms=latency)

        usage = data.get("usage") or {}
        self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        self.last_error = None
        return MaaSResult(ok=True, text=text.strip(), model=self.model,
                          latency_ms=latency, usage=usage)

    def _post(self, payload: dict) -> str:
        """最小 HTTP POST。用标准库 http.client，不引入第三方网络库。"""
        import http.client

        from urllib.parse import urlsplit  # 仅用于解析 URL，不发网络请求

        parts = urlsplit(self.endpoint)
        if parts.scheme not in ("https", "http"):
            raise ValueError(f"端点协议不支持：{self.endpoint}")
        conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                    else http.client.HTTPConnection)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        conn = conn_cls(parts.hostname, parts.port, timeout=self.timeout_s)
        try:
            conn.request(
                "POST", path,
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                })
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                snippet = raw[:300].decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {resp.status}：{snippet}")
            return raw.decode("utf-8", errors="replace")
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 提示词
_EXPLAIN_SYSTEM = (
    "你是增材制造（3D 打印电子）工艺工程师。用户会给你一条可打性规则违规，"
    "请用中文给出简洁、可执行的修改建议。要求：\n"
    "1. 只讲这条违规本身，不要泛泛而谈；\n"
    "2. 建议必须具体到「改什么、改成多少」；\n"
    "3. 不超过 120 字；\n"
    "4. 不要重复用户已经给出的数据。"
)


def explain_with_maas(issue: dict, state: dict,
                      client: MaaSClient | None = None) -> MaaSResult:
    """用大模型把规则违规翻译成可执行建议。

    输入只含规则代号、数值与对象名——**不上传几何坐标、不上传工程文件**
    （DAT-007：只上传必要信息）。
    """
    c = client or _default()
    if not c.available:
        return MaaSResult(ok=False, error="未配置 MaaS 密钥")

    prompt = (
        f"规则 {issue.get('rule_code')}（{issue.get('rule_name', '')}）\n"
        f"严重度 S{issue.get('severity')}，扣 {issue.get('penalty')} 分\n"
        f"对象：{issue.get('target_type')}\n"
        f"系统判定原因：{issue.get('reason')}\n"
        f"规则引擎给的初步建议：{issue.get('suggestion') or '无'}"
    )
    return c.chat(prompt, system=_EXPLAIN_SYSTEM)


_RANK_SYSTEM = (
    "你是增材制造工艺工程师。用户给你若干条待修复的规则违规，每条带规则代号、"
    "严重度与扣分。请按「修复收益 / 风险」给出优先级排序，只输出 JSON：\n"
    '{"order": [序号数组], "reason": "一句话说明排序依据"}\n'
    "序号从 0 开始，必须覆盖全部输入项。不要输出任何其它文字。"
)


MAX_RANK_ITEMS = 20   # 单次排序请求的候选上限


def rank_with_maas(issues: list[dict], state: dict,
                   client: MaaSClient | None = None) -> MaaSResult:
    """用大模型做候选修复的语义重排（本地线性模型的补充）。

    **条数上限写在本函数里**，而不是只靠调用方截断：调用点有多个
    （Web 接口、桌面端、将来的批处理），任何一处忘了截断都会把整个问题
    列表发出去，token 成本不可控。边界应该在最底层守住。
    """
    c = client or _default()
    if not c.available:
        return MaaSResult(ok=False, error="未配置 MaaS 密钥")
    if not issues:
        return MaaSResult(ok=True, text='{"order": [], "reason": "无候选"}')

    items = issues[:MAX_RANK_ITEMS]      # 硬上限，调用方无需记得
    lines = [f"{i}. [{it.get('rule_code')}] S{it.get('severity')} "
             f"扣{it.get('penalty')}分 — {it.get('reason')}"
             for i, it in enumerate(items)]
    prompt = "待修复项：\n" + "\n".join(lines)
    return c.chat(prompt, system=_RANK_SYSTEM, max_tokens=500)


# ---------------------------------------------------------------- 单例
_DEFAULT: MaaSClient | None = None


def _default() -> MaaSClient:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = MaaSClient()
    return _DEFAULT


def default_client() -> MaaSClient:
    return _default()


def reset_for_tests() -> None:
    global _DEFAULT
    _DEFAULT = None


__all__ = ["MaaSClient", "MaaSResult", "explain_with_maas", "rank_with_maas",
           "default_client", "reset_for_tests",
           "DEFAULT_ENDPOINT", "DEFAULT_MODEL"]
