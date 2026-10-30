"""AI 辅助层（AGT-012 / 赛题"运用华为人工智能技术"）.

设计约束（与 tests 强一致）::

- 核心流程**不依赖**本包：删除 `ame/ai` 后四域链路与设计 Agent 仍完整可用
  （AGT-010 无模型降级）。
- 本包默认路径是**本地推理**，无网络、无账号即可运行（NFR-007 离线）。
- 云端（华为云 ModelArts / 盘古）只在用户开启且注入 transport 时才被调用；
  `ame/` 内不出现任何网络客户端库依赖（DAT-007 隐私 / NFR-005 只绑本机）。
- 训练产物是可复现的：`python -m ame.ai.train` 用真实工程跑出数据集，
  权重与指标一起落盘，供验收时复核。

三层能力::

    features.py  问题 → 定长特征向量（纯数据，不含模型）
    local.py     本地模型：加载权重、预测修复收益（离线可用）
    cloud.py     云端适配器：可注入 transport，失败自动降级到本地
    train.py     训练脚本：真实工程采集标签 → 拟合 → 写权重与指标
    service.py   对界面/Agent 的统一门面（排序、解释、缺陷归因增强）
"""
from __future__ import annotations

from .service import (ai_status, explain_issue_ai, rank_proposals,
                      suggest_defect_cause)

__all__ = ["ai_status", "explain_issue_ai", "rank_proposals",
           "suggest_defect_cause"]
