"""面分析（STR-004）、板框识别（STR-007）、布尔建议预览（STR-005，只预览不执行）。"""
from __future__ import annotations

import math

from shapely.geometry import Polygon
from dataclasses import dataclass

from .stepio import Face, StepModel


@dataclass
class FaceInfo:
    fid: str
    kind: str            # 平面 / 柱面 / 圆锥面 / 球面 / 环面 / 其他
    normal: tuple
    area: float
    centroid: tuple
    slope_deg: float     # 法向与水平面夹角
    printable: bool
    hint: str


# surf 字段 → 中文类型名。曲面法向取"面上代表点"的法向（圆锥/球面在面上是变化的，
# 这里给的是面中心处的值），因此坡度对它们只有参考意义。
_KIND_NAMES = {
    "plane": "平面",
    "cylinder": "柱面",
    "cone": "圆锥面",
    "sphere": "球面",
    "torus": "环面",
    "tessellated": "三角面片",
}


def analyze_faces(model: StepModel, slope_threshold_deg: float = 45.0) -> list[FaceInfo]:
    out = []
    for f in model.faces:
        kind = _KIND_NAMES.get(f.surf, "其他")
        nz = abs(f.normal[2]) if f.normal else 0.0
        slope = math.degrees(math.asin(min(1.0, nz)))
        # 坡度规则：显著倾斜面（45°~85°）不利于浆料沉积；近水平/近竖直不标记。
        # 只对"整面坡度一致"的类型判定：平面与圆锥面（母线方向恒定）。
        # 球面/环面的法向沿面连续变化，单个代表法向不足以判定整面，故只给提示。
        bad = f.surf in ("plane", "cone") and slope_threshold_deg < slope <= 85.0
        hint = ""
        if bad:
            hint = f"坡度 {slope:.0f}°，超过 {slope_threshold_deg:.0f}° 阈值，浆料易流淌、喷头可达性差"
        elif f.surf == "cylinder":
            hint = "柱面按母线分区沉积，圆度受针径限制"
        elif f.surf == "cone":
            hint = f"圆锥面（半锥角 {slope:.0f}°），坡度沿母线恒定，针尖路径需随高度收放"
        elif f.surf == "sphere":
            hint = "球面法向沿面变化，坡度按面中心处估算，需逐点校核"
        elif f.surf == "torus":
            hint = "环面法向沿面变化且面积为参数近似（G-SURFACE-APPROX），需人工复核"
        out.append(FaceInfo(f.fid, kind, f.normal, f.area, f.centroid, slope,
                            printable=not bad, hint=hint))
    return out


def _board_plane_normal(model: StepModel) -> tuple[float, float, float]:
    """推断板面法向：取包围盒最薄的一轴。

    比硬编码 Z 稳——竖直薄板（如 30×2×30）的板面法向是 Y 而非 Z。
    """
    x0, y0, z0, x1, y1, z1 = model.bbox
    spans = [x1 - x0, y1 - y0, z1 - z0]
    thin = spans.index(min(spans))
    n = [0.0, 0.0, 0.0]
    n[thin] = 1.0
    return tuple(n)


def _face_plane_polygon(f: Face, n_axis: int) -> object:
    """把面投影到板面坐标（抛弃法向轴），返回 shapely Polygon 或 None。"""
    axes = [i for i in range(3) if i != n_axis]
    pts = [(round(p[axes[0]], 4), round(p[axes[1]], 4)) for p in f.loop]
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if (not poly.is_empty and poly.area > 0) else None


