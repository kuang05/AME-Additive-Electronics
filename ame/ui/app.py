"""主窗口（UI-001/002/003/004/005/007/008/009）。

布局（需求 §5.1）：
  顶栏：工程名 / 当前版本 / MRC 分数 / 设备状态
  左栏：四域（结构、走线、切片、打印）+ 操作按钮
  中央：画布（三维视图为俯视投影 + 二维布线视图）
  右栏：属性 / 问题 / 计划 / 对话
  底栏：层列表 / 时间轴 / 进度

画布说明：首版三维用等轴测投影绘制网格、板框、器件与走线（不依赖 OpenGL），
二维视图为严格俯视；两者共享同一选择对象（UI-002）。
"""
from __future__ import annotations

import os
import sys
import queue
import threading
import tkinter as tk
from tkinter import messagebox


from ..db.store import DataRoot, now_iso
from ..logging_setup import get_logger
from ..ledger.workspace import Workspace


def _ensure_tk_libraries():
    """某些 conda 发行版的 Tk 找不到 tcl/tk 脚本目录，导致启动即失败。

    这里按常见布局补齐 TCL_LIBRARY / TK_LIBRARY，只在变量缺失且目录存在时设置。
    """
    if os.environ.get("TCL_LIBRARY") and os.environ.get("TK_LIBRARY"):
        return
    roots = []
    base = os.path.dirname(os.path.dirname(sys.executable))
    for rel in ("Library/lib", "lib", "tcl", "../lib"):
        roots.append(os.path.normpath(os.path.join(base, rel)))
    roots.append(os.path.normpath(os.path.join(os.path.dirname(sys.executable), "..", "lib")))
    for root in roots:
        tcl = os.path.join(root, "tcl8.6")
        tkd = os.path.join(root, "tk8.6")
        if os.path.isfile(os.path.join(tcl, "init.tcl")) and \
                os.path.isfile(os.path.join(tkd, "tk.tcl")):
            os.environ.setdefault("TCL_LIBRARY", tcl)
            os.environ.setdefault("TK_LIBRARY", tkd)
            return


_ensure_tk_libraries()

from ._canvas import CanvasMixin      # noqa: E402  (需在 tk 库路径补齐后导入)
from ._shell import ShellMixin        # noqa: E402
from .domains import (AgentMixin, CircuitMixin, MrcMixin,  # noqa: E402
                      PrintingMixin, ProjectMixin, SlicingMixin,
                      StructureMixin)
from .theme import (ACCENT, BAD, BG, GOOD, MUTED, PANEL, TEXT,  # noqa: E402,F401
                    WARN)


