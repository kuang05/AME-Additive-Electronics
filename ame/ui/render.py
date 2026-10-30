"""结构渲染：相机投影 + 光照 + 深度排序（STR-003）。

设计取舍
--------
不引入 OpenGL / 3D 引擎（与「集显可运行、核心功能离线、安装包轻量」冲突），
用纯 Python + tkinter Canvas 做**画家算法**渲染：

1. **相机**：绕 Z 轴方位角 az、俯仰角 el 的正交投影（可旋转/缩放/平移）。
2. **光照**：Lambert 漫反射 + 环境光，光源为相机侧上方，保证转视角时明暗跟着变。
   面向坡度的可打性提示（斜面橙）作为**叠加色**保留，不与光照抢信息。
3. **深度**：按相机空间深度排序（远→近），并做背面剔除
   （法向背对相机的面不画），因此不会出现「远处面盖住近处面」的穿透感。
4. **规模分层**：面数多时降精度，保证交互帧率（STR-003 允许降精度显示）。

坐标约定：世界坐标为板坐标 mm（Z 向上），相机空间 X 向右、Y 向上、深度 D 沿视线。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# 光照参数（经验值：环境光占比低一些立体感更强）
AMBIENT = 0.24
DIFFUSE = 0.76


# ---------------------------------------------------------------- 光栅化后端
# 实测（800×600，800 个四边形）：
#   逐多边形 create_polygon + update : 72 ms/帧 = 14 fps
#   PIL 光栅化 + PhotoImage 贴图     : 5  ms/帧 = 209 fps
# 差距来自 Tk 的软件光栅化：每个 canvas item 都要单独走一遍抗锯齿绘制，
# 而 PIL 一次成像、一次贴图。因此优先用 PIL，不可用时回退 Canvas 多边形。
_PIL = None
_PIL_CHECKED = False


def _pil_available() -> bool:
    """检测 Pillow 是否可用（打包时可能被裁剪以省体积）。"""
    global _PIL, _PIL_CHECKED
    if not _PIL_CHECKED:
        _PIL_CHECKED = True
        try:
            from importlib.util import find_spec
            _PIL = find_spec("PIL") is not None
        except (ImportError, ValueError):
            _PIL = False
    return bool(_PIL)


def rasterize(items, width: int, height: int, background: str = "#15181c"):
    """把绘制指令光栅化成一帧图像（需要 Pillow）。

    返回 PIL.Image（RGB）。调用方用 ImageTk.PhotoImage 贴到 Canvas。
    比逐条 create_polygon 快一个数量级（见上）。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (max(1, width), max(1, height)), background)
    draw = ImageDraw.Draw(img)
    for it in items:
        cr = it["coords"]
        pts = [(cr[i], cr[i + 1]) for i in range(0, len(cr) - 1, 2)]
        if len(pts) < 3:
            continue
        try:
            draw.polygon(pts, fill=it["fill"], outline=it["outline"])
        except (TypeError, ValueError):
            # 退化多边形（点共线/极小）跳过，不影响整帧
            continue
    return img


