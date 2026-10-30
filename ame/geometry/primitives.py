"""几何基元：焊盘 / 走线 / 器件的统一几何构造（CIR/MRC/SLC 共用）。

原先 connectivity、mrc.engine、slicing.slicer 各写一份焊盘几何，且只有
connectivity 支持旋转焊盘——切片与 MRC 会把旋转焊盘当轴对齐矩形算错。
本模块是唯一实现，三处一律调用这里。

坐标约定：全部为板坐标 mm，二维（x, y）；焊盘 rot 为角度制。
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon, box

# 焊盘缺省尺寸（与 board 文件导入缺省一致）
DEFAULT_PAD_W = 1.5
DEFAULT_PAD_H = 1.0
# 器件本体缺省外框（导入器未给出尺寸时的保守估计）
DEFAULT_COMP_SIZE = 3.0
# 圆形/圆弧离散精度（quad_segs）
ARC_SEGS = 8


def pad_polygon(pad: dict, inflate: float = 0.0) -> Polygon:
    """焊盘几何。

    - circle：按外接直径做圆（max(w,h)）；
    - rect / oval：轴对齐矩形，按 ``rot``（角度）绕中心旋转；
    - ``inflate`` > 0 时径向/四周各外扩 inflate/2（切片留坑、间距判定用）。
    """
    w = float(pad.get("w", DEFAULT_PAD_W))
    h = float(pad.get("h", DEFAULT_PAD_H))
    x, y = float(pad["x"]), float(pad["y"])
    shape = pad.get("shape", "rect")
    if shape == "circle":
        return Point(x, y).buffer((max(w, h) + inflate) / 2, quad_segs=ARC_SEGS)
    half_w = (w + inflate) / 2
    half_h = (h + inflate) / 2
    rot = math.radians(float(pad.get("rot", 0.0) or 0.0))
    if abs(rot) < 1e-9:
        return box(x - half_w, y - half_h, x + half_w, y + half_h)
    ca, sa = math.cos(rot), math.sin(rot)
    corners = [(x - half_w, y - half_h), (x + half_w, y - half_h),
               (x + half_w, y + half_h), (x - half_w, y + half_h)]
    pts = [(x + ca * (px - x) - sa * (py - y), y + sa * (px - x) + ca * (py - y))
           for px, py in corners]
    return Polygon(pts)


def route_polygon(pts: list, width: float):
    """走线导体区：中心线按 width/2 加宽的带状。少于 2 点返回 None。"""
    xy = [(float(p[0]), float(p[1])) for p in pts]
    if len(xy) < 2:
        return None
    return LineString(xy).buffer(float(width) / 2, quad_segs=ARC_SEGS)


def route_line(pts: list):
    """走线中心线；少于 2 点返回 None（NFR-002：畸形数据不崩溃）。"""
    xy = [(float(p[0]), float(p[1])) for p in pts]
    if len(xy) < 2:
        return None
    return LineString(xy)


def comp_rect(comp: dict) -> Polygon:
    """器件本体外框（轴对齐）：用 size_dx/size_dy，缺省按 DEFAULT_COMP_SIZE。"""
    dx = float(comp.get("size_dx", DEFAULT_COMP_SIZE) or DEFAULT_COMP_SIZE)
    dy = float(comp.get("size_dy", DEFAULT_COMP_SIZE) or DEFAULT_COMP_SIZE)
    return box(comp["x"] - dx / 2, comp["y"] - dy / 2,
               comp["x"] + dx / 2, comp["y"] + dy / 2)


def board_polygon(outline: list) -> Polygon | None:
    """板框多边形；点数不足 3 返回 None。"""
    if not outline or len(outline) < 3:
        return None
    return Polygon([(float(p[0]), float(p[1])) for p in outline])


__all__ = ["pad_polygon", "route_polygon", "route_line", "comp_rect",
           "board_polygon", "DEFAULT_PAD_W", "DEFAULT_PAD_H", "DEFAULT_COMP_SIZE"]
