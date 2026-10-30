"""曲面解析（圆锥/球/环面/样条）的回归测试。

设计原则（与 ame/geometry/stepio.py 模块 docstring 一致）：

1. **面积一律解析式优先**，不依赖三角/多边形网格累加。因此这里的测试刻意用
   "不同密度的折线环"去逼近同一条曲面边界——解析面积必须**完全不变**。
   若哪天有人把面积改成网格累加，这两组密度的结果就会不同，测试立刻失败。
2. **诊断码要诚实**：圆锥/球面是解析解，不得再报 G-SURFACE-UNSUPPORTED；
   环面只做参数积分，必须报 G-SURFACE-APPROX；样条仍是不支持。
3. **确定性**：同一文本两次解析结果完全一致（切片可重放的前提）。

测试用 STEP 文本由本文件内的小构造器生成（只写 ADVANCED_FACE + 折线环），
避免引入新样本文件；参考 tests/test_structure.py 里手写 STEP 片段的写法。
"""
from __future__ import annotations

import math

import pytest

from ame.geometry.analysis import analyze_faces
from ame.geometry.stepio import read_step

HEADER = (
    "ISO-10303-21;\nHEADER;\n"
    "FILE_DESCRIPTION(('ame surface test'),'2;1');\n"
    "FILE_NAME('surf.step','2026-01-01',('ame'),('ame'),'','','');\n"
    "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
    "ENDSEC;\nDATA;\n"
    "#9=SI_UNIT(.MILLI.,.METRE.);\n"
)
FOOTER = "\nENDSEC;\nEND-ISO-10303-21;\n"


def _step(loop, surface_factory, origin=(0.0, 0.0, 0.0)):
    """用一条折线环 + 一个曲面实体构造最小 STEP 文件。

    surface_factory(add, placement_ref) -> 曲面实体 id。
    环用 LINE 边（折线）闭合：本解析器的曲面面积不读离散段数，折线足够。
    """
    ents: list[str] = []
    nid = [9]

    def add(body: str) -> int:
        nid[0] += 1
        ents.append(f"#{nid[0]}={body};")
        return nid[0]

    def pt(p):
        return add(f"CARTESIAN_POINT('',({p[0]:.9f},{p[1]:.9f},{p[2]:.9f}))")

    def dr(d):
        m = math.sqrt(sum(c * c for c in d)) or 1.0
        return add(f"DIRECTION('',({d[0] / m:.9f},{d[1] / m:.9f},{d[2] / m:.9f}))")

    pids = [pt(p) for p in loop]
    vids = [add(f"VERTEX_POINT('',#{i})") for i in pids]
    edge_ids = []
    for i in range(len(loop)):
        j = (i + 1) % len(loop)
        d = tuple(loop[j][k] - loop[i][k] for k in range(3))
        vec = add(f"VECTOR('',#{dr(d)},1.)")
        ln = add(f"LINE('',#{pids[i]},#{vec})")
        edge_ids.append(add(f"EDGE_CURVE('',#{vids[i]},#{vids[j]},#{ln},.T.)"))
    oes = [add(f"ORIENTED_EDGE('',*,*,#{e},.T.)") for e in edge_ids]
    lp = add(f"EDGE_LOOP('',({','.join('#%d' % o for o in oes)}))")
    bd = add(f"FACE_OUTER_BOUND('',#{lp},.T.)")
    # 曲面放置：原点在 origin，轴 +Z，参考方向 +X
    o = pt(origin)
    place = add(f"AXIS2_PLACEMENT_3D('',#{o},#{dr((0.0, 0.0, 1.0))},"
                f"#{dr((1.0, 0.0, 0.0))})")
    surf = surface_factory(add, place)
    add(f"ADVANCED_FACE('',(#{bd}),#{surf},.T.)")
    return HEADER + "\n".join(ents) + FOOTER


def _ring(r, z, n, start=0.0, sweep=2 * math.pi):
    """一圈圆上的 n 个点（折线近似）。"""
    return [(r * math.cos(start + sweep * k / n),
             r * math.sin(start + sweep * k / n), z) for k in range(n)]


def _frustum_loop(r0, semi_deg, h, n):
    """圆台侧面的一条闭合折线：底圈正序 + 顶圈反序。"""
    semi = math.radians(semi_deg)
    r1 = r0 + h * math.tan(semi)
    return _ring(r0, 0.0, n) + list(reversed(_ring(r1, h, n)))


