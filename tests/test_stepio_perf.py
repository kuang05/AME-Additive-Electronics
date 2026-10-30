"""STEP 解析性能回归 + 优化等价性。

这个文件做两件事：
1. **锁住性能成果**——防止将来的改动把 3.9 倍的加速又吃回去；
2. **证明优化没改变行为**——每个优化项都有对应的等价性断言。

背景：本轮优化把 12000 面模型从 9.59 s 降到 2.48 s（3.9×），
20 MB 外推从 9.75 s 降到 4.1 s（需求上限 15 s）。三项改动：

- `_loop_self_intersects`：按顶点数分级（<4 直接 False，==4 用叉积判，
  ≥5 才调 shapely）。实测这一步原本占 1.4 s/12000 面。
- 语句切分：试过用引号感知的 Python 循环替代正则，实测**更慢**
  （2.70 → 4.13 s），已撤回。这条记在这里避免后人重犯。
- `_v3`：热路径补齐三维，57.6 万次调用 / 0.97 s。改为 early-return
  与 try/IndexError。
"""
from __future__ import annotations

import time

import pytest

from ame.geometry.stepio import (StepBuilder, _loop_self_intersects, box_faces,
                                 read_step)
from ame.geometry.vec3 import _v3


def _bench_model(n_boxes: int = 2000) -> str:
    """构造 n_boxes×6 面的模型（与性能基准同口径）。"""
    faces = []
    for i in range(n_boxes):
        faces += box_faces((i % 50) * 12, (i // 50) * 12, 0, 10, 10, 2)
    return StepBuilder().build(faces, "bench")


# ================================================================ 性能
@pytest.mark.perf
def test_parse_12000_faces_under_6s():
    """12000 面模型解析 < 6 s（优化后实测 2.5 s，留 2.4× 余量）。

    阈值取 6 s 而不是"实测值 × 1.1"：CI 机器可能更慢，但也不允许退回
    9 s 的老水平——那说明优化被吃掉了。
    """
    txt = _bench_model()
    read_step(txt)                       # 预热（排除首次 import 开销）
    t0 = time.perf_counter()
    m = read_step(txt)
    dt = time.perf_counter() - t0
    assert len(m.faces) == 12000
    assert dt < 6.0, f"12000 面解析 {dt:.2f}s，超过 6s 上限（基线曾为 9.59s）"


@pytest.mark.perf
def test_parse_20mb_within_requirement():
    """NFR-001：20 MB 以内 STEP ≤ 15 s（按线性外推）。

    实测 12.2 MB 用 2.5 s，20 MB 外推约 4.1 s。
    """
    txt = _bench_model()
    size_mb = len(txt) / 1024 / 1024
    read_step(txt)
    dt = min(_time_once(txt) for _ in range(3))
    projected = dt / size_mb * 20
    assert projected < 15.0, f"外推 20 MB 需 {projected:.1f}s（上限 15s）"


def _time_once(txt: str) -> float:
    t0 = time.perf_counter()
    read_step(txt)
    return time.perf_counter() - t0


# ================================================================ 等价性
def _old_self_intersect(pts, normal):
    """旧实现：无条件构造 shapely LinearRing（作为等价性基准）。"""
    from shapely.geometry import LinearRing

    from ame.geometry.vec3 import v_cross, v_dot, v_sub, v_unit
    try:
        n = v_unit(normal)
        u = v_unit(v_cross(n, (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)))
        w = v_cross(n, u)
        p0 = pts[0]
        ring = [(v_dot(v_sub(p, p0), u), v_dot(v_sub(p, p0), w)) for p in pts]
        return not LinearRing(ring).is_simple
    except Exception:
        return False


@pytest.mark.parametrize("pts", [
    [(0, 0, 0), (1, 0, 0)],                                   # 退化两点
    [(0, 0, 0), (1, 0, 0), (1, 1, 0)],                        # 三角形
    [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],             # 正常四边形
    [(0, 0, 0), (1, 1, 0), (1, 0, 0), (0, 1, 0)],             # 蝴蝶结（自交）
    [(0, 0, 0), (2, 0, 0), (1, 1, 0), (2, 2, 0), (0, 2, 0)],  # 五边形自交
    [(0, 0, 0), (1, 0, 0), (2, 0, 0), (2, 1, 0), (0, 1, 0)],  # 正常五边形
], ids=["degenerate", "tri", "quad", "bowtie", "pent-cross", "pent-ok"])
def test_self_intersect_matches_shapely(pts):
    """分级判定必须与"总是调 shapely"逐例一致。

    这是优化的安全底线：性能可以快，结论不能变。
    """
    for normal in [(0, 0, 1), (0, 0, -1), (1, 0, 0)]:
        assert _loop_self_intersects(pts, normal) == _old_self_intersect(pts, normal), \
            f"pts={pts} normal={normal} 判定不一致"


def test_self_intersect_random_agree():
    """随机环上的批量等价性（覆盖 shapely 分支与快路径分支）。"""
    import random
    rng = random.Random(20261028)      # 固定种子，结果可复现
    for _ in range(200):
        n = rng.choice([3, 4, 4, 4, 5, 6])
        pts = [(rng.uniform(-1, 1), rng.uniform(-1, 1), 0.0) for _ in range(n)]
        assert _loop_self_intersects(pts, (0, 0, 1)) == \
            _old_self_intersect(pts, (0, 0, 1)), f"pts={pts}"


@pytest.mark.parametrize("arg,expect", [
    (None, (0.0, 0.0, 0.0)),
    ((), (0.0, 0.0, 0.0)),
    ((1,), (1.0, 0.0, 0.0)),
    ((1, 2), (1.0, 2.0, 0.0)),
    ((1, 2, 3), (1.0, 2.0, 3.0)),
    ([1, 2, 3], (1.0, 2.0, 3.0)),
    ((1, 2, 3, 4), (1.0, 2.0, 3.0)),    # 多余维度截断
])
def test_v3_semantics_unchanged(arg, expect):
    """`_v3` 优化后语义必须与原来完全一致（补齐到三维/截断/None）。"""
    assert _v3(arg) == expect


def test_v3_fast_path_returns_same_object():
    """已是 float 三元组时直接返回原对象（快路径），但值必须相同。"""
    v = (1.0, 2.0, 3.0)
    assert _v3(v) is v
    # int 三元组不能走快路径（需要转 float），但值要对
    assert _v3((1, 2, 3)) == (1.0, 2.0, 3.0)


def test_optimized_parse_gives_identical_result():
    """优化后的解析结果与手工核算一致（面数/体积/包围盒）。

    用可解析验证的立方体阵列：每个盒子 10×10×2，2000 个 → 体积 400000 mm³。
    """
    txt = _bench_model(2000)
    m = read_step(txt)
    assert len(m.faces) == 12000
    assert m.volume_mm3 == pytest.approx(400_000, rel=1e-6)
    # 包围盒：50 列 × 40 行，每格 12 mm，盒子 10 mm 宽
    assert m.bbox[0] == pytest.approx(0, abs=1e-6)
    assert m.bbox[3] == pytest.approx(49 * 12 + 10, abs=1e-6)


# ================================================================ 三角化归属的空间索引
class TestTessellatedAttributionIsSubQuadratic:
    """`_apply_tessellated_to_faces` 不得是 O(三角形×面)。

    回归背景：原实现对每个三角形线性扫描全部目标面的包围盒。T×F 配对
    斜率实测 1.03（纯二次）。**混合 AP242 文件**（B-rep 样条面 + 三角化网格）
    正是这种形态：20 MB 实测 23–35 s，超 STR-001 的 15 s 上限 1.5–2.4 倍。
    而 `test_stepio_perf` 的线性外推用的是纯平面盒阵，看不到这条路径。
    """

    @staticmethod
    def _model(F):
        from types import SimpleNamespace as NS
        faces = []
        for i in range(F):
            x, y = (i % 40) * 5.0, (i // 40) * 5.0
            faces.append(NS(surf="other", area=0.0, diagnostics=[], fid=f"F{i}",
                            loop=[(x, y, 0.0), (x + 3, y, 0.0),
                                  (x + 3, y + 3, 0.0), (x, y + 3, 0.0)]))
        return NS(faces=faces)

    @staticmethod
    def _tris(T, seed=1):
        import random
        random.seed(seed)
        out = []
        for _ in range(T):
            x, y = random.uniform(0, 200), random.uniform(0, 200)
            out.append(((x, y, 0.0), (x + 0.5, y, 0.0), (x, y + 0.5, 0.0)))
        return out

    def test_candidate_checks_scale_with_grid_not_with_faces(self):
        """确定性判据：每个三角形只查自己所在格的候选，总检查次数应远小于 T×F。

        为什么不用墙钟：审计实测 `dt < 3.0` 这个阈值区分不了
        "O(n²)" 与 "半 O(n²)"——候选集减半的实现跑 1.8 s，仍然全绿。
        而统计"包围盒判定被执行了多少次"是确定性的，不受机器快慢影响。

        索引版：每次查询的候选数是"同一格内的面数"（约 F/n² = F/F = 1 量级）；
        无索引：每条三角形都要试 F 个面（最坏 T×F 次）。
        """
        from ame.geometry import stepio as S

        m = self._model(1000)
        tris = self._tris(50000)

        # 计数：包裹 `_BoxGrid.candidates` 返回的**可迭代对象**，统计实际被
        # 遍历的候选数。只数 `len(got)` 不够——退化实现（候选为空时回退
        # `range(len(boxes))` 全扫）根本不经过 candidates，计数会漏。
        grid_cls = getattr(S, "_BoxGrid", None)
        assert grid_cls is not None, "缺少 _BoxGrid 空间索引"

        checks = {"n": 0}
        orig = grid_cls.candidates

        class _Counter:
            """包装候选序列：迭代时累加访问次数。"""

            def __init__(self, wrapped):
                self._w = wrapped

            def __iter__(self):
                for item in self._w:
                    checks["n"] += 1
                    yield item

            def __len__(self):
                return len(self._w)

        def counting(self, x, y):
            return _Counter(orig(self, x, y))

        grid_cls.candidates = counting
        try:
            S._apply_tessellated_to_faces(m, tris)
        finally:
            grid_cls.candidates = orig

        # 保险：若退化实现完全绕过 candidates，检查次数会接近 0，
        # 但这恰恰说明"没走索引"——用 boxes 全扫时的真实次数兜底估算。
        if checks["n"] == 0:
            checks["n"] = len(tris) * len(m.faces)

        pairs = len(tris) * len(m.faces)        # 50_000 * 1000 = 50M
        assert checks["n"] < pairs * 0.02, (
            f"候选检查 {checks['n']:,} 次，相对暴力全配对 {pairs:,} 只降到 "
            f"{checks['n'] / pairs:.1%}——空间索引退化（应 < 2%）")

    @pytest.mark.perf
    def test_large_tessellation_under_time_budget(self):
        """F=1000 / T=50000（50M 配对）：墙钟兜底。

        打 perf 标记：墙钟受机器负载影响，不应混在默认套件里跑。
        主要判据是上面那条确定性的"候选检查次数"。
        """
        import time

        from ame.geometry.stepio import _apply_tessellated_to_faces

        m = self._model(1000)
        tris = self._tris(50000)
        t0 = time.perf_counter()
        _apply_tessellated_to_faces(m, tris)
        dt = time.perf_counter() - t0
        # 二次实现约 5.7 s；索引版实测 0.14 s。给 20 倍余量防慢机器误报。
        assert dt < 3.0, f"50M 配对耗时 {dt:.2f}s，空间索引可能失效（二次实现约 5.7s）"

    def test_index_result_matches_bruteforce(self):
        """索引版与暴力全配对必须逐面逐位一致（不能为性能牺牲正确性）。"""
        import random
        from types import SimpleNamespace as NS

        from ame.geometry.stepio import _apply_tessellated_to_faces
        from ame.geometry.vec3 import v_cross, v_len, v_sub

        random.seed(9)
        faces = []
        for i in range(120):
            x, y = random.uniform(0, 60), random.uniform(0, 60)
            w, h = random.uniform(1, 5), random.uniform(1, 5)
            faces.append(NS(surf="other", area=0.0, diagnostics=[], fid=f"F{i}",
                            loop=[(x, y, 0.0), (x + w, y, 0.0),
                                  (x + w, y + h, 0.0), (x, y + h, 0.0)]))
        tris = []
        for _ in range(3000):
            x, y = random.uniform(0, 60), random.uniform(0, 60)
            tris.append(((x, y, 0.0), (x + 1.0, y, 0.0), (x, y + 1.0, 0.0)))

        # 暴力参考
        boxes = []
        for f in faces:
            xs = [p[0] for p in f.loop]
            ys = [p[1] for p in f.loop]
            zs = [p[2] for p in f.loop]
            pad = [1e-6 + 0.01 * (max(v) - min(v)) for v in (xs, ys, zs)]
            boxes.append(((min(xs) - pad[0], min(ys) - pad[1], min(zs) - pad[2]),
                          (max(xs) + pad[0], max(ys) + pad[1], max(zs) + pad[2])))
        expect = [0.0] * len(faces)
        for a, b, c in tris:
            ta = v_len(v_cross(v_sub(b, a), v_sub(c, a))) / 2.0
            if ta < 1e-12:
                continue
            cx = (a[0] + b[0] + c[0]) / 3.0
            cy = (a[1] + b[1] + c[1]) / 3.0
            cz = (a[2] + b[2] + c[2]) / 3.0
            for k, (lo, hi) in enumerate(boxes):
                if lo[0] <= cx <= hi[0] and lo[1] <= cy <= hi[1] and lo[2] <= cz <= hi[2]:
                    expect[k] += ta
                    break

        _apply_tessellated_to_faces(NS(faces=faces), tris)
        got = [f.area for f in faces]
        diff = max(abs(a - b) for a, b in zip(expect, got, strict=True))
        assert diff == 0.0, f"索引版与暴力不一致，最大面积差 {diff:.3e}"

    def test_overlapping_boxes_still_correct(self):
        """包围盒大量重叠（网格退化情形）仍须正确归属。"""
        from types import SimpleNamespace as NS

        from ame.geometry.stepio import _apply_tessellated_to_faces

        # 30 个面全挤在同一小块区域，几乎全部互相重叠
        faces = [NS(surf="other", area=0.0, diagnostics=[], fid=f"F{i}",
                    loop=[(0.0, 0.0, 0.0), (10.0, 0.0, 0.0),
                          (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)])
                 for i in range(30)]
        tris = [((4.0, 4.0, 0.0), (5.0, 4.0, 0.0), (4.0, 5.0, 0.0))]
        _apply_tessellated_to_faces(NS(faces=faces), tris)
        # 只有第一个命中的面拿到面积（break 语义）
        got = [f.area for f in faces]
        assert got[0] > 0.0
        assert all(a == 0.0 for a in got[1:]), "一个三角形只能归一个面（break 语义）"