def _board_candidate_layer(model: StepModel):
    """选板框候选层：按法向位置分层，各层求并集，取面积最大的那层。

    返回 (n_axis, merged_polygon, pos, 该层的面)，无候选返回 None。

    单独抽出来是因为板框与板框内孔必须来自**同一层**。早期
    find_board_outline 用并集层、board_holes 用"面积最大的单片"：当某层
    由多片拼成且其中单片比另一层更大时，两者会选到不同层，切片就会在
    错误位置扣孔（甚至把孔扣到板框外）。
    """
    from shapely.ops import unary_union

    normal = _board_plane_normal(model)
    n_axis = 2 if abs(normal[2]) > 0.5 else (1 if abs(normal[1]) > 0.5 else 0)

    groups: list[tuple] = []          # (面, 投影面, 沿法向的位置)
    for f in model.faces:
        # 三角化网格（AP242）的面也是平面片：surf="tessellated" 必须一并接受，
        # 否则网格文件永远认不出板框（实测 100×60 的网格盒子被判"无板框"）。
        if f.surf not in ("plane", "tessellated") or len(f.loop) < 3:
            continue
        if abs(f.normal[n_axis]) < 0.98:
            continue
        coords = [p[n_axis] for p in f.loop]
        if max(coords) - min(coords) > 0.05:
            continue
        poly = _face_plane_polygon(f, n_axis)
        if poly is None:
            continue
        groups.append((f, poly, max(coords)))
    if not groups:
        return None

    # 不能取"最高层"：带凸台的板，凸台顶面位置更高但面积小得多
    # （plate_boss 会把 12×12 的凸台当成板框，板宽 100 丢失）。
    by_level: dict[float, list] = {}
    for f, poly, pos in groups:
        by_level.setdefault(round(pos, 2), []).append((f, poly))
    best_area, best = -1.0, None
    for pos, items in by_level.items():
        u = unary_union([p for _f, p in items])
        if u.is_empty:
            continue
        if u.area > best_area:
            best_area, best = u.area, (u, pos, [f for f, _p in items])
    if best is None:
        return None
    merged, pos, faces = best
    if merged.geom_type == "MultiPolygon":
        merged = max(merged.geoms, key=lambda p: p.area)
    if merged.is_empty or merged.area <= 0:
        return None
    return n_axis, merged, pos, faces


def find_board_outline(model: StepModel):
    """板框 = 所有近水平朝上面的**并集**（STR-007）。返回 (轮廓点列, 说明)。

    为什么用并集而不是"最大单面"：
    CAD 里顶面常被分模线、特征拼接拆成多片共面矩形。取最大单片会把
    板框少算一半，**而且不报错**（实测 100×60 板被识别成 50×60）。
    KiCad DRC 处理板轮廓也是先把所有 Edge.Cuts 拼成一条闭合多边形再判定。
    """
    normal = _board_plane_normal(model)
    layer = _board_candidate_layer(model)
    if layer is None:
        return None, "未找到近水平朝上的大面作为板框，请检查模型或手动画板框"
    n_axis, merged, _pos, top_faces = layer

    # 板状性：板面积占"投影包围盒"的比例。用并集自身的 bounds 而非模型 bbox，
    # 否则凸台/悬垂等特征会把包围盒撑大，占比算不准。
    minx, miny, maxx, maxy = merged.bounds
    proj_area = max((maxx - minx) * (maxy - miny), 1e-6)
    x0, y0, z0, x1, y1, z1 = model.bbox
    all_spans = sorted([x1 - x0, y1 - y0, z1 - z0])
    thickness = all_spans[0]                 # 最薄轴 = 板厚
    fill = merged.area / proj_area
    # 板状性门槛：候选面必须占"模型投影包围盒"够大的比例。
    # 竖直薄板（30×2×30）的顶面只占 3.3%，会被正确拒绝；
    # 而带凸台的板（100×60 基板 + 12×12 凸台）基板占 94%，通过。
    spans3 = [x1 - x0, y1 - y0, z1 - z0]
    # 增材打印要求板面接近水平：大面法向必须接近 +Z。
    # 竖直薄板（no_outline 30×2×30）的大面法向是 Y，虽是最薄轴但板面竖立，
    # 不能作为打印基板 —— 这类模型必须让人工指定板框。
    horiz_ok = abs(normal[2]) > 0.9
    proj_axes = [i for i in range(3) if i != n_axis]
    model_proj = max(spans3[proj_axes[0]] * spans3[proj_axes[1]], 1e-6)
    share_of_model = merged.area / model_proj
    # 板件判据：法向轴必须与"最薄轴"一致（即真正的薄板），且平面占模型投影够大
    if (not horiz_ok) or fill < 0.05 or share_of_model < 0.25             or max(maxx - minx, maxy - miny) < 10.0:
        return None, (f"板框候选面积 {merged.area:.0f} mm² 仅占模型投影的 "
                      f"{share_of_model * 100:.1f}%，不像板框"
                      "（可能为竖直薄板或多个分离实体），请手工指定板框")

    raw = list(merged.exterior.coords)
    if len(raw) >= 2 and raw[0] == raw[-1]:
        raw.pop()
    # 标准化输出：逆时针方向、从最左下顶点开始。
    # 保证同一个板框无论输入面点序如何，输出序列都一致（便于比较与重放）。
    if merged.exterior.is_ccw is False:
        raw = list(reversed(raw))
    if raw:
        start = min(range(len(raw)), key=lambda i: (raw[i][1], raw[i][0]))
        raw = raw[start:] + raw[:start]
    pts = [(round(x, 4), round(y, 4)) for x, y in raw]
    note = (f"板框由 {len(top_faces)} 个共面朝上面并集而成"
            f"（面积 {merged.area:.0f} mm²，占投影 {fill * 100:.0f}%，厚 {thickness:.2f} mm）")
    if merged.interiors:
        note += f"，含 {len(merged.interiors)} 个内孔"
    return pts, note