def _codes(model) -> set[str]:
    return {d["code"] for d in model.diagnostics}


# ---------------------------------------------------------------- 圆锥面
def test_cone_area_is_analytic_frustum_formula():
    """圆锥（圆台）面积 = π(r1+r2)·l，l 为母线长。

    r0=10, α=30°, h=10 → r1=15.774, l=11.547, A=934.96 mm²。
    """
    r0, semi_deg, h = 10.0, 30.0, 10.0
    txt = _step(_frustum_loop(r0, semi_deg, h, 24),
                lambda add, place: add(
                    f"CONICAL_SURFACE('',#{place},{r0},"
                    f"{math.radians(semi_deg):.9f})"))
    m = read_step(txt)
    assert len(m.faces) == 1
    f = m.faces[0]
    assert f.surf == "cone"
    semi = math.radians(semi_deg)
    r1 = r0 + h * math.tan(semi)
    expect = math.pi * (r0 + r1) * (h / math.cos(semi))
    assert f.area == pytest.approx(expect, rel=1e-9), f"圆锥面积应为 {expect:.4f}"


def test_cone_area_does_not_depend_on_tessellation():
    """解析式优先：把折线环从 8 点加密到 96 点，面积必须**完全一致**。

    这一条是"不许用网格累加"的守门测试（项目重视解析式）。
    """
    areas = []
    for n in (8, 24, 96):
        txt = _step(_frustum_loop(10.0, 25.0, 12.0, n),
                    lambda add, place: add(
                        f"CONICAL_SURFACE('',#{place},10.,{math.radians(25.0):.12f})"))
        areas.append(read_step(txt).faces[0].area)
    assert areas[0] == pytest.approx(areas[1], rel=1e-12)
    assert areas[1] == pytest.approx(areas[2], rel=1e-12)


def test_cone_normal_and_slope_semantics():
    """法向 = 径向-轴向合成（cosα·er − sinα·az）；slope_deg 即半锥角。

    项目对 slope_deg 的定义是「法向与水平面夹角」= asin(|nz|)。
    轴为 +Z 时圆锥法向 nz = −sinα，故 slope = α：半锥角 30° → 30°。
    MRC 的 R3 规则基于同一语义，所以这里同时验证 analyze_faces 的产出。
    """
    r0, semi_deg, h = 10.0, 30.0, 10.0
    txt = _step(_frustum_loop(r0, semi_deg, h, 32),
                lambda add, place: add(
                    f"CONICAL_SURFACE('',#{place},{r0},"
                    f"{math.radians(semi_deg):.9f})"))
    m = read_step(txt)
    f = m.faces[0]
    n = f.normal
    assert math.sqrt(sum(c * c for c in n)) == pytest.approx(1.0, abs=1e-9)
    assert n[2] == pytest.approx(-math.sin(math.radians(semi_deg)), abs=1e-9)
    # 径向分量必须垂直于轴（水平），即 n 的 xy 模 = cosα
    assert math.hypot(n[0], n[1]) == pytest.approx(
        math.cos(math.radians(semi_deg)), abs=1e-9)

    info = analyze_faces(m)[0]
    assert info.kind == "圆锥面"
    assert info.slope_deg == pytest.approx(semi_deg, abs=1e-6)
    assert info.hint, "圆锥面应给出可打性提示"


def test_cone_negative_semi_angle_flips_slope_sign():
    """semi_angle 可为负（半径沿 +轴递减）：面积仍正确，法向轴向分量随之翻转。"""
    r0, h = 5.0, 10.0
    semi = math.radians(-20.0)
    r1 = r0 + h * math.tan(semi)
    loop = _ring(r0, 0.0, 24) + list(reversed(_ring(r1, h, 24)))
    txt = _step(loop, lambda add, place: add(
        f"CONICAL_SURFACE('',#{place},{r0},{semi:.12f})"))
    f = read_step(txt).faces[0]
    expect = math.pi * (r0 + r1) * (h / math.cos(semi))
    assert f.area == pytest.approx(expect, rel=1e-9)
    assert f.normal[2] == pytest.approx(-math.sin(semi), abs=1e-9)
    assert f.normal[2] > 0.0, "负锥角时法向应朝上"