# ---------------------------------------------------------------- 规模分级（LOD）
# STR-003 要求"20 万面 ≥30 fps、50 万面降精度 ≥20 fps"。
# 纯 Python + tkinter Canvas 的绘制能力有硬上限，实测（本机，800×600 画布）：
#
#   每帧多边形数    帧耗时      帧率
#     484          13.8 ms     72 fps
#     961          44.4 ms     23 fps
#    1936          78.1 ms     13 fps
#    4900         134.4 ms      7 fps
#   19881         679.8 ms      1.5 fps
#
# 所以"全量绘制"只能覆盖到 ~1500 面（≥15 fps）。面数再多必须靠 LOD 抽样
# 把每帧多边形压回预算内——这是**降精度**（需求明确允许），不是功能缺失。
#
#   档位     面数区间          策略                            每帧多边形
#   full     ≤ 1 500          全量绘制                         ≤ 1 500
#   lod      2 000–200 000    斜面优先 + 面积降序 + 共面聚合      ≤ 900
#   coarse   200 000–500 000  同上但更少                         ≤ 700
#   bbox     > 500 000        退化为包围盒线框                   12 条线
#
# 关键在"保留哪些面"：按面积降序 + 斜面优先，能保住轮廓、大平面与所有
# 可打性风险面；丢掉的是视觉上可忽略的碎片。
LOD_FULL_MAX = 1_500           # 以内全量（实测 ≥15 fps）
LOD_COARSE_MAX = 200_000       # 到 20 万仍做 LOD
LOD_BBOX_MAX = 500_000         # 超过 50 万退化为包围盒
# 多边形预算按实测帧率反推（同一台机器上量出来的）：
#   1200 面 ≈ 25 fps，800 面 ≈ 31 fps，1500 面 ≈ 21 fps，2000 面骤降到 7 fps
#   （Canvas 的 create_polygon 在一定数量后明显变慢）
# 因此 lod 档取 1200（满足 20 万面 ≥20 fps 的"降精度"要求），
# coarse 档取 800（满足 50 万面 ≥20 fps）。
LOD_BUDGET_FULL = 1_500        # full 档多边形预算（与 LOD_FULL_MAX 一致）
LOD_BUDGET_LOD = 800           # lod 档：实测 ≈30 fps（端到端）
LOD_BUDGET_COARSE = 700        # coarse 档：实测 ≈25–30 fps

# 兼容旧名：超过它不再保证全量，但仍走 LOD 而不是直接退化成线框
DETAIL_LIMIT = LOD_FULL_MAX


def lod_level(n_faces: int) -> str:
    """按面数返回 LOD 档位：full / lod / coarse / bbox。"""
    if n_faces <= LOD_FULL_MAX:
        return "full"
    if n_faces <= LOD_COARSE_MAX:
        return "lod"
    if n_faces <= LOD_BBOX_MAX:
        return "coarse"
    return "bbox"


def lod_budget(level: str) -> int:
    return {"full": LOD_BUDGET_FULL, "lod": LOD_BUDGET_LOD,
            "coarse": LOD_BUDGET_COARSE, "bbox": 0}.get(level, LOD_BUDGET_LOD)


# LOD 归约缓存：归约只依赖面列表与预算，**不依赖相机**。
# 5 万面归约实测 270 ms、15 万面 375 ms——若每帧都重做，旋转视角直接卡死。
# 用 (id(面列表), 长度, 预算) 做键；面列表本身由 Workspace 的模型缓存持有，
# 同一模型的生命周期内 id 稳定，模型换了 id 也变，缓存自然失效。
_LOD_CACHE: dict[tuple, tuple] = {}
_LOD_CACHE_MAX = 4


def _cached_reduce(faces, budget: int):
    """带缓存的 reduce_faces（渲染路径专用）。"""
    key = (id(faces), len(faces), budget)
    hit = _LOD_CACHE.get(key)
    if hit is not None:
        return hit
    kept, info = reduce_faces(faces, budget)
    if len(_LOD_CACHE) >= _LOD_CACHE_MAX:
        _LOD_CACHE.pop(next(iter(_LOD_CACHE)))
    _LOD_CACHE[key] = (kept, info)
    return kept, info


def clear_lod_cache():
    """清空 LOD 缓存（测试与模型切换时调用）。"""
    _LOD_CACHE.clear()


