"""各功能域的动作 mixin（由 App 多重继承）。

拆分原则：每个文件对应一个需求模块（结构/走线/可打性/切片/打印/
智能体/工程），方法体逐字从原 app.py 搬运，不做逻辑改写。
"""
from __future__ import annotations

from .agent import AgentMixin
from .circuit import CircuitMixin
from .mrc import MrcMixin
from .printing import PrintingMixin
from .project import ProjectMixin
from .slicing import SlicingMixin
from .structure import StructureMixin

__all__ = ["AgentMixin", "CircuitMixin", "MrcMixin", "PrintingMixin",
           "ProjectMixin", "SlicingMixin", "StructureMixin"]