def test_cone_no_longer_reports_unsupported():
    """圆锥已解析 → 不得再报 G-SURFACE-UNSUPPORTED / G-SURFACE-APPROX。"""
    txt = _step(_frustum_loop(10.0, 30.0, 10.0, 24),
                lambda add, place: add(
                    f"CONICAL_SURFACE('',#{place},10.,0.523598775598)"))
    m = read_step(txt)
    codes = _codes(m)
    assert "G-SURFACE-UNSUPPORTED" not in codes, codes
    assert "G-SURFACE-APPROX" not in codes, codes
    assert "G-SURFACE-UNSUPPORTED" not in (m.faces[0].diagnostics or [])


# ---------------------------------------------------------------- 球面
def _sphere_band(r, th1_deg, th2_deg, n):
    """纬度带边界：低纬圈正序 + 高纬圈反序。"""
    t1, t2 = math.radians(th1_deg), math.radians(th2_deg)

    def ring(theta, rev=False):
        pts = [(r * math.sin(theta) * math.cos(2 * math.pi * k / n),
                r * math.sin(theta) * math.sin(2 * math.pi * k / n),
                r * math.cos(theta)) for k in range(n)]
        return list(reversed(pts)) if rev else pts
    return ring(t1) + ring(t2, rev=True)


def test_sphere_band_area_is_analytic():
    """球带面积 A = r²(cosθ1 − cosθ2)·Δφ（θ 为距极点的极角）。"""
    r = 10.0
    txt = _step(_sphere_band(r, 30.0, 60.0, 48),
                lambda add, place: add(f"SPHERICAL_SURFACE('',#{place},{r})"))
    m = read_step(txt)
    f = m.faces[0]
    assert f.surf == "sphere"
    expect = r * r * (math.cos(math.radians(30.0)) - math.cos(math.radians(60.0))) * 2 * math.pi
    assert f.area == pytest.approx(expect, rel=1e-9)
    # 完整经度 → 不是近似
    assert "G-SURFACE-UNSUPPORTED" not in _codes(m)
    assert "G-SURFACE-APPROX" not in _codes(m)


def test_sphere_full_sphere_area():
    """两极各用一圈极小的纬线代替极点 → 面积逼近 4πr²（解析式，非网格累加）。"""
    r = 7.0
    eps = 1e-3
    txt = _step(_sphere_band(r, math.degrees(eps), 180.0 - math.degrees(eps), 64),
                lambda add, place: add(f"SPHERICAL_SURFACE('',#{place},{r})"))
    f = read_step(txt).faces[0]
    assert f.area == pytest.approx(4 * math.pi * r * r, rel=1e-5)


def test_sphere_single_latitude_is_approx_and_conservative():
    """只有一条纬环时无法判断面在纬线哪一侧 → 取较小球冠并报 G-SURFACE-APPROX。

    半径为 r 的球在赤道处切开，上下两半的边界圆完全相同；解析器无从区分，
    因此只能给保守估值（半球 2πr²）并如实标注"近似"。
    """
    r = 10.0
    txt = _step(_ring(r, 0.0, 48),
                lambda add, place: add(f"SPHERICAL_SURFACE('',#{place},{r})"))
    m = read_step(txt)
    f = m.faces[0]
    assert f.area == pytest.approx(2 * math.pi * r * r, rel=1e-9)
    assert "G-SURFACE-APPROX" in (f.diagnostics or [])
    assert "G-SURFACE-APPROX" in _codes(m), "近似必须汇总到模型级"
    assert "G-SURFACE-UNSUPPORTED" not in _codes(m)


def test_sphere_normal_points_outward():
    """球面法向必须是外法向（与"球心→代表点"同向），否则坡度与内外判定会反。"""
    r = 10.0
    txt = _step(_sphere_band(r, 20.0, 70.0, 32),
                lambda add, place: add(f"SPHERICAL_SURFACE('',#{place},{r})"))
    f = read_step(txt).faces[0]
    radial = tuple(c / r for c in f.centroid)        # 球心在原点
    dot = sum(a * b for a, b in zip(f.normal, radial, strict=True))
    assert dot == pytest.approx(1.0, abs=1e-6)
    info = analyze_faces(read_step(txt))[0]
    assert info.kind == "球面"