def board_holes(model: StepModel) -> list[list[tuple[float, float]]]:
    """板框面的内孔（螺丝孔等），供切片扣除。返回 [[(x,y),...], ...]。

    与 find_board_outline 取同一层（见 _board_candidate_layer），并按板面
    投影轴取坐标——非 Z 向板件也不会退化成 (x,y) 错位。
    """
    layer = _board_candidate_layer(model)
    if layer is None:
        return []
    n_axis, _merged, _pos, faces = layer
    axes = [i for i in range(3) if i != n_axis]
    out: list[list[tuple[float, float]]] = []
    for f in faces:
        for h in getattr(f, "holes", None) or []:
            if len(h) < 3:
                continue
            out.append([(round(p[axes[0]], 4), round(p[axes[1]], 4)) for p in h])
    return out


def manual_outline(x0, y0, x1, y1) -> list[tuple[float, float]]:
    """识别失败时手工指定矩形板框（STR-007）。"""
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@dataclass
class BoolSuggestion:
    prim: str            # box / cylinder / prism
    kind: str            # add / sub
    params: dict         # 原点/尺寸，可调
    desc: str


def suggest_booleans(model: StepModel, outline) -> list[BoolSuggestion]:
    """布尔候选（STR-005）：基于顶面与柱面统计给出加/减体建议，仅生成参数供预览。"""
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    top_z = model.bbox[5]
    sugs = [
        BoolSuggestion("box", "add",
                       {"x": round(cx - 5, 2), "y": round(cy - 5, 2), "z": round(top_z, 2),
                        "dx": 10.0, "dy": 10.0, "dz": 2.0},
                       "中央加 10×10×2 凸台（器件支撑/避让）"),
        BoolSuggestion("box", "sub",
                       {"x": round(cx - 6, 2), "y": round(cy - 4, 2), "z": round(top_z, 2),
                        "dx": 12.0, "dy": 8.0, "dz": 0.1},
                       "中央挖 12×8×0.1 槽（定位/嵌件沉槽）"),
        BoolSuggestion("cylinder", "add",
                       {"x": round(min(xs) + 8, 2), "y": round(min(ys) + 8, 2), "z": round(top_z, 2),
                        "r": 3.0, "dz": 2.0},
                       "角部加 Ø6×2 圆凸台（定位柱）"),
        BoolSuggestion("prism", "sub",
                       {"x": round(cx - 5, 2), "y": round(cy - 3, 2), "z": round(top_z, 2),
                        "dx": 10.0, "dy": 6.0, "dz": 0.2, "n": 3},
                       "挖三棱槽（走线过脊引导）"),
    ]
    return sugs
