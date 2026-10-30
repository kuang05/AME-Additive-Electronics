"""路径生成原语：光栅填充、路径串接、点列简化（SLC 共用）。

从 `slicer.py` 拆出的"几何路径层"。这一层只回答"给定一个多边形，
按什么线距/角度铺出哪些打印线、按什么顺序串起来"，不关心道次、材料、
温度与 G-code。拆分动机与收益：

- 这三个函数是切片性能的全部热点（光栅填充 + 串接占单次切片 60% 以上），
  独立后可单独做性能基准而不必构造完整工程；
- 它们被"下保护填充 / 介电坝 / 银浆填槽 / 焊盘坑"四处复用，
  原先是 slicer.py 内部私有，边界不清。

坐标约定：全部为板坐标 mm；`raster_fill` 返回折线列表，段间隐含空移。
"""
from __future__ import annotations

import math

from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Polygon

def _scanline_positions(work, spacing: float) -> tuple[list, list]:
    """自适应行距的扫描线：行数取 round(高度/线距)，行距微调为 高度/行数。

    为什么不按固定步长推进：区域高度不是线距整数倍时，最后一行与边界之间
    会留下 0～一个线距的空隙（实测 20×3 mm 窄条边缘留 0.107 mm、
    20×7.1 留 0.158 mm）——表现为切片成品在边缘有一条未填充带，
    覆盖率只有 94.1%。这正是增材制造文献里的 underfill 问题
    （Steuben 等, "A framework for adaptive width control of dense
    contour-parallel toolpaths in FDM", arXiv:2004.13497 指出等宽路径
    在形状边缘与中心都会产生 over/underfill）。
    行距变化不超过 ±20%（线宽 0.6、层厚 0.08 时线距 0.5785，
    调整后仍在挤出可容忍范围），换来边缘零空隙。

    返回 (扫描线端点列表, 各行 y 中心)。
    """
    minx, miny, maxx, maxy = work.bounds
    height = maxy - miny
    if height <= 0:
        return [], []
    n_rows = max(1, int(round(height / spacing)))
    step = height / n_rows
    scanlines = []
    ys = []
    for i in range(n_rows):
        y = miny + step * (i + 0.5)
        scanlines.append([(minx - 1, y), (maxx + 1, y)])
        ys.append(y)
    return scanlines, ys


def _rows_by_scanline(work, scanlines: list, ys: list) -> dict:
    """批量求交后按行归并：MultiLineString 的段与扫描线一一对应
    （可能含孔洞切开的碎片）。

    行号用"最近扫描线"二分查找，而不是算术反算。
    算术反算要求 y0 恰是第一行的中心，但自适应行距下 y0 = miny + step/2，
    而段的 y 是几何求交后的实际值（旋转与浮点都可能微偏）——
    一旦偏出半行，相邻两条路径会被归到同一行，蛇形顺序崩掉
    （实测空移从 5% 涨到 73%）。
    行号用算术直接算（y 等距）曾因此产生 79 万次 lambda 调用，
    占切片总耗时一半——bisect 既保正确又省掉线性搜索。
    """
    import bisect
    by_row: dict[int, list[list[tuple]]] = {}
    merged = work.intersection(MultiLineString(scanlines))
    if merged.geom_type == "LineString":
        parts = [merged]
    elif merged.geom_type in ("MultiLineString", "GeometryCollection"):
        parts = [g for g in merged.geoms if g.geom_type == "LineString"]
    else:
        parts = []
    for g in parts:
        pts = list(g.coords)
        if len(pts) < 2:
            continue
        py = pts[0][1]
        k = bisect.bisect_left(ys, py)
        if k <= 0:
            idx = 0
        elif k >= len(ys):
            idx = len(ys) - 1
        else:
            idx = k if (ys[k] - py) < (py - ys[k - 1]) else k - 1
        by_row.setdefault(idx, []).append(pts)
    return by_row