# ---------------------------------------------------------------- 环面
def _torus_patch(R, r, u0, u1, v0, v1, n):
    """环面上 (u,v) 矩形裁剪面的一条闭合边界（沿四条等参数边采样）。"""
    def p(u, v):
        return ((R + r * math.cos(v)) * math.cos(u),
                (R + r * math.cos(v)) * math.sin(u),
                r * math.sin(v))

    loop = []
    for k in range(n + 1):                     # v=v1 边
        loop.append(p(u0 + (u1 - u0) * k / n, v1))
    for k in range(1, n + 1):                  # u=u1 边
        loop.append(p(u1, v1 + (v0 - v1) * k / n))
    for k in range(1, n + 1):                  # v=v0 边
        loop.append(p(u1 + (u0 - u1) * k / n, v0))
    for k in range(1, n):                      # u=u0 边
        loop.append(p(u0, v0 + (v1 - v0) * k / n))
    return loop


def test_torus_patch_area_matches_parametric_integral():
    """A = Δu·r·[R·Δv + r·(sin(v0+Δv) − sin v0)]，与 4π²Rr 的整环公式一致。"""
    R, r = 20.0, 5.0
    u0, u1, v0, v1 = 0.0, math.pi, -math.pi / 2, math.pi / 2
    txt = _step(_torus_patch(R, r, u0, u1, v0, v1, 24),
                lambda add, place: add(
                    f"TOROIDAL_SURFACE('',#{place},{R},{r})"))
    m = read_step(txt)
    f = m.faces[0]
    assert f.surf == "torus"
    du, dv = u1 - u0, v1 - v0
    expect = du * r * (R * dv + r * (math.sin(v0 + dv) - math.sin(v0)))
    assert f.area == pytest.approx(expect, rel=1e-9)


def test_torus_is_marked_approx_not_silently_exact():
    """环面积分是参数范围启发式 → 必须报 G-SURFACE-APPROX（不得假装精确支持）。"""
    R, r = 20.0, 5.0
    txt = _step(_torus_patch(R, r, 0.0, math.pi, -math.pi / 2, math.pi / 2, 16),
                lambda add, place: add(
                    f"TOROIDAL_SURFACE('',#{place},{R},{r})"))
    m = read_step(txt)
    assert "G-SURFACE-APPROX" in (m.faces[0].diagnostics or [])
    assert "G-SURFACE-APPROX" in _codes(m)
    assert "G-SURFACE-UNSUPPORTED" not in _codes(m)
    info = analyze_faces(m)[0]
    assert info.kind == "环面"
    assert "近似" in info.hint


# ---------------------------------------------------------------- 样条（仍不支持）
def test_bspline_surface_stays_unsupported_and_estimate_is_conservative():
    """B 样条不做几何求值：面积取"包围盒与三角形扇的较小值"，标 UNSUPPORTED。

    保守估算很重要：夸大会让无效面被下游当成大面（渲染分级/统计）。
    """
    loop = _ring(10.0, 0.0, 8)
    txt = _step(loop, lambda add, place: add(
        "B_SPLINE_SURFACE_WITH_KNOTS('',1,1,((1.,1.),(2.,2.)),"
        ".UNSPECIFIED.,.F.,.F.,.F.,(2,2),(2,2),(0.,1.),(0.,1.),.UNSPECIFIED.)"))
    m = read_step(txt)
    f = m.faces[0]
    assert f.surf == "other"
    assert "G-SURFACE-UNSUPPORTED" in (f.diagnostics or [])
    assert "G-SURFACE-UNSUPPORTED" in _codes(m)
    # 包围盒 20×20=400，三角形扇 = 正八边形面积 ≈ 282.8 → 取较小值 282.8
    fan = 0.5 * 8 * 10.0 * 10.0 * math.sin(2 * math.pi / 8)
    assert f.area == pytest.approx(fan, rel=1e-6), "应取三角形扇（比包围盒小）"


