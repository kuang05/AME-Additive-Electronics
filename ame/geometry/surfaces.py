"""曲面离散与解析面积（圆锥 / 球面 / 环面）。

从 `stepio.py` 拆出的"曲面层"。为什么单独成模块：

- 这一层是纯数学：输入是参数方程与环点列，输出是 `Face` 的顶点环与解析面积，
  不读实体表、不认 STEP 语法——与解析流程耦合度最低，最好独立测试；
- 面积必须用**解析式**（项目硬约束：有测试锁定"圆锥环从 8 点加密到 96 点，
  面积完全相等"）。把公式集中在这里，改公式时不会误伤解析逻辑；
- 早期这些函数混在 `stepio.py` 里，该文件一度涨到 1384 行、`read_step` 单函数
  507 行，定位"某个曲面的面积公式"要翻遍整个解析器。

坐标与单位：与 `stepio` 一致（模型单位，通常是 mm）；法向为单位向量。
"""
from __future__ import annotations

import math

from .vec3 import (_v3, centroid_3d, v_add, v_cross, v_dot, v_len,
                   v_mul, v_sub, v_unit)

def _ref_dir(ref, axis: tuple) -> tuple:
    """把 AXIS2_PLACEMENT 的 x 轴投影成"垂直于 axis 的单位向量"（方位基准）。

    参考方向可能缺失、为零、或与轴平行（真实导出里都见过）。此时按轴选一个
    固定的世界方向兜底 —— 只用它建立方位角，不影响面积与法向的绝对值。
    """
    a = v_unit(axis)
    d = v_sub(_v3(ref), v_mul(a, v_dot(_v3(ref), a))) if ref is not None else (0.0, 0.0, 0.0)
    if v_len(d) < 1e-9:
        alt = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
        d = v_sub(alt, v_mul(a, v_dot(alt, a)))
    return v_unit(d)