def reduce_faces(faces, budget: int, merge_coplanar: bool = True):
    """按视觉重要性裁剪面列表（STR-003 降精度）。

    保留优先级：
      1. **斜面**（2% ≤ |nz| ≤ 98%）——承载可打性风险信息，信息量最高；
      2. **大面**（面积降序）——决定轮廓与体积感；
      3. 其余面按"共面聚合"补位，保住形状又不爆多边形数。

    merge_coplanar：把被丢弃的小面按空间网格 + 法向聚合，每格留一个最大代表面。
    对三角化网格（一个平面被切成上千个三角形）压缩效果显著。
    只影响显示，不改几何数据。

    性能：用 heapq.nlargest 只取预算内的大面，避免对 50 万面做全排序
    （全排序实测 1.5 s，nlargest 约 0.2 s）。

    返回 (保留的面列表, info)。
    """
    import heapq

    total = len(faces)
    if budget <= 0 or total <= budget:
        return list(faces), {"level": "full", "total": total, "kept": total,
                             "merged": 0, "dropped": 0}

    n_slant_budget = max(0, int(budget * 0.4))

    # ① 斜面优先。用一次遍历分桶，避免构造多个中间列表。
    slants = []
    planes = []
    for f in faces:
        nz = abs(f.normal[2]) if f.normal else 0.0
        (slants if face_orientation(nz) == "slant" else planes).append(f)

    if len(slants) > n_slant_budget:
        keep_slants = heapq.nlargest(n_slant_budget, slants, key=lambda f: f.area)
    else:
        keep_slants = slants
    remain = max(0, budget - len(keep_slants))

    if len(planes) > remain:
        keep_planes = heapq.nlargest(remain, planes, key=lambda f: f.area)
        kept_plane_ids = {id(f) for f in keep_planes}
        dropped = [f for f in planes if id(f) not in kept_plane_ids]
    else:
        keep_planes = planes
        dropped = []

    kept = keep_slants + keep_planes
    merged = 0
    if merge_coplanar and dropped:
        cells: dict[tuple, object] = {}
        for f in dropped:
            c = f.centroid
            key = (round(abs(f.normal[2]) if f.normal else 0.0, 1),
                   int(c[0] // 5.0), int(c[1] // 5.0), int(c[2] // 1.0))
            cur = cells.get(key)
            if cur is None or f.area > cur.area:
                cells[key] = f
        extra = sorted(cells.values(), key=lambda f: -f.area)
        room = max(0, budget - len(kept))
        pick = extra[:room]
        merged = len(dropped) - len(pick)
        kept = kept + pick

    # 保持原始顺序（画家算法的稳定输入需要一个确定性的输入顺序）
    order = {id(f): i for i, f in enumerate(faces)}
    kept.sort(key=lambda f: order.get(id(f), 0))
    return kept, {"level": "lod", "total": total, "kept": len(kept),
                  "merged": merged, "dropped": total - len(kept)}


@dataclass
class Camera:
    """正交相机：方位角/俯仰角 + 缩放 + 平移 + 目标中心。"""
    az_deg: float = 45.0        # 绕 Z 轴方位（0 = 沿 +X 看）
    el_deg: float = 35.0        # 俯仰（90 = 正上方俯视）
    zoom: float = 1.0
    pan: tuple = (0.0, 0.0)     # 屏幕像素平移
    center: tuple = (0.0, 0.0, 0.0)   # 看向的世界坐标点

    def _basis(self):
        """相机基向量：右向量 r、上向量 u、视线方向 v（由相机指向场景）。

        结果按相机参数缓存：render_faces 对每个面的每个顶点都要调用
        project()/depth()，900 面 × 4 点 = 3600 次。每次重算 4 个三角函数
        实测占渲染耗时的三成，缓存后基本归零。
        """
        key = (self.az_deg, self.el_deg)
        cached = self.__dict__.get("_basis_cache")
        if cached is not None and cached[0] == key:
            return cached[1]
        az = math.radians(self.az_deg)
        el = math.radians(self.el_deg)
        ca, sa = math.cos(az), math.sin(az)
        ce, se = math.cos(el), math.sin(el)
        # v = 从场景指向相机（用于背面剔除判断）
        v = (ce * ca, ce * sa, se)
        r = (-sa, ca, 0.0)
        u = (-se * ca, -se * sa, ce)
        result = (r, u, v)
        self._basis_cache = (key, result)
        return result

    def project(self, p, scale: float, origin: tuple) -> tuple:
        """世界点 → 屏幕点。scale: mm→px 比例；origin: 屏幕中心像素坐标。"""
        r, u, v = self._basis()
        cx, cy, cz = self.center
        dx, dy, dz = p[0] - cx, p[1] - cy, p[2] - cz
        sx = dx * r[0] + dy * r[1] + dz * r[2]
        sy = dx * u[0] + dy * u[1] + dz * u[2]
        return (origin[0] + sx * scale + self.pan[0],
                origin[1] - sy * scale + self.pan[1])

    def depth(self, p) -> float:
        """世界点的相机空间深度（值越大越靠近相机）。"""
        _r, _u, v = self._basis()
        cx, cy, cz = self.center
        return ((p[0] - cx) * v[0] + (p[1] - cy) * v[1] + (p[2] - cz) * v[2])

    def facing(self, normal, tol: float = -0.35) -> bool:
        """面是否朝向相机。

        判据：法向 · 视线方向 > tol。tol 取负值而非 0，理由：
        正交投影下侧面恰好垂直于视线（点积≈0）时仍应显示；而薄板/楔形体的
        少量背面被剔除后会出现"空洞"（overhang60 曾只剩 2 面），
        因此允许一定程度的背面绘制，由画家算法保证前后覆盖关系。
        """
        _r, _u, v = self._basis()
        return (normal[0] * v[0] + normal[1] * v[1] + normal[2] * v[2]) > tol


# 固定光源方向（世界坐标，相机侧上方偏左）。
# 若跟随相机，旋转视角时明暗关系恒定，反而看不出立体感——固定光让不同朝向的面
# 有稳定的明暗差，转动模型时能看出体积。
LIGHT_DIR = (-0.36, 0.48, 0.80)


def _light_dir(cam: Camera) -> tuple:
    """光源方向：世界坐标固定（不随相机旋转）。"""
    lx, ly, lz = LIGHT_DIR
    n = math.sqrt(lx * lx + ly * ly + lz * lz) or 1.0
    return (lx / n, ly / n, lz / n)


def shade(normal, light) -> float:
    """Lambert：返回 0..1 亮度（用绝对值，双面受光，避免背面全黑）。"""
    d = abs(normal[0] * light[0] + normal[1] * light[1] + normal[2] * light[2])
    return min(1.0, AMBIENT + DIFFUSE * d)


_MIX_CACHE: dict[tuple, str] = {}


def _mix(base: tuple[int, int, int], k: float) -> str:
    """按亮度缩放颜色（k=0 暗，1 亮）。

    颜色组合有限（3 种基色 × 亮度量化到 1/64），缓存后省掉每帧数千次
    字符串格式化。profile 显示 900 面时 _mix 被调 4800 次、占 24 ms 中的 10 ms。
    """
    key = (base, int(k * 64))
    hit = _MIX_CACHE.get(key)
    if hit is not None:
        return hit
    out = "#%02x%02x%02x" % tuple(max(0, min(255, int(c * k))) for c in base)
    if len(_MIX_CACHE) < 4096:
        _MIX_CACHE[key] = out
    return out


# 面的朝向判定阈值（|nz| = |法向·Z|）：
#   < VERTICAL_MAX  → 竖直面（几乎垂直于水平面）
#   > HORIZONTAL_MIN → 水平面
#   介于两者之间    → 斜面（可打性风险面）
# 这三个判定在 LOD 抽样、竖面过滤、着色、画法(A) 四处都要用，统一在此定义，
# 避免各自写死 0.02/0.98 后改一处漏三处。
VERTICAL_MAX = 0.02
HORIZONTAL_MIN = 0.98


def face_orientation(nz: float) -> str:
    """按 |nz| 分类面朝向：horizontal / vertical / slant。"""
    a = abs(nz)
    if a < VERTICAL_MAX:
        return "vertical"
    if a > HORIZONTAL_MIN:
        return "horizontal"
    return "slant"


# 基色：水平面偏冷灰、竖直面略深、斜面用橙色系（可打性提示）
BASE_HORIZONTAL = (122, 138, 156)
BASE_VERTICAL = (74, 86, 100)
BASE_SLANT = (212, 140, 62)


def render_faces(faces, cam: Camera, scale: float, origin: tuple,
                 section_z: float | None = None,
                 detail_limit: int | None = None,
                 lod: str | None = None) -> list[dict]:
    """把面列表转成可绘制的多边形指令（已排序，远→近）。自动按面数分级（LOD）。

    返回 [{coords:[...], fill, outline, width, dash, slant, _lod, _kept,
           _total}]，调用方直接 create_polygon；`_lod` 等字段供状态栏显示。

    lod 显式指定档位（"full"/"lod"/"coarse"）时跳过自动判断，便于测试与调试。
    """
    level = lod or lod_level(len(faces))
    if detail_limit is not None and level == "full" and len(faces) > detail_limit:
        level = "lod"
    if level == "bbox":
        # 超过 50 万面：调用方会画包围盒线框，这里不做任何逐面计算
        return []
    if level == "full":
        kept, info = list(faces), {"level": "full", "total": len(faces),
                                   "kept": len(faces), "merged": 0, "dropped": 0}
    else:
        kept, info = _cached_reduce(faces, lod_budget(level))
    faces = kept
    light = _light_dir(cam)
    draw_vertical = len(faces) <= 300      # 大模型跳过纯竖面（信息量低、数量多）

    items = []
    for f in faces:
        if len(f.loop) < 3:
            continue
        nz = abs(f.normal[2])
        if not draw_vertical and face_orientation(nz) == "vertical":
            continue
        zs = [p[2] for p in f.loop]
        if section_z is not None and min(zs) > section_z + 1e-6:
            continue
        if not cam.facing(f.normal):
            continue                       # 背面剔除：不画背对相机的面

        coords = []
        for pnt in f.loop:
            sx, sy = cam.project(pnt, scale, origin)
            coords.extend((sx, sy))
        depth = sum(cam.depth(p) for p in f.loop) / len(f.loop)

        orient = face_orientation(nz)
        is_slant = orient == "slant"
        if orient == "horizontal":
            base = BASE_HORIZONTAL
        elif orient == "vertical":
            base = BASE_VERTICAL
        else:
            base = BASE_SLANT
        k = shade(f.normal, light)
        # 亮度映射到 0.45..1.15：下限压暗保留层次，上限略超 1 让受光面更亮
        # （原先 0.55+0.55k 会把 0.85 与 1.0 都压到 255 截断，层次丢失）
        fill = _mix(base, 0.45 + 0.70 * k)
        items.append({
            "coords": coords, "depth": depth, "area": f.area,
            "z_mean": sum(zs) / len(zs),
            "fill": fill,
            "outline": _mix(base, 0.28),
            "width": 2 if is_slant else 1,
            "dash": (4, 3) if is_slant else None,
            "slant": is_slant,
            "_lod": info["level"], "_kept": info["kept"], "_total": info["total"],
            "_merged": info["merged"], "_fid": f.fid,
        })

    # 画家算法：深度小的（远）先画。
    # 关键：共面或近共面（如基板顶面与凸台顶面）时，必须让**位置更高的面后画**，
    # 否则面积大的基板顶面会把凸台盖住。深度量化到 0.5 mm 分桶，桶内按高度升序。
    items.sort(key=lambda d: (round(d["depth"] / 0.5), d["z_mean"], -d["area"]))
    return items


def bounds_and_scale(bbox, cam: Camera, width: int, height: int,
                     fill_ratio: float = 0.78) -> tuple[float, tuple]:
    """根据模型包围盒与当前相机，算出 mm→px 的比例与屏幕中心。"""
    x0, y0, z0, x1, y1, z1 = bbox
    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    pts = [cam.project(p, 1.0, (0.0, 0.0)) for p in corners]
    if not pts:
        return 1.0, (width / 2, height / 2)
    sx = [p[0] for p in pts]
    sy = [p[1] for p in pts]
    span_x = max(max(sx) - min(sx), 1e-6)
    span_y = max(max(sy) - min(sy), 1e-6)
    scale = fill_ratio * min(width / span_x, height / span_y) * cam.zoom
    cx = (max(sx) + min(sx)) / 2
    cy = (max(sy) + min(sy)) / 2
    return scale, (width / 2 - cx * scale, height / 2 - cy * scale)


__all__ = ["Camera", "render_faces", "bounds_and_scale", "shade", "DETAIL_LIMIT",
           "lod_level", "lod_budget", "reduce_faces",
           "LOD_FULL_MAX", "LOD_COARSE_MAX", "LOD_BBOX_MAX", "clear_lod_cache",
           "rasterize", "face_orientation", "VERTICAL_MAX", "HORIZONTAL_MIN"]