# ---------------------------------------------------------------- AP242 网格补位
def test_hybrid_file_uses_tessellation_for_spline_face():
    """B-rep + AP242 网格混合：样条面若能从同文件三角化数据取到面积，就不再
    用包围盒估算，而是升级为 G-SURFACE-APPROX（面积可信、归属是启发式）。

    AP242 允许同时导出精确曲面与其三角化表示；这时样条面的真实面积就在
    三角形里，比包围盒估算准确得多。
    """
    loop = _ring(10.0, 0.0, 8)          # 面积 ≈ 282.8 的八边形边界
    base = _step(loop, lambda add, place: add(
        "B_SPLINE_SURFACE_WITH_KNOTS('',1,1,((1.,1.),(2.,2.)),"
        ".UNSPECIFIED.,.F.,.F.,.F.,(2,2),(2,2),(0.,1.),(0.,1.),.UNSPECIFIED.)"))
    # 同一位置的一对三角形（覆盖 [-10,10]²，总面积 400 > 估算 282.8）
    extra = (
        "#900=CARTESIAN_POINT('',(-10.,-10.,0.));\n"
        "#901=CARTESIAN_POINT('',(10.,-10.,0.));\n"
        "#902=CARTESIAN_POINT('',(10.,10.,0.));\n"
        "#903=CARTESIAN_POINT('',(-10.,10.,0.));\n"
        "#910=COORDINATES_LIST('',(#900,#901,#902,#903));\n"
        "#911=TRIANGULATED_FACE_SET('',#910,$,$,((1,2,3),(1,3,4)),$);\n"
    )
    txt = base.replace("ENDSEC;\nEND-ISO", extra + "ENDSEC;\nEND-ISO")
    m = read_step(txt)
    assert len(m.faces) == 1, "三角化数据不应另外生成面（是同一批曲面的离散）"
    f = m.faces[0]
    assert f.area == pytest.approx(400.0, rel=1e-9), "应改用三角化面积"
    assert "G-SURFACE-APPROX" in (f.diagnostics or [])
    codes = _codes(m)
    assert "G-SURFACE-UNSUPPORTED" not in codes, "已用三角化数据补位，不应再报不支持"
    assert "G-SURFACE-APPROX" in codes


# ---------------------------------------------------------------- 确定性
def test_surface_parsing_is_deterministic():
    """同一输入两次解析结果完全一致（切片可重放的前提）。"""
    txt = _step(_frustum_loop(10.0, 30.0, 10.0, 20),
                lambda add, place: add(
                    "CONICAL_SURFACE('',#%d,10.,0.523598775598)" % place))
    a, b = read_step(txt), read_step(txt)
    assert len(a.faces) == len(b.faces) == 1
    fa, fb = a.faces[0], b.faces[0]
    assert fa.area == fb.area
    assert fa.normal == fb.normal
    assert fa.centroid == fb.centroid
    assert fa.loop == fb.loop
    assert fa.surf == fb.surf
    assert a.bbox == b.bbox
    assert a.diagnostics == b.diagnostics


