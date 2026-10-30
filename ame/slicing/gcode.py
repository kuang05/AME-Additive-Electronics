"""G-code 导出（SLC-008）：确定可复现，含版本、材料批次、道次标记、烘箱阶段。

同一输入（state/params/materials/source_commit）→ 字节级相同的文件，
末行 ;gcode_sha256 为内容哈希（不含该行自身），连续两次导出一致。
"""
from __future__ import annotations

from .. import digest
from .slicer import build_schedule

MAT_TOOL = {"A": 0, "B": 1}   # A 银浆 → E1/T0，B 介电 → E2/T1

# 回抽参数：浆料粘度高、针头细，空移前必须回抽，否则持续渗出形成拖尾
# （PRT-004「停挤出、回吸、抬针」；标定时按材料粘度调整）
RETRACT_MM = 0.8
RETRACT_FEED = 1200        # mm/min
Z_HOP_MM = 0.4             # 空移抬针高度，避免蹭到已打印路径


def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def export_gcode(slices: dict, params: dict, mat_a: dict, mat_b: dict,
                 project_name: str, source_commit_id: str, design_digest: str) -> str:
    lines: list[str] = []
    A = lines.append
    A(";AME-GCODE v1 ; encoding=utf-8 ; 单位mm")
    A(f";project {project_name}")
    A(f";source_commit {source_commit_id}")
    A(f";design_sha256 {design_digest}")
    A(f";material A {mat_a['name']} batch {mat_a.get('batch', '')}")
    A(f";material B {mat_b['name']} batch {mat_b.get('batch', '')}")
    A(f";params_sha256 {digest(params)}")
    A(f";physical_layers {slices['layer_count']} passes {len(slices['passes'])}")
    A(f";z bottom={_fmt(slices['z_bottom'])} circuit={_fmt(slices['z_circuit'])} "
      f"top={_fmt(slices['z_top'])}")
    A(f";retract {_fmt(RETRACT_MM)} mm @ F{RETRACT_FEED}")
    A(";resume_safe_markers on ; 每道次结束写入安全点（PRT-014 断电续打）")
    A("G21 ; mm")
    A("G90 ; 绝对坐标")
    A("M83 ; 相对挤出（回抽用负 E，不影响绝对坐标）")
    A("M107 ; 风扇关（热固化工艺不开风扇）")

    x = y = z = 0.0
    e = {"A": 0.0, "B": 0.0}
    cur_mat = None
    import math

    retracted = {"on": False}

    def retract():
        """回抽浆料（PRT-004：空移前停挤出 + 回吸 + 抬针）。"""
        if cur_mat is None or retracted["on"]:
            return
        A(f"G1 E{MAT_TOOL[cur_mat]}:{_fmt(-RETRACT_MM)} F{RETRACT_FEED} ; 回抽")
        retracted["on"] = True

    def unretract():
        """恢复挤出前补偿回抽量。"""
        if cur_mat is None or not retracted["on"]:
            return
        A(f"G1 E{MAT_TOOL[cur_mat]}:{_fmt(RETRACT_MM)} F{RETRACT_FEED} ; 恢复出料")
        retracted["on"] = False

    def travel(nx, ny, hop: bool = True):
        """空移到下一条路径起点。

        hop=True 时抬针（跨道次、跨材料、跨大距离时必要）；
        同层同材料的相邻填充线之间只需回抽+贴面移动，抬针纯属浪费
        （实测每段都抬针会让 G-code 从 56 KB 涨到 122 KB）。
        """
        nonlocal x, y, z
        if abs(nx - x) < 1e-4 and abs(ny - y) < 1e-4:
            return
        retract()
        if hop and z + Z_HOP_MM <= 9.9:    # 抬针（不撞限位）
            A(f"G0 Z{_fmt(z + Z_HOP_MM)} F600 ; 抬针")
        A(f"G0 X{_fmt(nx)} Y{_fmt(ny)} F3600")
        if hop:
            A(f"G0 Z{_fmt(z)} F600 ; 落针")
        x, y = nx, ny

    def emit_cure(phase: dict):
        A(f";CURE {phase['phase']} T={_fmt(phase['temp_c'])} "
          f"t={int(phase['duration_s'])}s ; {phase['note']}")
        if phase["duration_s"] > 0:
            A(f"G4 P{int(phase['duration_s'] * 1000)}")

    # 烘箱阶段按物理层交错（SLC-005 排程）：该层最后一个道次打印完 →
    # 预凝胶/定型/升温/层固化；整板终固化与冷却在末尾。
    schedule = build_schedule(params, slices)
    printed_phases = {"打印·下保护层·介电", "打印·下保护层·银浆",
                      "打印·电路层·介电", "打印·电路层·银浆",
                      "打印·上保护层·介电", "打印·上保护层·银浆"}
    by_pass: dict[int, list[dict]] = {}
    for s in schedule:
        if s["kind"] in ("bottom", "circuit", "top") and s["phase"] not in printed_phases:
            by_pass.setdefault(s["pass_no"], []).append(s)
    tail_cures = [s for s in schedule if s["kind"] == "board"]

    A(";OVEN begin")
    for p in slices["passes"]:
        A(f";PASS {p['pass_no']} kind={p['kind']} mat={p['mat']} "
          f"z={_fmt(p['z_mm'])} h={_fmt(p['h_mm'])}")
        if p["mat"] != cur_mat:
            cur_mat = p["mat"]
            A(f"T{MAT_TOOL[cur_mat]} ; 切换 {'银浆E1' if cur_mat == 'A' else '介电E2'}")
        A(f"G0 Z{_fmt(p['z_mm'])} F600")
        z = p["z_mm"]
        prev_role = None
        for path in p["paths"]:
            if not path["pts"]:
                continue
            pts = path["pts"]
            # 轮廓/坝/坑这类跨区域跳转抬针；连续填充线贴面移动即可
            hop = path["role"] != "fill" or prev_role != "fill"
            travel(pts[0][0], pts[0][1], hop=hop)
            unretract()
            prev_role = path["role"]
            for i in range(1, len(pts)):
                (x0, y0), (x1, y1) = pts[i - 1], pts[i]
                ln = math.dist((x0, y0), (x1, y1))
                de = ln * path["width_mm"] * p["h_mm"] * params["flow_factor"]
                e[cur_mat] += de
                A(f"G1 X{_fmt(x1)} Y{_fmt(y1)} "
                  f"E{MAT_TOOL[cur_mat]}:{_fmt(e[cur_mat])} F{int(path['speed_mm_s'] * 60)}")
                x, y = x1, y1
        retract()   # 道次结束回抽，避免停留时渗出
        for s in by_pass.get(p["pass_no"], []):
            emit_cure(s)
        # 安全点标记：断电后可按此恢复（PRT-014 依赖这些标记定位）
        A(f";SAFE_POINT pass={p['pass_no']} z={_fmt(p['z_mm'])} "
          f"x={_fmt(x)} y={_fmt(y)}")
        A(f";PASS_END {p['pass_no']}")

    for s in tail_cures:
        emit_cure(s)
    A(";OVEN end")
    A("M107")
    total_len = sum(
        math.dist(path["pts"][i], path["pts"][i + 1])
        for pa in slices["passes"] for path in pa["paths"]
        for i in range(len(path["pts"]) - 1))
    A(f";TOTAL_LENGTH {_fmt(round(total_len, 1))} mm")
    A(f";TOTAL_PASSES {len(slices['passes'])}")
    A("G0 Z" + _fmt(z + 5) + " F600")
    A("G28 ; 回零")
    A("M84 ; 关电机")
    body = "\n".join(lines)
    from .. import sha256_text
    h = sha256_text(body + "\n")
    return body + f"\n;gcode_sha256 {h}\n"


def gcode_content_hash(text: str) -> str:
    """去掉 hash 行后取内容哈希，用于导入校验（DAT-005）。"""
    lines = [ln for ln in text.splitlines() if not ln.startswith(";gcode_sha256")]
    from .. import sha256_text
    return sha256_text("\n".join(lines) + "\n")