class App(ShellMixin, CanvasMixin, StructureMixin, CircuitMixin, MrcMixin,
          SlicingMixin, PrintingMixin, AgentMixin, ProjectMixin, tk.Tk):
    def __init__(self, data_dir=None, hidden=False):
        super().__init__()
        self.title("增材电子一体化软件 AME")
        self.geometry("1600x960")
        self.minsize(1280, 800)
        self.configure(bg=BG)
        self.hidden = hidden
        if hidden:
            # 测试/自动化：完全不映射到屏幕，且对话框只记录不弹出
            self.withdraw()
            self.dialogs: list[dict] = []
            for name in ("showinfo", "showerror", "showwarning"):
                setattr(messagebox, name, self._record_box(name))
            def _askno(title, msg, **kw):
                self.dialogs.append({"kind": "askyesno", "title": title,
                                     "message": msg})
                if len(self.dialogs) > self.DIALOG_LOG_MAX:
                    del self.dialogs[: len(self.dialogs) - self.DIALOG_LOG_MAX]
                return False
            messagebox.askyesno = _askno
        self.data_root = DataRoot(data_dir)
        # NFR-006：关键路径日志写到 <数据目录>/logs/ame.log（1 MB × 3 轮转）
        try:
            from ..logging_setup import configure as _configure_log
            self.log_file = _configure_log(
                self.data_root.data_dir,
                level=self.data_root.settings.get("log_level", "info"))
        except Exception:
            self.log_file = None
        self.log = get_logger("ui")
        self.log.info("启动 AME（隐藏模式=%s，数据目录=%s）",
                      hidden, self.data_root.data_dir)
        self.ws: Workspace | None = None
        self.selected: dict | None = None
        self.view_mode = "3d"
        self.mrc: dict = {"issues": [], "score": None}
        self.slices: dict | None = None
        self.last_job: dict | None = None
        self._busy = False
        self._build_style()
        self._build_menu()
        self._build_layout()
        self._refresh_all()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 外观




    # ------------------------------------------------------------ 右侧面板












    # ------------------------------------------------------------ 域动作


    # ------------------------------------------------------------ 状态与刷新
    # 隐藏模式记录弹窗的上限：长时间自动化运行（如批量切片）会不断累积，
    # 不设上限会持续吃内存。只保留最近 1000 条，足够测试断言用。
    DIALOG_LOG_MAX = 1000




    # ------------------------------------------------------------ 后台任务（UI-005）
    # 超过该预估耗时的任务转到后台线程执行，避免界面冻结（实测 400×400 板切片 0.8 s）
    ASYNC_THRESHOLD_MS = 120

    def _run_async(self, label: str, work, on_done, on_error=None):
        """真正的后台执行：work 在子线程跑，结果回到主线程。

        注意：**不能在子线程里调用 Tk 的 after()**（Tk 不是线程安全的，
        会抛 "main thread is not in main loop"，回调永不执行）。
        改为子线程只写队列，主线程用 after() 轮询取结果。
        """
        self.task_cancel.clear()
        self.status(f"{label}…（可点「取消」中止）")
        self.progress_set(5)
        result_queue: queue.Queue = queue.Queue()

        def runner():
            try:
                result_queue.put(("ok", work()))
            except Exception as e:      # 异常也只放进队列，不碰 Tk
                result_queue.put(("err", e))

        threading.Thread(target=runner, daemon=True).start()
        self._poll_task(result_queue, label, on_done, on_error)

    def _poll_task(self, q, label, on_done, on_error, waited_ms: int = 0):
        """主线程轮询后台结果（Tk 唯一的合法调用方）。"""
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            # 每 50 ms 轮询一次；任务超长时也不过密
            self.after(50, lambda: self._poll_task(
                q, label, on_done, on_error, waited_ms + 50))
            return
        self._finish_async(label, payload if kind == "ok" else None,
                           payload if kind == "err" else None, on_done, on_error)

    def _finish_async(self, label, res, err, on_done, on_error):
        if err is not None:
            self.progress_set(0)
            if on_error:
                on_error(err)
            else:
                messagebox.showerror(f"{label}失败", str(err))
            return
        if self.task_cancel.is_set():
            self.progress_set(0)
            self.status(f"{label}已取消", "warn")
            return
        self.progress_set(100)
        on_done(res)
        self.progress_set(0)

    def cancel_task(self):
        """取消当前后台任务（协作式：任务跑完即丢弃结果）。"""
        self.task_cancel.set()
        self.status("已请求取消当前任务…", "warn")

    def _require_ws(self) -> bool:
        if not self.ws:
            if messagebox.askyesno("没有打开的工程",
                                   "当前没有工程。是否新建一个？\n\n"
                                   "提示：可以先用“工程 → 新建工程”创建。"):
                self.new_project()
            else:
                self.status("未打开工程：请先新建或打开工程（UI-007）", "warn")
            return False
        return True

    def _refresh_all(self):
        ws = self.ws
        if not ws:
            self.lbl_project.configure(text="未打开工程")
            self.lbl_version.configure(text="版本 —")
            self.lbl_score.configure(text="分数 —", foreground=MUTED)
            self.lbl_device.configure(text="设备：未连接")
            self.prop_text.configure(state="normal")
            self.prop_text.delete("1.0", "end")
            self.prop_text.insert("1.0",
                                  "还没有工程。\n\n"
                                  "想快速体验：工程 → 打开示例工程\n"
                                  "（自动导入示例板，给出 60 分基线，可直接切片打印）\n\n"
                                  "用自己的文件：工程 → 新建工程 → 导入 STEP 与板文件\n"
                                  "继续之前的工作：工程 → 打开工程")
            self.prop_text.configure(state="disabled")
            self.redraw()
            return
        hist = ws.history()
        self.lbl_project.configure(text=ws.project["name"])
        self.lbl_version.configure(
            text=f"版本 {hist[-1]['seq'] if hist else 0}｜{now_iso()}")
        # 分数只在“有可评对象”时显示：没有结构也没有走线时，100 分毫无意义且误导；
        # 有结构但没板框时也不能给满分（此时切片/布线都被阻断）
        has_struct = bool(ws.structure())
        has_circuit = bool(ws.routes()) or bool(ws.pads())
        outline, _onote = ws.board_outline() if has_struct else (None, "")
        if not has_struct and not has_circuit:
            self.lbl_score.configure(text="分数 —（待导入设计）", foreground=MUTED)
        elif has_struct and not has_circuit:
            # 只有结构没有电路时，七条规则都没有可查对象，满分无意义
            self.lbl_score.configure(text="分数 —（待导入板文件）", foreground=MUTED)
        elif not outline:
            self.lbl_score.configure(text="分数 —（缺板框）", foreground=WARN)
        elif self.mrc.get("score") is None:
            self.lbl_score.configure(text="分数 —（待检查）", foreground=MUTED)
        else:
            s = self.mrc["score"]
            self.lbl_score.configure(
                text=f"分数 {s}",
                foreground=GOOD if s >= 80 else WARN if s >= 60 else BAD)
        self._fill_issues()
        self._update_layer_label(ws)
        self._refresh_layer_tab()
        self._refresh_print_tab()
        self.redraw()




    # ------------------------------------------------------------ 工程






    # ------------------------------------------------------------ 工程管理（PRJ-007）








    # ------------------------------------------------------------ 结构域











    # ------------------------------------------------------------ 三维查看：剖切 / 测量



    # ------------------------------------------------------------ 走线域












    # ------------------------------------------------------------ MRC









    # ------------------------------------------------------------ 切片





    # ------------------------------------------------------------ 打印










    # ------------------------------------------------------------ 智能体





    # ------------------------------------------------------------ 画布








    # ---- 相机（STR-003）：三维用可旋转的正交相机，二维为纯俯视 ----













    # ---- 平移（右键拖拽）----



    # 属性面板字段 → 中文标签（UI-008：界面全中文，不暴露内部字段名）
    PROP_LABELS = {
        "ref": "位号", "footprint": "封装", "x": "X 坐标 (mm)", "y": "Y 坐标 (mm)",
        "x_mm": "X 坐标 (mm)", "y_mm": "Y 坐标 (mm)", "surface": "所在面",
        "rot": "旋转 (°)", "rot_deg": "旋转 (°)", "height": "器件高度 (mm)",
        "height_mm": "器件高度 (mm)", "size_dx": "外形宽 (mm)", "size_dy": "外形高 (mm)",
        "locked": "锁定", "pad_no": "焊盘号", "net": "网络", "w": "焊盘宽 (mm)",
        "h": "焊盘高 (mm)", "w_mm": "焊盘宽 (mm)", "h_mm": "焊盘高 (mm)",
        "shape": "焊盘形状", "pts": "路径点", "width": "线宽 (mm)",
        "width_mm": "线宽 (mm)", "layer_index": "层号", "strategy": "布线方式",
        "points": "路径点", "from_pad": "起点焊盘", "to_pad": "终点焊盘",
        "rule_code": "规则", "severity": "严重度", "penalty": "扣分",
        "reason": "原因", "suggestion": "建议", "target_type": "对象类型",
        "target_id": "对象", "id": "编号", "from": "起点", "to": "终点",
        "distance_mm": "距离 (mm)",
    }
    PROP_HIDDEN = {"id"}




    # ------------------------------------------------------------ 帮助

    HELP_SECTIONS = [
        ("术语", [
            ("四域", "结构 / 走线 / 切片 / 打印"),
            ("MRC", "可打性规则检查（R1 线宽、R2 间距、R3 坡度、R4 可达性、"
                    "R5 遮挡、R6 温度场、R7 介电流道）"),
            ("道次", "一次材料沉积；同一高度可有介电与银浆两个道次"),
            ("物理层", "下保护层 / 电路层 / 上保护层"),
            ("禁布区", "不允许布线的区域，自动布线与手动落线都会避开（CIR-006）"),
            ("装配展平", "STEP 装配体按 MAPPED_ITEM 位姿把同一零件的多个实例摆开（STR-001）"),
        ]),
        ("规则速查", [
            ("R1 线宽", "走线或槽宽低于下限（默认 0.5 mm）"),
            ("R2 间距", "异网最小距离（默认 0.5 mm），同网不误报"),
            ("R3 坡度", "面法向与水平面夹角超过阈值（默认 45°）"),
            ("R4 可达性", "针尖安全半径内的间隙不足（针尖外径 + 0.5 mm）"),
            ("R5 遮挡", "已打印层（高度区间重叠且先于走线打印）或器件挡住路径"),
            ("R6 温度场", "位置估算温差超过允许（默认 5 ℃，炉门在 x=0 侧）"),
            ("R7 介电流道", "坝间通道宽度不足（默认 0.5 mm）"),
        ]),
        ("错误码", [
            ("E-STEP-IMPORT", "STEP 解析失败"),
            ("E-BOARD-IMPORT", "板文件解析失败"),
            ("E-SLICE", "切片被阻断（连通性/材料/参数）"),
            ("G-UNIT-MISSING", "未声明单位"),
            ("G-OPEN-SHELL", "壳不闭合"),
            ("G-TINY-FACE", "极小面（面积 < 0.01 mm²）"),
            ("G-SELF-INTERSECT", "平面的环投影自交"),
            ("G-SURFACE-UNSUPPORTED", "曲面类型不支持（样条/圆锥/球/环面）"),
            ("G-ASSEMBLY-FLATTENED", "装配体已按实例位姿展开"),
            ("G-ASSEMBLY-EXPLICIT", "含装配实例但几何已展开（无需处理）"),
            ("G-ASSEMBLY-MIRROR", "实例变换含镜像"),
            ("G-SIZE-SUSPICIOUS", "换算后尺寸量级异常，请核对 CAD 单位设置"),
        ]),
        ("快捷键", [
            ("Ctrl+Z / Ctrl+Y", "撤销 / 重做"),
            ("Ctrl+S", "提交版本"),
            ("Ctrl+L", "层（切片道次）显隐"),
            ("Ctrl+0", "适配窗口"),
            ("Esc", "取消当前工具"),
            ("滚轮", "缩放画布"),
            ("双击", "布线结束时结束本段"),
        ]),
    ]



    # ------------------------------------------------------------ 生命周期


def main(data_dir=None):
    App(data_dir).mainloop()


run = main
