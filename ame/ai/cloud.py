"""云端适配器（AGT-012 / 华为云 ModelArts + 盘古）.

为什么不在本文件里写网络代码
----------------------------
项目约束（DAT-007 隐私 / NFR-005 只绑本机）要求 `ame/` 源码**不出现**
网络库痕迹；同时赛题要求"运用华为人工智能技术"并能给出真实调用证据。
两者同时满足的做法是：**依赖注入**。

- 本文件只定义协议（`Transport`）、请求构造与响应解析，不含任何网络实现。
- 真正的传输调用由宿主注入：桌面端若已安装华为云 SDK，可用其封装一个
  transport（见 `ame.ai.transport_huawei` 的说明）；验收脚本注入录制的
  transport 以复现"一次真实训练任务 + 一次在线推理"。
- 未注入 transport 时 `available=False`，`service` 自动走本地模型，
  主流程零影响（AGT-010 无模型降级）。

这样"云端可选"不是文档话术，而是代码结构决定的：删掉注入就一定是本地路径。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# 华为云推理接口的默认形状（ModelArts 在线服务 / 盘古 Doer 均可用此封装）
DEFAULT_ENDPOINT = "https://modelarts.cn-north-4.myhuaweicloud.com/v1/infers"
DEFAULT_TIMEOUT_S = 8.0


@dataclass
class Transport:
    """最小传输协议：拿到 (url, payload, headers) 返回 JSON 可解析的 bytes.

    宿主可注入任意实现（SDK、录制回放、测试桩）；本模块不关心其内部。
    """

    send: Callable[[str, dict, dict], bytes]
    name: str = "injected"


@dataclass
class CloudResult:
    ok: bool
    scores: list[float] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    error: str | None = None
    latency_ms: float | None = None


class CloudRanker:
    """华为云在线推理：把特征矩阵送上去，拿回每个候选的收益打分。

    请求体刻意保持"纯数值 + 版本号"，不含器件名、网络名或几何坐标，
    满足 DAT-007「只上传必要信息」。
    """

    def __init__(self, transport: Transport | None = None,
                 endpoint: str = DEFAULT_ENDPOINT,
                 api_key: str | None = None,
                 enabled: bool = True,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self.transport = transport
        self.endpoint = endpoint
        # 密钥只存在于内存（DAT-007：密钥不写入工程文件）
        self.api_key = api_key
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.transport is not None)

    def describe(self) -> dict:
        return {"available": self.available,
                "enabled": self.enabled,
                "transport": getattr(self.transport, "name", None),
                "endpoint": self.endpoint,
                "last_error": self.last_error}

    def build_payload(self, feature_rows: list[list[float]]) -> dict:
        from .features import FEATURE_NAMES, FEATURE_VERSION

        return {"model": "ame-mrc-ranker", "feature_version": FEATURE_VERSION,
                "feature_names": list(FEATURE_NAMES),
                "instances": [list(map(float, row)) for row in feature_rows]}

    def rank(self, feature_rows: list[list[float]]) -> CloudResult:
        """返回逐候选打分；任何异常都转成 ok=False（NFR-007 云失败不影响主流程）。"""
        if not self.available:
            self.last_error = "云端未启用或未注入 transport"
            return CloudResult(ok=False, error=self.last_error)
        if not feature_rows:
            return CloudResult(ok=True, scores=[])
        payload = self.build_payload(feature_rows)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Auth-Token"] = self.api_key
        import time
        t0 = time.perf_counter()
        try:
            body = self.transport.send(self.endpoint, payload, headers)
        except Exception as e:      # 传输层任何失败都降级，不向上抛
            self.last_error = f"{type(e).__name__}: {e}"
            return CloudResult(ok=False, error=self.last_error)
        latency = (time.perf_counter() - t0) * 1000.0
        try:
            import json
            data = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray))
                              else str(body))
            scores = [float(x) for x in (data.get("scores") or [])]
        except (ValueError, AttributeError, TypeError, UnicodeDecodeError) as e:
            self.last_error = f"响应解析失败：{type(e).__name__}: {e}"
            return CloudResult(ok=False, error=self.last_error)
        if len(scores) != len(feature_rows):
            self.last_error = (f"响应条数 {len(scores)} 与请求条数 "
                               f"{len(feature_rows)} 不符")
            return CloudResult(ok=False, error=self.last_error, raw=data,
                               latency_ms=latency)
        self.last_error = None
        return CloudResult(ok=True, scores=scores, raw=data, latency_ms=latency)


__all__ = ["Transport", "CloudRanker", "CloudResult",
           "DEFAULT_ENDPOINT", "DEFAULT_TIMEOUT_S"]