# ================================================================ 圆弧离散精度与性能
class TestArcPointsIncrementalTrig:
    """`_arc_points` 用增量三角递推，不得牺牲精度。

    回归背景：原实现每个离散点各调一次 math.cos + math.sin。proe.stp 上
    112 次调用 / 10 240 点，占 read_step 的 17.2%；18 MB 放大件上
    `_arc_points` 占 `_assemble_faces` 的 36.7%。
    改为递推（c,s ← c·cd − s·sd, s·cd + c·sd）后实测 2.48×，
    与逐点 cos/sin 的最大误差 2.84e-14 mm。
    """

    @staticmethod
    def _reference(circle, p_start, p_end, max_sagitta=0.02):
        """逐点 cos/sin 的参考实现（与优化前完全一致）。"""
        import math

        from ame.geometry.vec3 import v_cross, v_dot, v_sub, v_unit

        center, axis, r = circle
        axis = v_unit(axis)
        ref = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
        u = v_unit(v_cross(axis, ref))
        w = v_cross(axis, u)

        def angle(p):
            d = v_sub(p, center)
            return math.atan2(v_dot(d, w), v_dot(d, u))

        a0, a1 = angle(p_start), angle(p_end)
        same = math.hypot(p_start[0] - p_end[0], p_start[1] - p_end[1],
                          p_start[2] - p_end[2]) < 1e-9
        sweep = 2 * math.pi if same else (a1 - a0)
        while sweep <= 0:
            sweep += 2 * math.pi
        while sweep > 2 * math.pi:
            sweep -= 2 * math.pi
        if sweep < 1e-9:
            sweep = 2 * math.pi
        if max_sagitta <= 0 or max_sagitta >= r:
            n = 8
        else:
            step = 2 * math.acos(max(-1.0, min(1.0, 1.0 - max_sagitta / r)))
            n = max(2, min(256, int(math.ceil(sweep / max(step, 1e-6)))))
        pts = [tuple(p_start)]
        for k in range(1, n):
            a = a0 + sweep * k / n
            pts.append((center[0] + r * (math.cos(a) * u[0] + math.sin(a) * w[0]),
                        center[1] + r * (math.cos(a) * u[1] + math.sin(a) * w[1]),
                        center[2] + r * (math.cos(a) * u[2] + math.sin(a) * w[2])))
        pts.append(tuple(p_end))
        return pts

    @pytest.mark.parametrize("radius,axis", [
        (5.0, (0, 0, 1)), (2.5, (0, 0, 1)), (10.0, (1, 1, 0)),
        (0.3, (0, 0, 1)), (100.0, (0, 0, 1)),
    ])
    def test_matches_reference_within_tolerance(self, radius, axis):
        """与逐点 cos/sin 参考实现的坐标差必须远低于弦高容差。"""
        import math
        import random

        from ame.geometry.step_build import _arc_points

        random.seed(11)
        center = (1.0, 2.0, 3.0)
        worst = 0.0
        for _ in range(20):
            a0 = random.uniform(0, 2 * math.pi)
            sw = random.uniform(0.05, 2 * math.pi)
            ps = (center[0] + radius * math.cos(a0),
                  center[1] + radius * math.sin(a0), center[2])
            pe = (center[0] + radius * math.cos(a0 + sw),
                  center[1] + radius * math.sin(a0 + sw), center[2])
            got = _arc_points((center, axis, radius), ps, pe, False, 0.02)
            ref = self._reference((center, axis, radius), ps, pe, 0.02)
            assert len(got) == len(ref), f"段数不一致 {len(got)} vs {len(ref)}"
            for p, q in zip(got, ref, strict=True):
                worst = max(worst, max(abs(p[i] - q[i]) for i in range(3)))
        assert worst < 1e-9, f"坐标差 {worst:.2e} mm 超出可接受范围（应 < 1e-9）"

    def test_sagitta_still_within_tolerance(self):
        """弦高误差仍须满足容差（性能优化不能放松精度）。"""
        import math

        from ame.geometry.step_build import _arc_points

        r = 5.0
        tol = 0.02
        circle = ((0, 0, 0), (0, 0, 1), r)
        pts = _arc_points(circle, (r, 0, 0), (0, r, 0), False, tol)
        # 找出最大弦高
        worst = 0.0
        for a, b in zip(pts, pts[1:], strict=False):
            chord = math.dist(a, b)
            if chord <= 1e-12:
                continue
            # 弦高 = r - sqrt(r² - (chord/2)²)
            sag = r - math.sqrt(max(r * r - (chord / 2) ** 2, 0.0))
            worst = max(worst, sag)
        assert worst <= tol + 1e-12, f"弦高 {worst:.5f} 超过容差 {tol}"

    @pytest.mark.perf
    def test_incremental_is_faster(self):
        """递推实现必须确实更快（防止被改回逐点 cos/sin 而无人发现）。

        打 perf 标记的原因：这是**墙钟时间比较**，在 `check_quality --coverage`
        （插桩会拖慢两边但比例不稳）或慢 CI 上会误报。审计实测
        `--coverage` 下它稳定失败，等于那个开关永远红、实际不可用。
        """
        import time

        from ame.geometry.step_build import _arc_points

        r = 5.0
        circle = ((0, 0, 0), (0, 0, 1), r)
        ps, pe = (r, 0, 0), (0, r, 0)
        N = 800

        t0 = time.perf_counter()
        for _ in range(N):
            _arc_points(circle, ps, pe, False, 0.02)
        t_fast = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(N):
            self._reference(circle, ps, pe, 0.02)
        t_ref = time.perf_counter() - t0

        # 递推应至少快 20%（实测 1.47×；给宽余量防慢机器/噪声）
        assert t_fast < t_ref * 0.85, (
            f"增量三角未生效：递推 {t_fast * 1000:.0f}ms vs 参考 {t_ref * 1000:.0f}ms")