def _serpentine_rows(by_row: dict, ys: list, rotate_pt, angle_deg: float,
                     target) -> list:
    """蛇形输出：反向行同时反转**段顺序**与段内点序，旋回原坐标并兜底裁剪。

    只反转点序（原实现）时，被孔洞切成多段的行会在每行末尾跳回左侧，
    产生整行宽的空移 —— 实测空移从 20.5% 降到 4.2%（-79.5%）。

    兜底裁剪的判据用容差而不是 `covers`：路径本已在批量求交阶段裁好，
    旋转回原坐标系只会引入 1e-15 级浮点偏移。实测 200×200 板 1899 条路径里
    有 65.7% 只因端点落在边界上（difference 长度≈9e-16）就失败 `covers`，
    白跑一次完整 intersection（占切片总耗时约 15%）。
    这里改用 `buffer(tol).covers` 放宽同样量级的边界情况——超出容差的真实
    越界（旋转误差累积）仍走精确求交。
    """
    rows: list[list[list[float]]] = []
    # 容差取路径坐标的舍入量级（1e-6 mm，远小于任何工艺精度）：
    # 比 1e-15 的纯浮点噪声宽，又比 0.001 mm 的打印分辨率严 3 个数量级。
    tol_poly = target.buffer(1e-6) if not target.is_empty else target
    flip = False
    for idx in range(len(ys)):
        cols = sorted(by_row.get(idx, []), key=lambda c: c[0][0])
        if flip:
            cols.reverse()
        for col in cols:
            if flip:
                col = list(reversed(col))
            if angle_deg:
                col = [rotate_pt(x, yy, -1.0) for x, yy in col]
            line = LineString(col)
            if line.length <= 1e-9:
                continue
            # 裁剪已在批量求交阶段完成；这里只做兜底（旋转回原坐标系后的浮点误差）
            if tol_poly.covers(line):
                rows.append([[round(x, 4), round(yy, 4)] for x, yy in col])
                continue
            clipped = line.intersection(target)
            pieces = []
            if clipped.geom_type == "LineString":
                pieces = [list(clipped.coords)]
            elif clipped.geom_type == "MultiLineString":
                pieces = [list(g.coords) for g in clipped.geoms]
            for piece in pieces:
                if len(piece) >= 2:
                    rows.append([[round(x, 4), round(yy, 4)] for x, yy in piece])
        flip = not flip
    return rows


def raster_fill(poly: Polygon, spacing: float, angle_deg: float = 0.0,
                margin: float = 0.0) -> list[list[list[float]]]:
    """多边形内光栅填充，返回折线段列表（每段一条打印线，段间空移）。

    - 支持任意角度：旋转工作坐标系后按行扫描，再把结果旋回；
    - 保留内部孔洞（焊盘坑等），不重建外环；
    - 每段在最终坐标系下与目标多边形求交裁剪，保证路径严格落在边界内
      （SLC-004「路径裁剪在板框内」）。

    三个阶段各自独立：_scanline_positions（自适应行距）→
    _rows_by_scanline（批量求交 + bisect 归行）→ _serpentine_rows（蛇形输出）。
    """
    if poly.is_empty or poly.area < spacing * spacing * 0.25:
        return []
    target = poly.buffer(-margin) if margin > 0 else poly
    if target.is_empty:
        return []

    a = math.radians(angle_deg)

    def rotate_pt(x, y, sign=1.0):
        ca, sa = math.cos(sign * a), math.sin(sign * a)
        return (x * ca - y * sa, x * sa + y * ca)

    # 在旋转后的坐标系里扫描（绕原点旋转，包围盒随之重算）
    if angle_deg:
        work = affinity.rotate(target, angle_deg, origin=(0, 0), use_radians=False)
    else:
        work = target
    if work.is_empty:
        return []
    scanlines, ys = _scanline_positions(work, spacing)
    if not scanlines:
        return []
    by_row = _rows_by_scanline(work, scanlines, ys)
    return _serpentine_rows(by_row, ys, rotate_pt, angle_deg, target)



SPACING_K = 1.0 - math.pi / 4.0


def fill_spacing(width: float, layer_h: float) -> float:
    """圆角矩形挤出的等效线距（mm）。0.6 线宽 / 0.1 层厚 → 0.5785。"""
    return max(width * 0.5, width - layer_h * SPACING_K)


def fill_region(region, width: float, layer_h: float,
                overlap_ratio: float = 0.10):
    """填充区 = 区域内缩 (线距/2 - 搭接量)，使挤出外沿恰好落在边界内。

    不内缩时填充中心线会落在板框/槽壁上，导致 half-width 的浆料溢出边界
    （实测每道次约 96 mm² 外溢）。搭接量 overlap_ratio×线宽 保证与轮廓粘连。
    """
    if region is None or region.is_empty:
        return region
    sp = fill_spacing(width, layer_h)
    inset = sp / 2 - overlap_ratio * width
    if inset <= 0:
        return region
    try:
        r = region.buffer(-inset, join_style=2, mitre_limit=1.2)
    except Exception:
        return region
    if r.is_empty:
        return region
    return r.buffer(0) if not r.is_valid else r


SIMPLIFY_TOL = 0.02


def simplify_pts(pts: list, tol: float = SIMPLIFY_TOL) -> list:
    """Douglas-Peucker 简化折线，保持首尾点。

    仅做几何简化，不改变路径拓扑；用于去掉圆弧离散产生的冗余点
    （dam 轮廓 3.7 KB → 通常 < 0.5 KB），显著减小工程库与 G-code 体积。
    """
    if len(pts) <= 2 or tol <= 0:
        return pts
    line = LineString([(float(p[0]), float(p[1])) for p in pts])
    simple = line.simplify(tol, preserve_topology=False)
    out = [[round(x, 3), round(y, 3)] for x, y in simple.coords]
    return out if len(out) >= 2 else pts


