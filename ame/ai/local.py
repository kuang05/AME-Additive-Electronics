"""本地模型：加载权重、预测候选修复的期望收益（离线可用，AGT-010）.

模型选择
--------
这里用**岭回归**而不是神经网络，理由是可复现与可解释优先于拟合容量：

- 特征只有 24 维、样本量在几百到几千量级，线性模型已经能把"先修哪条"
  排序做对；深度模型在这个数据量上没有可测量的优势，反而带来不可复现。
- 权重是一个 JSON 文件，参与版本管理与哈希；同一权重 + 同一特征 →
  同一排序（AGT-008 / SLC-008 的确定性要求）。
- 权重缺失或损坏时自动退回"按规则权重 × 严重度"的启发式排序，
  主流程不中断（NFR-002）。

云端（cloud.py）用的是同一套特征；本地与云端只有**打分器**不同。
"""
from __future__ import annotations

import json
from pathlib import Path

from .features import FEATURE_NAMES, FEATURE_VERSION

# 权重随包发布（打包时作为数据文件）
WEIGHTS_PATH = Path(__file__).with_name("weights.json")

# 无权重时的启发式打分：规则扣分权重 × 严重度，来自需求 §3.4 的经验排序。
# 它必须存在——这是 AGT-010「无模型降级」的落地点。
HEURISTIC_RULE_PRIOR = {
    "R1": 0.62, "R2": 0.70, "R3": 0.42, "R4": 0.74,
    "R5": 0.78, "R6": 0.30, "R7": 0.48,
}
HEURISTIC_ACTION_PRIOR = {
    "change_width": 0.85,   # 改线宽最稳：单参数、可撤销、不引入新几何
    "reroute": 0.55,        # 绕行收益大但可能引入新违规
    "add_route": 0.45,
    "add_jumper": 0.50,
    "move_comp": 0.35,      # 动器件影响面最大，放最后
}

# 模型输出在最终打分中的权重。0.5 来自留一场景交叉验证：
# 纯模型 0.90 / 纯先验 0.78 / 融合 0.925（见 docs 与 train 的 holdout 记录）。
MODEL_WEIGHT = 0.5


def heuristic_score(proposal: dict) -> float:
    """无模型时的排序依据：规则先验 × 动作先验 × 严重度。"""
    issue = proposal.get("issue") or {}
    sev = float(issue.get("severity", 1) or 1)
    r = HEURISTIC_RULE_PRIOR.get(issue.get("rule_code", ""), 0.40)
    a = HEURISTIC_ACTION_PRIOR.get(proposal.get("action", ""), 0.40)
    return r * a * (sev / 3.0)


class LocalModel:
    """岭回归打分器；`available=False` 表示正在用启发式降级。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else WEIGHTS_PATH
        self.weights: list[float] | None = None
        self.bias: float = 0.0
        self.meta: dict = {}
        self.load_error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.load_error = f"{type(e).__name__}: {e}"
            return
        if data.get("feature_version") != FEATURE_VERSION:
            self.load_error = (f"权重特征版本 {data.get('feature_version')} "
                               f"与本版 {FEATURE_VERSION} 不符，已降级")
            return
        w = data.get("weights")
        if not isinstance(w, list) or len(w) != len(FEATURE_NAMES):
            self.load_error = f"权重维度 {len(w) if isinstance(w, list) else '?'} 与特征维度 {len(FEATURE_NAMES)} 不符"
            return
        try:
            self.weights = [float(x) for x in w]
            self.bias = float(data.get("bias", 0.0))
        except (TypeError, ValueError) as e:
            self.load_error = f"权重含非数值：{e}"
            return
        self.meta = data.get("meta", {}) or {}

    @property
    def available(self) -> bool:
        return self.weights is not None

    def predict(self, feats: list[float]) -> float:
        """预测该修复带来的分数增量的一个单调代理量（越大越优先）。"""
        if not self.available:
            raise RuntimeError("本地模型不可用，请先调用 score()")
        if len(feats) != len(self.weights):
            raise ValueError(f"特征维度 {len(feats)} 与权重维度 {len(self.weights)} 不符")
        return self.bias + sum(w * f for w, f in zip(self.weights, feats, strict=True))

    def score(self, proposal: dict, feats: list[float]) -> float:
        """统一打分入口：模型输出与规则先验按固定比例融合。

        **为什么融合而不是纯模型**：留一场景交叉验证（train.py 报告）显示，
        纯模型在没见过的板型上会因特征分布外推而退化（LOSO 单独模型 0.90、
        单独先验 0.78），50/50 融合后升到 0.925。模型给出细粒度排序，
        先验在模型外推失准时兜底。
        """
        if not self.available:
            return heuristic_score(proposal)
        raw = self.predict(feats)
        # 模型输出是"分数量纲"，先压到 0..1 再与先验（天然 0..1）融合
        model_part = max(0.0, min(1.0, raw / 10.0))
        return MODEL_WEIGHT * model_part + (1.0 - MODEL_WEIGHT) * heuristic_score(proposal)

    def describe(self) -> dict:
        return {"available": self.available,
                "source": "local-ridge" if self.available else "heuristic",
                "feature_version": FEATURE_VERSION,
                "weights_path": str(self.path),
                "load_error": self.load_error,
                **{k: self.meta.get(k) for k in
                   ("trained_at", "samples", "mae", "r2") if k in self.meta}}


_DEFAULT: LocalModel | None = None


def default_model() -> LocalModel:
    """进程内单例：权重文件只在首次调用时读一次。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LocalModel()
    return _DEFAULT


def reset_default_model() -> None:
    """测试用：丢弃单例，让下次调用重新读盘（权重可注入）。"""
    global _DEFAULT
    _DEFAULT = None


__all__ = ["LocalModel", "default_model", "reset_default_model",
           "heuristic_score", "WEIGHTS_PATH", "HEURISTIC_RULE_PRIOR",
           "HEURISTIC_ACTION_PRIOR"]
