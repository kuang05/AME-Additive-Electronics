"""启动入口：`python -m ame`、`python -m ame.ui`，或打包后的 AME.exe。

参数
----
    AME.exe                 启动图形界面
    AME.exe <数据目录>       指定数据目录后启动界面
    AME.exe --selftest      无界面自检：跑完整四域链路并输出结果（用于打包验证）

打包（PyInstaller）后 `__package__` 为空，相对导入不可用，
因此统一使用绝对导入 `ame.*`。
"""
from __future__ import annotations

import sys


def _selftest_units() -> tuple[bool, str]:
    """验证英寸 STEP 的单位换算（PRJ-008）：找到随包发布的英寸样本并核对尺寸。

    这条链路曾整体错 25.4 倍且不报错，因此必须在打包产物上实测，而不只是
    依赖源码测试。返回 (是否通过, 描述)。
    """
    from pathlib import Path

    from ame.db.store import resource_path
    from ame.geometry.analysis import board_holes, find_board_outline
    from ame.geometry.stepio import read_step

    path = Path(resource_path("samples/data/real")) / "feature.stp"
    if not path.exists():
        return True, "跳过（包内无英寸样本）"
    model = read_step(path.read_text("utf-8", errors="replace"))
    span_x = model.bbox[3] - model.bbox[0]
    codes = {d["code"] for d in model.diagnostics}
    # 真实零件宽 135.75 mm；若漏换算会得到 5.34 mm（小 25.4 倍）
    ok = (model.unit == "mm" and 134 < span_x < 138
          and "G-UNIT-INCH-CONVERTED" in codes and model.volume_mm3 > 1e4)
    outline, _note = find_board_outline(model)
    holes = board_holes(model)
    ok = ok and outline is not None and len(holes) == 8
    return ok, f"英寸样本 {span_x:.2f} mm / 体积 {model.volume_mm3:.0f} mm3 / {len(holes)} 孔"


def _selftest_ui() -> tuple[bool, str]:
    """验证界面在**打包产物**里能组装起来（App 与全部 mixin 可导入）。

    不构造真实窗口（自检在无显示环境也要能跑），只检查：
    - 9 个 mixin 类可导入，且 `App` 确实是它们的子类；
    - 每个域的关键入口方法存在于类上。

    这条检查针对的失败模式很具体：PyInstaller 漏收某个 mixin 模块时，
    `AME.exe` 仍能启动到 selftest，但用户点开界面会直接崩或某个功能区空白。
    """
    domains = ("structure", "circuit", "mrc", "slicing", "printing",
               "agent", "project")
    probes = {"structure": "import_step", "circuit": "auto_route",
              "mrc": "run_mrc", "slicing": "slice_now",
              "printing": "start_print", "agent": "run_agent",
              "project": "open_sample", "canvas": "redraw", "shell": "show_help"}
    try:
        from ame.ui.app import App
        from ame.ui._canvas import CanvasMixin
        from ame.ui._shell import ShellMixin
        from ame.ui.domains import (AgentMixin, CircuitMixin, MrcMixin,
                                    PrintingMixin, ProjectMixin,
                                    SlicingMixin, StructureMixin)
    except Exception as e:
        return False, f"界面模块不可导入：{type(e).__name__}: {e}"

    mixins = [ShellMixin, CanvasMixin, StructureMixin, CircuitMixin, MrcMixin,
              SlicingMixin, PrintingMixin, AgentMixin, ProjectMixin]
    missing = [m.__name__ for m in mixins if not issubclass(App, m)]
    if missing:
        return False, f"App 未继承：{missing}"

    absent = [f"{k}:{v}" for k, v in probes.items() if not hasattr(App, v)]
    if absent:
        return False, f"缺少入口方法：{absent}"

    return True, f"9 个 mixin 全部就位（{len(domains)} 个域 + 骨架 + 画布）"


def _selftest_ai(ws, mrc) -> tuple[bool, str]:
    """验证 AI 收益排序层在**打包形态**下可用（AGT-012）。

    返回 (是否通过, 描述)。通过条件：
    - 随包权重能被加载（维度与特征一致）——否则比赛版只有规则先验；
    - 能对当前工程给出候选排序；
    - 关闭云端时**不发任何请求**（DAT-007）。
    """
    try:
        from ame.agent.loop import designer_propose
        from ame.ai.features import FEATURE_NAMES, group_proposals
        from ame.ai.service import ai_status, rank_proposals
    except Exception as e:
        return False, f"AI 模块不可导入：{type(e).__name__}: {e}"

    st = ai_status(cloud_enabled=False)
    local = st["local"]
    if not local.get("available"):
        return False, (f"本地权重不可用（{local.get('load_error')}），"
                       "比赛版将只能用规则先验")

    try:
        state = ws.mrc_state()
        props = group_proposals(mrc["issues"], designer_propose(ws, mrc, state))
        res = rank_proposals(props, state, mrc, cloud_enabled=False)
    except Exception as e:
        return False, f"排序失败：{type(e).__name__}: {e}"

    meta = local
    desc = (f"本地模型可用（{meta.get('samples')} 样本，特征 {len(FEATURE_NAMES)} 维）"
            f"，候选 {len(res['ordered'])} 条，来源 {res['source']}")
    return True, desc


