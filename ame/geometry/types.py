"""几何数据类型（STEP 面 / 模型容器）。

从 `stepio.py` 拆出：类型被解析器、装配展平、面分析、测试共同使用，
独立后各模块可以只依赖"数据形状"，不依赖解析实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Face:
    fid: str
    surf: str                      # plane / cylinder / other
    normal: tuple
    area: float
    centroid: tuple
    loop: list                     # 主外环顶点 [(x,y,z),...]
    radius: float | None = None
    diagnostics: list = field(default_factory=list)
    holes: list = field(default_factory=list)   # 内环（孔洞）顶点列表的列表


@dataclass
class StepModel:
    unit: str = "mm"
    faces: list = field(default_factory=list)
    bbox: tuple = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # xmin..zmax
    diagnostics: list = field(default_factory=list)   # 工程级诊断
    solid_count: int = 0
    volume_mm3: float = 0.0     # 有向体积绝对值（散度定理；封闭实体才有意义）

    def summary(self) -> str:
        return (f"{self.solid_count} 实体 / {len(self.faces)} 面 / 单位 {self.unit} / "
                f"包围盒 {self.bbox[0]:.1f}~{self.bbox[3]:.1f} × {self.bbox[1]:.1f}~{self.bbox[4]:.1f} "
                f"× {self.bbox[2]:.1f}~{self.bbox[5]:.1f} mm")
