"""三维向量与多边形几何原语（STEP 解析共用）。

从 `stepio.py` 拆出的纯函数层：只依赖 math，不依赖解析状态。
拆分动机：`stepio.py` 曾 1644 行，向量运算与 STEP 实体解析混在一起，
修改任何一处都要在千行文件里定位；这些函数在解析、装配展平、网格化
三处复用，独立成模块后边界清楚，也便于单测。
"""
from __future__ import annotations

import math

def v_sub(a, b) -> tuple: return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_add(a, b) -> tuple: return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_mul(a, s) -> tuple: return (a[0] * s, a[1] * s, a[2] * s)


def _v3(a) -> tuple:
    """补齐到 3 维（真实 STEP 里 DIRECTION 参数可能少于 3 个）。

    **热路径**：v_dot/v_cross/v_sub 都对每个操作数调一次，12 MB 模型上
    实测 57.6 万次调用、0.97 s。优化点：

    - 早退：已是长度 3 的 tuple 且元素都是 float 时直接返回，不重建；
    - 用 try/IndexError 代替 `i < len(a)` 判断（Python 里异常开销仅在
      真正越界时支付，而越界是少数）；
    - 去掉中间的 list，直接构造 tuple（省一次分配）。

    结果与旧实现完全一致：不足 3 维补 0.0，None 返回零向量。
    """
    if a is None:
        return (0.0, 0.0, 0.0)
    if type(a) is tuple and len(a) == 3 and type(a[0]) is float             and type(a[1]) is float and type(a[2]) is float:
        return a
    try:
        x = float(a[0])
    except IndexError:
        return (0.0, 0.0, 0.0)
    try:
        y = float(a[1])
    except IndexError:
        y = 0.0
    try:
        z = float(a[2])
    except IndexError:
        z = 0.0
    return (x, y, z)


def v_dot(a, b) -> float:
    a, b = _v3(a), _v3(b)
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_len(a) -> float: return math.sqrt(v_dot(a, a))


def v_unit_safe(a) -> tuple:
    return v_unit(_v3(a))


def v_unit(a) -> tuple:
    n = v_len(a)
    return (0.0, 0.0, 1.0) if n < 1e-12 else v_mul(a, 1.0 / n)


def v_cross(a, b) -> tuple:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def polygon_area_3d(pts: list[tuple], normal) -> float:
    n = v_unit(normal)
    p0 = pts[0]
    a = 0.0
    for i in range(1, len(pts) - 1):
        p1, p2 = pts[i], pts[i + 1]
        e1 = v_sub(p1, p0)
        e2 = v_sub(p2, p0)
        a += v_dot(v_cross(e1, e2), n)
    return abs(a) / 2.0


def centroid_3d(pts) -> tuple:
    return tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