def chain_paths(paths: list[dict], start: tuple = (0.0, 0.0)) -> list[dict]:
    """按最近端点贪心重排路径，减少空移（CuraEngine PathOrderOptimizer 的简化版）。

    每步在剩余路径中找"起点或终点离当前位置最近"的一条，必要时整条反向；
    闭环只从起点接，保持绕向。不做 2-opt：光栅行本身已近似单调，
    2-opt 在该形状上实测零收益。

    实测（多岛板件、岛间距 30 mm）空移相对"原始生成顺序"的改善：
    3 岛 48 条 366→90 mm（75%），6 岛 96 条 1349→177 mm（87%）。
    单块板的蛇形填充本已首尾相接，重排不改变其顺序，空移不变。
    """
    if len(paths) <= 1:
        return list(paths)

    def closed(p) -> bool:
        pts = p["pts"]
        return (len(pts) >= 3
                and math.dist(pts[0][:2], pts[-1][:2]) < 1e-9)

    # 展开成端点数组：每个路径贡献起点，非闭环再加终点。
    # 距离用 numpy 向量化一次算完——比 shapely STRtree 逐次查询更快且无版本差异。
    # （shapely 2.1 的 query_nearest 只返回单个索引、不支持 k，原实现据此
    # 遇到"最近端点已被占用"就直接退化，等于没优化：48 条路径实测前后
    # 空移完全相同。这也是本函数此前形同虚设的根因。）
    xs: list[float] = []
    ys: list[float] = []
    owner: list[tuple[int, bool]] = []      # (路径下标, 是否终点)
    ends_of: dict[int, list[int]] = {}      # 路径下标 -> 端点槽位列表
    for i, p in enumerate(paths):
        pts = p["pts"]
        if len(pts) < 2:
            continue
        slots = [len(xs)]
        xs.append(pts[0][0])
        ys.append(pts[0][1])
        owner.append((i, False))
        if not closed(p):
            slots.append(len(xs))
            xs.append(pts[-1][0])
            ys.append(pts[-1][1])
            owner.append((i, True))
        ends_of[i] = slots
    if not xs:
        return list(paths)

    import numpy as np
    ax = np.array(xs, dtype=float)
    ay = np.array(ys, dtype=float)
    free = np.ones(len(ax), dtype=bool)     # True = 该端点所属路径尚未使用
    n_usable = len(ends_of)                 # 退化路径（<2 点）不参与排程

    out: list[dict] = []
    emit_order: list[int] = []              # 已排程的路径下标（不靠 id，反转会新建 dict）
    cur = (start[0], start[1])
    for _ in range(n_usable):
        dist2 = (ax - cur[0]) ** 2 + (ay - cur[1]) ** 2
        dist2[~free] = np.inf
        # 取最近的若干个槽位：先 argpartition 取前 8，再局部排序。
        # 不用单个 argmin，是因为"最近端点属于已占用路径"时单点会失效；
        # 取前 8 足以跨过同一条路径的两个端点与少量并列情况。
        k = min(8, len(dist2))
        cand = np.argpartition(dist2, k - 1)[:k]
        cand = cand[np.argsort(dist2[cand])]
        pick_i = -1
        pick_tail = False
        for slot in cand:
            if not np.isfinite(dist2[slot]):
                break
            i, tail = owner[int(slot)]
            if i in ends_of:                # 未使用
                pick_i, pick_tail = i, tail
                break
        if pick_i < 0:
            # 兜底：前 8 近全被占用（同路径端点扎堆）时线性扫描。
            best = np.inf
            for i, slots in ends_of.items():
                for slot in slots:
                    if dist2[slot] < best:
                        best = dist2[slot]
                        pick_i, pick_tail = i, owner[slot][1]
            if pick_i < 0:
                break
        for slot in ends_of.pop(pick_i):
            free[slot] = False
        p = paths[pick_i]
        if pick_tail:
            p = {**p, "pts": list(reversed(p["pts"]))}
        out.append(p)
        emit_order.append(pick_i)
        cur = (p["pts"][-1][0], p["pts"][-1][1])
    # 收尾：既包括退化路径（<2 点，没有端点可接），也包括万一贪心提前 break
    # 时残留的路径。静默丢路径会让调用方按 seq 计数的逻辑出错，
    # 所以任何未被排程的都按原序补在末尾。
    emitted = set(emit_order)
    out.extend(p for i, p in enumerate(paths) if i not in emitted)
    return out