def _angle_sweep(angles: list[float]) -> tuple[float, float]:
    """从一组方位角估计"扇区扫过的角度与起始角"，返回 (sweep, start)，sweep ∈ (0, 2π]。

    做法：去重后排序，取相邻间隙（含跨 2π 的那一段）。均匀离散的整圈只会在
    "最后一点回到起点"处留下一个等于步长的间隙，所以
    「最大间隙 ≤ 2.5×中位间隙」判为整圈；否则整圈减去最大间隙就是真实扇区。

    去重是必须的：矩形面片的边界沿一条等参数边会有大量**相同**角度，
    不去重时相邻间隙中出现大量 0，中位间隙变成 0，会误判成整圈（实测
    1/4 环面被算成整环，面积大 3.5 倍）。
    只依赖排序与比较，不含随机、也不依赖集合遍历顺序，因此解析可重放。
    """
    if not angles:
        return 0.0, 0.0
    # 角度去重（保留 1e-9 rad 内视为同一点）；sorted 保证与输入顺序无关
    uniq: list[float] = []
    for a in sorted(angles):
        if not uniq or a - uniq[-1] > 1e-9:
            uniq.append(a)
    if len(uniq) == 1:
        # 单个不同角度无法判断扇区，按整圈处理（面积宁可按整圈，配合 APPROX 诊断）
        return 2 * math.pi, uniq[0]
    if len(uniq) == 2:
        # 只有两个不同角度不可能是"整圆"（整圆至少 3 个不共线顶点），
        # 只能是踏面（如圆锥侧面上的一块四边形片）。取较小弧；近似性由调用方标注。
        d = uniq[1] - uniq[0]
        return (d, uniq[0]) if d <= math.pi else (2 * math.pi - d, uniq[1])
    gaps = [uniq[i + 1] - uniq[i] for i in range(len(uniq) - 1)]
    gaps.append(2 * math.pi - (uniq[-1] - uniq[0]))
    gaps = [g if g > 0.0 else 0.0 for g in gaps]
    mx = max(gaps)
    if mx <= 1e-12:
        return 2 * math.pi, uniq[0]
    med = sorted(gaps)[len(gaps) // 2]
    if med <= 1e-12 or mx <= 2.5 * med:
        return 2 * math.pi, uniq[0]
    # 扇区起点 = 最大间隙的终点（跨 2π 时归一到 [0,2π)）
    k = gaps.index(mx)
    start = uniq[0] if k == len(gaps) - 1 else uniq[k + 1]
    return max(0.0, 2 * math.pi - mx), start


def _surface_type_name(ent: dict, surf_ref, complex_parts: dict | None,
                       complex_types: dict | None = None) -> str:
    """曲面实体对用户有意义的类型名（诊断信息用）。

    复杂实例 #248=(BOUNDED_SURFACE() B_SPLINE_SURFACE(1,3,(...))) 在词法阶段只
    保留第一个子实体，直接报 "BOUNDED_SURFACE" 没有信息量；优先取含 SPLINE 的
    子类型（complex_types 由 _parse_entities 用不依赖嵌套括号的正则收集）。
    """
    names = (complex_types or {}).get(surf_ref) or []
    for name in names:
        if "SPLINE" in name:
            return name
    for sub in (complex_parts or {}).get(surf_ref) or []:
        if sub and "SPLINE" in sub[0]:
            return sub[0]
    e = ent.get(surf_ref) if surf_ref is not None else None
    return e[0] if e else "?"


def _fallback_face(loop_pts: list[tuple], normal) -> tuple:
    """无法求值的曲面（样条/自由曲面）的保守面积估算。

    面积取「环的轴对齐包围盒面积」与「环的三角形扇面积」中的**较小值**：
    宁可低估也不夸大——面积被下游用于统计与渲染分级，夸大会把无效面
    误当成大面保留，负面影响更大。返回值第三项保留原法向（不做猜测）。
    """
    if len(loop_pts) < 3:
        return 0.0, centroid_3d(loop_pts or [(0, 0, 0)]), normal
    xs = [p[0] for p in loop_pts]
    ys = [p[1] for p in loop_pts]
    bbox = (max(xs) - min(xs)) * (max(ys) - min(ys))
    p0 = loop_pts[0]
    fan = 0.0
    for i in range(1, len(loop_pts) - 1):
        fan += v_len(v_cross(v_sub(loop_pts[i], p0),
                             v_sub(loop_pts[i + 1], p0))) / 2.0
    area = min(bbox, fan)
    return area, centroid_3d(loop_pts), normal


def _cone_face(loop_pts: list[tuple], axis, loc, r0, sparams) -> tuple:
    """圆锥/圆台侧面：解析面积 π(r1+r2)·l（l 为母线长），返回 (area, cen, normal, approx)。

    STEP 的 CONICAL_SURFACE(position, radius, semi_angle) 里 radius 是"过 position
    原点、垂直于轴"那一圈的半径，半锥角是与**轴**的夹角。于是：
      t    = (q - loc)·axis                       轴向坐标
      R(t) = r0 + t·tan(α)                        该高度处的半径
      l    = (t2-t1)/cos(α)                       母线长
      A    = π(R1+R2)·l · Δφ/2π                   圆台侧面积
    弦高容差与离散段数都不参与面积计算 —— 面积只由环的轴向范围与方位扇区决定。
    """
    if r0 is None or r0 < 0.0 or not sparams or len(loop_pts) < 3:
        return 0.0, centroid_3d(loop_pts or [(0, 0, 0)]), axis, False
    ax = v_unit(axis)
    # semi_angle 在 STEP 里可正可负：负值表示半径沿 +axis 递减。用绝对值判退化，
    # 但半径与法向必须用带符号值，否则外侧圆锥的法向会朝下（坡度符号错误）。
    semi_signed = float(sparams.get("semi") or 0.0)
    semi = abs(semi_signed)
    # 半锥角趋近 90° 时 tan/cos 发散，几何已退化成平面，交给包围盒估算
    if semi >= math.radians(89.0):
        return 0.0, centroid_3d(loop_pts), ax, False
    e1 = _ref_dir(sparams.get("x"), ax)
    e2 = v_cross(ax, e1)
    ts, angles = [], []
    for q in loop_pts:
        d = v_sub(q, loc)
        t = v_dot(d, ax)
        ts.append(t)
        radial = v_sub(d, v_mul(ax, t))
        angles.append(math.atan2(v_dot(radial, e2), v_dot(radial, e1)))
    t1, t2 = min(ts), max(ts)
    if t2 - t1 <= 1e-9 * max(1.0, abs(t1), abs(t2)):
        # 环全部落在同一高度（如只有一个圆边界）→ 无法界定母线，返回 0 让调用方回退
        return 0.0, centroid_3d(loop_pts), ax, False
    tan_s = math.tan(semi_signed)
    r1, r2 = r0 + t1 * tan_s, r0 + t2 * tan_s
    if r1 < -1e-9 or r2 < -1e-9:
        # 环越过了锥顶：半径符号翻转说明面片几何与曲面参数不自洽
        return 0.0, centroid_3d(loop_pts), ax, False
    slant = (t2 - t1) / math.cos(semi)
    sweep, start = _angle_sweep(angles)
    if sweep <= 0.0:
        return 0.0, centroid_3d(loop_pts), ax, False
    area = math.pi * (abs(r1) + abs(r2)) * slant * sweep / (2 * math.pi)
    # 代表点：轴向与方位都取中点。
    # 外法向推导：P(t,φ)=loc+t·ax+R(t)·er，eφ×ax=er、eφ×er=-ax，
    # 故 n ∝ cos(α)·er - sin(α)·ax（α 带符号，负锥角时法向上翘）。
    t_mid = (t1 + t2) / 2.0
    phi = start + sweep / 2.0
    r_mid = r0 + t_mid * tan_s
    er = v_add(v_mul(e1, math.cos(phi)), v_mul(e2, math.sin(phi)))
    cen = v_add(v_add(loc, v_mul(ax, t_mid)), v_mul(er, r_mid))
    normal = v_unit(v_sub(v_mul(er, math.cos(semi)),
                          v_mul(ax, math.sin(semi_signed))))
    # 轴为 +Z 时 nz = -sin(α)，下游 slope=asin(|nz|)=|α|，即法向与水平面夹角；
    # 与平面/柱面共用同一 slope 语义（MRC R3 直接读）。
    approx = sweep < 2 * math.pi - 1e-6
    return area, cen, normal, approx


def _sphere_face(loop_pts: list[tuple], axis, loc, r, sparams=None) -> tuple:
    """球面（球带/球冠）：解析面积，返回 (area, cen, normal, approx)。

    纬带面积：A = r²·(cosθ1 - cosθ2)·Δφ，θ 是距 +axis 极点的极角。
    整个球面 θ∈[0,π]、Δφ=2π → 4πr²，与解析式一致，不依赖三角网格累加。

    单纬环（只有一条圆边界）无法判断面在纬线的**哪一侧**（球被平面切开时，
    两半的边界圆完全相同），此时按较小的球冠给面积并标记近似。
    """
    if not r or r <= 0.0 or len(loop_pts) < 3:
        return 0.0, centroid_3d(loop_pts or [(0, 0, 0)]), axis, False
    ax = v_unit(axis)
    e1 = _ref_dir((sparams or {}).get("x"), ax)
    e2 = v_cross(ax, e1)
    thetas, angles = [], []
    for q in loop_pts:
        d = v_sub(q, loc)
        c = max(-1.0, min(1.0, v_dot(d, ax) / r))
        thetas.append(math.acos(c))
        radial = v_sub(d, v_mul(ax, v_dot(d, ax)))
        angles.append(math.atan2(v_dot(radial, e2), v_dot(radial, e1)))
    th1, th2 = min(thetas), max(thetas)
    sweep, start = _angle_sweep(angles)
    if sweep <= 0.0:
        return 0.0, centroid_3d(loop_pts), ax, False
    single_lat = (th2 - th1) <= 1e-6
    if single_lat:
        # 侧别不可判定 → 取面积较小的球冠（保守），并标记近似
        th = th2
        frac = min(1.0 - math.cos(th), 1.0 + math.cos(th)) / 2.0
        area = 4 * math.pi * r * r * frac * sweep / (2 * math.pi)
        th_mid = th / 2.0 if math.cos(th) >= 0 else (th + math.pi) / 2.0
    else:
        area = r * r * (math.cos(th1) - math.cos(th2)) * sweep
        th_mid = (th1 + th2) / 2.0
    phi = start + sweep / 2.0
    st, ct = math.sin(th_mid), math.cos(th_mid)
    er = v_add(v_mul(e1, st * math.cos(phi)), v_mul(e2, st * math.sin(phi)))
    radial_dir = v_add(er, v_mul(ax, ct))            # 单位球面外法向
    cen = v_add(loc, v_mul(radial_dir, r))
    approx = single_lat or sweep < 2 * math.pi - 1e-6
    return area, cen, v_unit(radial_dir), approx


def _torus_face(loop_pts: list[tuple], axis, loc, minor_r, sparams) -> tuple:
    """环面：按 (u,v) 参数范围积分面积，返回 (area, cen, normal)。

    dA = (R + r·cos v)·r·dv·du，在 v∈[v0, v0+Δv]、u 扫过 Δu 上积分得
      A = Δu·r·[ R·Δv + r·(sin(v0+Δv) - sin(v0)) ]
    整环（Δu=Δv=2π）退化为 4π²Rr，与经典公式一致。

    参数范围由边界点反解（v = atan2(t, ρ-R)、u = 方位角）后用间隙法估计，
    是**启发式**，因此本函数的结果一律由调用方标记 G-SURFACE-APPROX。
    """
    major = float((sparams or {}).get("major") or 0.0)
    r = float(minor_r or 0.0)
    if major <= 0.0 or r <= 0.0 or len(loop_pts) < 3:
        return 0.0, centroid_3d(loop_pts or [(0, 0, 0)]), axis
    ax = v_unit(axis)
    e1 = _ref_dir((sparams or {}).get("x"), ax)
    e2 = v_cross(ax, e1)
    us, vs = [], []
    for q in loop_pts:
        d = v_sub(q, loc)
        t = v_dot(d, ax)
        radial = v_sub(d, v_mul(ax, t))
        rho = v_len(radial)
        # 单位量纲：sin v ∝ t、cos v ∝ (ρ-R)，atan2 不需要除以 r
        vs.append(math.atan2(t, rho - major))
        us.append(math.atan2(v_dot(radial, e2), v_dot(radial, e1)))
    du, u0 = _angle_sweep(us)
    dv, v0 = _angle_sweep(vs)
    if du <= 0.0 or dv <= 0.0:
        return 0.0, centroid_3d(loop_pts), ax
    area = du * r * (major * dv + r * (math.sin(v0 + dv) - math.sin(v0)))
    if area < 0.0:
        area = 0.0
    u_mid = u0 + du / 2.0
    v_mid = v0 + dv / 2.0
    er = v_add(v_mul(e1, math.cos(u_mid)), v_mul(e2, math.sin(u_mid)))
    cen = v_add(loc, v_add(v_mul(er, major + r * math.cos(v_mid)),
                           v_mul(ax, r * math.sin(v_mid))))
    normal = v_unit(v_add(v_mul(er, math.cos(v_mid)), v_mul(ax, math.sin(v_mid))))
    return area, cen, normal


def _ring_area(ring: list[tuple]) -> float:
    """环形顶点的近似面积（用鞋带公式，投影到主平面）。"""
    if len(ring) < 3:
        return 0.0
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    zs = [p[2] for p in ring]
    # 选跨度最大的两个轴构成投影平面
    spans = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
    drop = spans.index(min(spans))
    axes = [i for i in range(3) if i != drop]
    a = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i][axes[0]], ring[i][axes[1]]
        x2, y2 = ring[(i + 1) % len(ring)][axes[0]], ring[(i + 1) % len(ring)][axes[1]]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2

