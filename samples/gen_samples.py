"""样例数据生成器：STEP 样本、KiCad 板文件、T19-A 验收板。

用法：python -m samples.gen_samples [输出目录]
默认输出 samples/data/。T19-A：8 器件 / 12 网络 / 单层 / 初始 62±3 分。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from ame.geometry.stepio import StepBuilder, box_faces, prism_faces

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"


def kicad_board(outline, comps, nets=(), segments=()) -> str:
    """comps: [{ref, fp, x, y, rot, pads:[{no, net, dx, dy, w, h, shape}]}]
    nets: 网络号声明 [(no, name)]；segments: 走线 [{netno, net, pts, width}]"""
    netno = {name: no for no, name in nets}
    L = ["(kicad_pcb (version 20171130)",
         f'  (gr_rect (start {outline[0][0]} {outline[0][1]}) '
         f'(end {outline[2][0]} {outline[2][1]}) (layer "Edge.Cuts") (width 0.05))']
    for no, name in nets:
        L.append(f'  (net {no} "{name}")')
    for c in comps:
        L.append(f'  (footprint "{c["fp"]}" (layer "F.Cu") (at {c["x"]} {c["y"]} {c["rot"]})')
        L.append(f'    (fp_text reference "{c["ref"]}" (at 0 0) (layer "F.SilkS"))')
        for p in c["pads"]:
            no = p.get("netno") or netno.get(p.get("net"), 0)
            net = f'(net {no} "{p["net"]}")' if p.get("net") else '(net 0 "")'
            L.append(f'    (pad "{p["no"]}" smd {p.get("shape", "rect")} '
                     f'(at {p["dx"]} {p["dy"]} {c["rot"]}) (size {p["w"]} {p["h"]}) '
                     f'(layers "F.Cu" "F.Mask") {net})')
        L.append("  )")
    for s in segments:
        pts = [p[:2] for p in s["pts"]]
        for i in range(len(pts) - 1):
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            L.append(f'  (segment (start {x0} {y0}) (end {x1} {y1}) '
                     f'(width {s["width"]}) (layer "F.Cu") (net {s["netno"]}))')
    L.append(")")
    return "\n".join(L)


def two_pin(ref, x, y, net_a, net_b, no_a=1, no_b=2, dx=2.8, w=1.5, h=1.0, fp="Lib:R0603"):
    return {"ref": ref, "fp": fp, "x": x, "y": y, "rot": 0,
            "pads": [{"no": str(no_a), "net": net_a, "dx": -dx, "dy": 0, "w": w, "h": h},
                     {"no": str(no_b), "net": net_b, "dx": dx, "dy": 0, "w": w, "h": h}]}


def four_pad(ref, x, y, nets4, dx=2.8, dy=2.0, w=1.5, h=1.0, fp="Lib:QFN8"):
    """四焊盘器件：左右各两列（1/3 在左侧，2/4 在右侧），nets4 = [n1,n2,n3,n4]。"""
    return {"ref": ref, "fp": fp, "x": x, "y": y, "rot": 0,
            "pads": [{"no": "1", "net": nets4[0], "dx": -dx, "dy": 0, "w": w, "h": h},
                     {"no": "2", "net": nets4[1], "dx": dx, "dy": 0, "w": w, "h": h},
                     {"no": "3", "net": nets4[2], "dx": -dx, "dy": -dy, "w": w, "h": h},
                     {"no": "4", "net": nets4[3], "dx": dx, "dy": -dy, "w": w, "h": h}]}


# ---------------------------------------------------------------- STEP 样本
def gen_step_samples():
    # T01 平板
    (OUT / "plate_100x60.step").write_text(
        StepBuilder().build(box_faces(0, 0, 0, 100, 60, 2), "plate"), "utf-8")
    # T02 缺面（去掉一面 → OPEN_SHELL）
    (OUT / "broken.step").write_text(
        StepBuilder().build(box_faces(0, 0, 0, 50, 40, 2)[:-1], "broken"), "utf-8")
    # T03/T11 平板+凸台（两个实体：装配体展平）+ 60° 悬垂楔（斜面法向与水平面 60°）
    boss = box_faces(40, 25, 2, 12, 12, 3)
    (OUT / "plate_boss.step").write_text(
        StepBuilder().build(box_faces(0, 0, 0, 100, 60, 2) + boss, "plate_boss"), "utf-8")
    # 60° 悬垂面：轮廓斜面使面法向与水平面夹角 60°（run=10, rise=tan30*10→法向60°）
    # 面倾斜角 α=30°（对水平面），法向与水平面 = 90-30 = 60° ✓
    prof = [(0, 0), (10, 0), (10, 10 * math.tan(math.radians(30)))]
    (OUT / "overhang60.step").write_text(
        StepBuilder().build(prism_faces(prof, 0, 30), "overhang60"), "utf-8")
    # 无板框（竖直薄板，顶面小）→ STR-007 提示
    (OUT / "no_outline.step").write_text(
        StepBuilder().build(box_faces(0, 0, 0, 30, 2, 30), "wall"), "utf-8")


# ---------------------------------------------------------------- 板文件样本
def board_10():
    """T06：10 器件板（12 个网络，无走线）。"""
    comps = []
    nets = [f"N{i}" for i in range(1, 13)]
    k = 0
    for i in range(10):
        x, y = 15 + (i % 5) * 17, 20 + (i // 5) * 20
        comps.append(two_pin(f"R{i + 1}", x, y, nets[k % 12], nets[(k + 1) % 12]))
        k += 2
    declared = [(i + 1, n) for i, n in enumerate(nets)]
    return kicad_board([(5, 5), (95, 5), (95, 55), (5, 55)], comps, declared)


def board_t19a():
    """T19-A 验收板：8 器件、12 网络、单层、违规设计（细线 + 近距对）。
    违规走线以 KiCad segment 形式随板文件导入（导入只校验可读性，合规交给 MRC）。"""
    comps = [
        four_pad("U1", 20, 15, ["GND", "VCC", "TP1", "TP2"]),
        two_pin("U2", 80, 15, "VCC", "N1"),
        four_pad("C3", 80, 30, ["N6", "GND", "TP3", "TP4"]),
        two_pin("R1", 20, 45, "N1", "N2"),
        two_pin("R2", 45, 45, "N2", "N3"),
        two_pin("R3", 70, 45, "N3", "N4"),
        two_pin("C1", 20, 30, "N4", "N5"),
        two_pin("C2", 50, 30, "N5", "N6"),
    ]
    names = ["GND", "VCC", "N1", "N2", "N3", "N4",
             "N5", "N6", "TP1", "TP2", "TP3", "TP4"]
    nets = [(i + 1, n) for i, n in enumerate(names)]
    netno = {n: i + 1 for i, n in enumerate(names)}
    # 焊盘绝对坐标（dx=±2.8）：U1(20,15) C3(80,30) U2(80,15) R1(20,45) R2(45,45)
    #   R3(70,45) C1(20,30) C2(50,30)
    routes = routes_t19a()
    segments = [{"netno": netno[r["net"]], "net": r["net"],
                 "pts": r["pts"], "width": r["width"]} for r in routes]
    return kicad_board([(5, 5), (95, 5), (95, 55), (5, 55)], comps, nets, segments)


def routes_t19a():
    """T19-A 初始走线（与板文件 segment 一致），初始分 62（需求 §7.1 T19 基准）：

    R1 命中 5 条：VCC/N1/N2/N3/N6 为 0.30 mm（低于 0.5 下限）；
    R2/R7 命中：N5-N6 两段平行异网（边距 1.2 mm < 0.5+2×0.6=1.7）；
    R6 命中：GND 从炉门侧 x=10 起绕行，估算温差 5.1 ℃ > 5。
    全部两焊盘网络连通、无短路、无器件遮挡 → 可直接切片与打分开局。"""
    return [
        {"net": "VCC", "pts": [[22.8, 15], [77.2, 15]], "width": 0.30},
        # N1 走底部走廊绕回 R1.1，避开 GND 回程
        {"net": "N1",  "pts": [[82.8, 15], [88, 15], [88, 20], [13, 20], [13, 41], [17.2, 45]],
         "width": 0.30},
        {"net": "N2",  "pts": [[22.8, 45], [22.8, 42], [42.2, 42], [42.2, 45]], "width": 0.30},
        {"net": "N3",  "pts": [[47.8, 45], [47.8, 42], [67.2, 42], [67.2, 45]], "width": 0.30},
        {"net": "N4",  "pts": [[72.8, 45], [72.8, 39], [15, 39], [15, 33], [17.2, 30]],
         "width": 0.6},
        # N5/N6 平行近距对（R2+R7）
        {"net": "N5",  "pts": [[22.8, 30], [22.8, 27.5], [47.2, 27.5], [47.2, 30]],
         "width": 0.6},
        {"net": "N6",  "pts": [[52.8, 30], [52.8, 28.9], [77.2, 28.9], [77.2, 30]],
         "width": 0.30},
        # GND 从炉门侧绕行（R6）
        {"net": "GND", "pts": [[17.2, 15], [14, 12], [10, 12], [10, 9], [90, 9],
                               [90, 30], [82.8, 30]], "width": 0.6},
    ]


def routes_t07():
    """T07 手动连 3 个网络（board_10 布局，0.6 mm）：
    N1: R1.1(12.2,20) ↔ R7.1(29.2,40)
    N3: R2.1(29.2,20) ↔ R8.1(46.2,40)
    N5: R3.1(46.2,20) ↔ R9.1(63.2,40)

    竖直引出段刻意偏离焊盘所在列（12.2/29.2/46.2/63.2），横向段错开，
    因此三条线互不接触、不压其他焊盘：连通性 ok、无异网短路。
    其余网络（N2/N4/N6/N7/N8）按 T07 要求保持未布线，连通性检查会报未连通，
    这是预期状态——T07 只验证这 3 个网络的布线、连通与撤销。
    """
    return [
        {"net": "N1", "pts": [[12.2, 20], [12.2, 22.5], [28, 22.5],
                              [28, 37.5], [29.2, 37.5], [29.2, 40]], "width": 0.6},
        {"net": "N3", "pts": [[29.2, 20], [29.2, 25.5], [45, 25.5],
                              [45, 37.5], [46.2, 37.5], [46.2, 40]], "width": 0.6},
        {"net": "N5", "pts": [[46.2, 20], [46.2, 33.5], [62, 33.5],
                              [62, 37.5], [63.2, 37.5], [63.2, 40]], "width": 0.6},
    ]


def netlist_sample():
    return {
        "outline": [[5, 5], [95, 5], [95, 55], [5, 55]],
        "comps": [{"ref": "D1", "footprint": "Lib:SOD-323", "x": 30, "y": 30,
                   "surface": "top", "rot": 0, "height": 1.0}],
        "pads": [{"ref": "D1", "pad": "1", "net": "NET1", "x": 28.1, "y": 30,
                  "w": 1.5, "h": 1.0},
                 {"ref": "D1", "pad": "2", "net": "NET2", "x": 31.9, "y": 30,
                  "w": 1.5, "h": 1.0}],
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    gen_step_samples()
    (OUT / "board_10.kicad_pcb").write_text(board_10(), "utf-8")
    (OUT / "board_T19A.kicad_pcb").write_text(board_t19a(), "utf-8")
    (OUT / "routes_T19A.json").write_text(
        json.dumps(routes_t19a(), ensure_ascii=False, indent=1), "utf-8")
    (OUT / "routes_T07.json").write_text(
        json.dumps(routes_t07(), ensure_ascii=False, indent=1), "utf-8")
    (OUT / "netlist.json").write_text(
        json.dumps(netlist_sample(), ensure_ascii=False, indent=1), "utf-8")
    print(f"样例已生成 → {OUT}")
    for f in sorted(OUT.iterdir()):
        print(" ", f.name, f.stat().st_size, "B")


if __name__ == "__main__":
    main()