def selftest() -> int:
    """无界面自检：示例工程 → 评分 → 切片 → G-code → 虚拟打印 → 重放。

    输出纯文本结果与退出码（0 成功 / 1 失败），便于自动化验证打包产物。
    """
    import tempfile
    import traceback
    from pathlib import Path

    # Windows 控制台默认 GBK，遇到 GBK 不含的字符（如 ³、′）会直接抛
    # UnicodeEncodeError 把自检打断。改为无法编码时替换，保证自检始终能跑完。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    try:
        from ame import __version__
        from ame.db.store import DataRoot
        from ame.ledger.services import (do_slice, gcode_of, latest_slice,
                                        run_print)
        from ame.ledger.workspace import build_sample_project
        from ame.agent.loop import clone_workspace, replay

        print(f"AME {__version__} 自检")
        print(f"  Python {sys.version.split()[0]}")
        frozen = hasattr(sys, "_MEIPASS")
        print(f"  运行形态 {'打包产物' if frozen else '源码'}")

        tmp = Path(tempfile.mkdtemp(prefix="ame_selftest_"))
        root = DataRoot(tmp / "data")

        ws = build_sample_project(root)
        print(f"  [1/11] 示例工程  {ws.project['name']}"
              f"（{len(ws.comps())} 器件 / {len(ws.pads())} 焊盘 / "
              f"{len(ws.routes())} 走线）")

        mrc = ws.run_mrc()
        print(f"  [2/11] 可打性评分 {mrc['score']} 分（{len(mrc['issues'])} 条问题）")

        model = ws.step_model()
        print(f"  [3/11] STEP 解析  {len(model.faces)} 面 / {model.solid_count} 实体")

        units_ok, units_desc = _selftest_units()
        print(f"  [4/11] 单位换算  {units_desc}")

        # STR-003：三维渲染用 PIL 光栅化（比逐多边形快一个数量级）。
        # 打包时若误把 Pillow 排掉，帧率会掉回 14 fps，这里显式检查。
        try:
            from ame.ui.render import _pil_available
            pil_ok = _pil_available()
        except Exception:
            pil_ok = False
        print(f"       渲染后端  {'PIL 光栅化（快路径）' if pil_ok else 'tkinter 多边形（回退）'}")

        ws.commit("自检基线")
        sl = do_slice(ws)
        print(f"  [5/11] 切片      {sl['passes']} 道次 / {sl['layer_count']} 层 / "
              f"总厚 {sl['total_thickness_mm']} mm")

        gcode = gcode_of(ws, latest_slice(ws)["id"])
        # 打印"运动内容哈希"而非整文件哈希：整文件含
        # `;project`（工程名）与 `;source_commit`（版本 uuid）两行溯源信息，
        # 每次新建示例工程都会变——那是**设计上应当不同**的，
        # 不是不确定。可复现性要求的是"同一设计 → 同一打印动作"（SLC-008）。
        motion = "\n".join(
            ln for ln in gcode.splitlines()
            if not ln.startswith((";project", ";source_commit", ";gcode_sha256")))
        from ame import sha256_text
        print(f"  [6/11] G-code    {len(gcode) / 1024:.0f} KB，"
              f"运动内容哈希 {sha256_text(motion)[:12]}")

        job = run_print(ws)
        print(f"  [7/11] 虚拟打印  {job['state']}，缺陷 {len(job['defects'])} 条")

        # DAT-005：内容寻址文件与记录对得上（结构 + 板文件 + G-code）
        integ = ws.integrity_check()
        integ_ok = bool(integ) and all(r["ok"] for r in integ)
        print(f"  [8/11] 完整性    {len(integ)} 项，"
              f"{'全部一致' if integ_ok else '有不一致：' + str([r for r in integ if not r['ok']])}")

        snap = clone_workspace(ws, "selftest")
        rp = replay(snap, [], ws, do_slice, source_commit=ws.project["head_commit_id"])
        ok = rp["design_match"] and rp["gcode_match"]
        print(f"  [9/11] 重放校验  设计一致={rp['design_match']} "
              f"G-code 一致={rp['gcode_match']}")

        # AGT-012：AI 收益排序层可用（随包权重是否被正确打进产物）。
        # 权重缺失时会静默降级到规则先验，功能不坏但比赛材料会失真，
        # 因此这里显式检查"权重可用"，而不只是检查"没崩"。
        ai_ok, ai_desc = _selftest_ai(ws, mrc)
        print(f"  [10/11] AI 排序  {ai_desc}")

        # 界面组装：App 由 9 个 mixin 组合（_shell/_canvas + 7 个域）。
        # PyInstaller 对多重继承的静态发现较弱，缺一个 mixin 就是整块功能区
        # 静默消失——这属于"打包后才发现"的典型问题，必须在自检里挡住。
        ui_ok, ui_desc = _selftest_ui()
        print(f"  [11/11] 界面组装  {ui_desc}")

        passed = bool(ok and integ_ok and units_ok and job["state"] == "done"
                      and mrc["score"] >= 0 and sl["passes"] >= 7
                      and len(model.faces) > 0 and ai_ok and ui_ok)
        print("\n结果：" + ("通过" if passed else "失败"))
        return 0 if passed else 1
    except Exception:
        print("\n自检异常：", file=sys.stderr)
        traceback.print_exc()
        return 1


def main(data_dir: str | None = None) -> int:
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        return selftest()
    if data_dir is None and args:
        arg = args[0]
        if not arg.startswith("-"):
            data_dir = arg
    try:
        from ame.ui.app import App
    except ImportError as e:
        print(f"无法加载界面：{e}\n请确认环境中带 tkinter。", file=sys.stderr)
        return 2
    app = App(data_dir)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
