"""界面骨架与面板（Mixin）。

从 `app.py` 拆出。`App` 通过多重继承获得这些方法，对外接口与拆分前
完全一致——分离动机只是让单个文件可读：原 `app.py` 3500 行、131 个方法，
定位任何一个面板都要在千行范围内搜索。

本文件只包含方法体（逐字搬运），新增状态字段仍写在 `App.__init__`，
避免 mixin 之间出现隐式依赖。
"""
from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..ledger.errors import CmdError

from .theme import ACCENT, BAD, BG, GOOD, MUTED, PANEL, TEXT, WARN


class ShellMixin:
    def _build_layout(self):
        # 顶栏
        top = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        top.pack(side="top", fill="x")
        self.lbl_project = ttk.Label(top, text="未打开工程", style="H1.TLabel")
        self.lbl_project.pack(side="left")
        self.lbl_version = ttk.Label(top, text="版本 —", style="Muted.TLabel")
        self.lbl_version.pack(side="left", padx=(16, 0))
        self.lbl_score = ttk.Label(top, text="分数 —", style="Panel.TLabel",
                                   font=("Microsoft YaHei UI", 14, "bold"))
        self.lbl_score.pack(side="left", padx=(16, 0))
        ttk.Button(top, text="重新检查", command=self.run_mrc).pack(side="right", padx=4)
        self.lbl_device = ttk.Label(top, text="设备：未连接", style="Muted.TLabel")
        self.lbl_device.pack(side="right", padx=(0, 12))

        # 底栏（层 / 时间轴 / 进度）
        bottom = ttk.Frame(self, style="Panel.TFrame", padding=(12, 6))
        bottom.pack(side="bottom", fill="x")
        ttk.Label(bottom, text="进度", style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(bottom, length=260, mode="determinate",
                                       maximum=100)
        self.progress.pack(side="left", padx=8)
        self.btn_cancel = ttk.Button(bottom, text="取消", width=6,
                                     command=self.cancel_task)
        self.btn_cancel.pack(side="left", padx=(0, 8))
        self.lbl_status = ttk.Label(bottom, text="就绪", style="Muted.TLabel")
        self.lbl_status.pack(side="left", padx=12)
        self.lbl_layer = ttk.Label(bottom, text="层：—", style="Muted.TLabel")
        self.lbl_layer.pack(side="right")

        # 左栏：四域
        left = ttk.Frame(self, style="Panel.TFrame", width=230, padding=(10, 10))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ttk.Label(left, text="四域工作区", style="H1.TLabel").pack(anchor="w")
        self.domain_buttons = {}
        for key, label in (("structure", "① 结构域"), ("circuit", "② 走线域"),
                           ("mrc", "③ 可打性"), ("slicing", "④ 切片"),
                           ("print", "⑤ 打印")):
            b = ttk.Button(left, text=label, command=lambda k=key: self.enter_domain(k))
            b.pack(fill="x", pady=2)
            self.domain_buttons[key] = b
        ttk.Separator(left).pack(fill="x", pady=8)
        self.action_frame = ttk.Frame(left, style="Panel.TFrame")
        self.action_frame.pack(fill="both", expand=True)
        self._render_domain_actions("structure")

        # 右栏：属性 / 问题 / 计划 / 对话
        right = ttk.Frame(self, style="Panel.TFrame", width=380, padding=(8, 8))
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.tab_prop = ttk.Frame(self.nb, style="Panel.TFrame", padding=8)
        self.tab_issue = ttk.Frame(self.nb, style="Panel.TFrame", padding=8)
        self.tab_layer = ttk.Frame(self.nb, style="Panel.TFrame", padding=8)
        self.tab_print = ttk.Frame(self.nb, style="Panel.TFrame", padding=8)
        self.tab_plan = ttk.Frame(self.nb, style="Panel.TFrame", padding=8)
        self.tab_chat = ttk.Frame(self.nb, style="Panel.TFrame", padding=8)
        self.nb.add(self.tab_prop, text="属性")
        self.nb.add(self.tab_issue, text="问题")
        self.nb.add(self.tab_layer, text="层")
        self.nb.add(self.tab_print, text="打印")
        self.nb.add(self.tab_plan, text="计划")
        self.nb.add(self.tab_chat, text="对话")
        self._build_prop_tab()
        self._build_issue_tab()
        self._build_layer_tab()
        self._build_print_tab()
        self._build_plan_tab()
        self._build_chat_tab()

        # 中央画布
        center = ttk.Frame(self, style="Panel.TFrame")
        center.pack(side="left", fill="both", expand=True, padx=(6, 6), pady=6)
        bar = ttk.Frame(center, style="Panel.TFrame")
        bar.pack(fill="x")
        self.btn_3d = ttk.Button(bar, text="三维视图", command=lambda: self.set_view("3d"))
        self.btn_3d.pack(side="left", padx=2)
        self.btn_2d = ttk.Button(bar, text="二维布线", command=lambda: self.set_view("2d"))
        self.btn_2d.pack(side="left", padx=2)
        ttk.Button(bar, text="适配窗口", command=self.fit_view).pack(side="left", padx=2)
        ttk.Button(bar, text="层显隐", command=self.toggle_layer_visible).pack(
            side="left", padx=2)
        self.lbl_view = ttk.Label(bar, text="", style="Muted.TLabel")
        self.lbl_view.pack(side="right")
        self.canvas = tk.Canvas(center, bg="#15181c", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, pady=(6, 0))
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<ButtonPress-3>", self.on_canvas_pan_start)
        self.canvas.bind("<B3-Motion>", self.on_canvas_pan)
        self.canvas.bind("<ButtonRelease-3>", self.on_canvas_pan_end)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        # CIR-004：双击结束布线（状态栏一直提示"双击结束"，但事件从未绑定）
        self.canvas.bind("<Double-Button-1>", self.finish_route)
        self.zoom = 1.0
        self.pan = [0.0, 0.0]
        self.task_cancel = threading.Event()   # 后台任务取消标志
        self.cam_az = 45.0          # 相机方位角（水平旋转，STR-003 旋转）
        self.cam_el = 35.0          # 相机俯仰角（抬升视角）
        self._orbit = None          # 旋转拖拽起点 (x, y, az, el)
        self._pan_drag = None       # 平移拖拽起点
        self._drag_start = None
        self._drag_pts: list = []
        self.tool = None
        self._drag_pts: list = []      # CIR-004 布线落点
        self.route_net: str | None = None   # CIR-004 本次布线的网络
        self.layers_visible = True
        self.section_z = None          # STR-003 剖切高度
        self.measure_pts: list = []    # STR-003 测量点
        self.play_index: int | None = None   # SLC-006 当前播放道次
        self.last_dialog: dict | None = None
        self.heatmap_on = False       # MRC-010 热力图开关
        self.show_ratlines = True     # CIR-003 飞线显示
        self.highlight_net: str | None = None   # CIR-003 单网络高亮
        self.feature_preview: list[dict] = []   # STR-005 布尔建议预览（不落库）
        self._bg_photo = None         # 光栅化渲染的 PhotoImage 引用（防 GC 回收）
        self._lod_kept = []           # 上一帧 LOD 实际绘制的面（顶面高亮用）
        self._cam_cache = None        # 单次重绘内复用的 (cam, scale, origin)
        self.last_measure: dict | None = None
        self.play_window = None

    def _render_domain_actions(self, domain: str):
        for w in self.action_frame.winfo_children():
            w.destroy()
        F = self.action_frame
        self.current_domain = domain
        if domain == "structure":
            ttk.Button(F, text="导入 STEP…", command=self.import_step).pack(fill="x", pady=2)
            ttk.Button(F, text="面分析", command=self.show_face_analysis).pack(
                fill="x", pady=2)
            ttk.Button(F, text="布尔建议…", command=self.show_boolean).pack(fill="x", pady=2)
            ttk.Button(F, text="手工板框…", command=self.manual_outline).pack(
                fill="x", pady=2)
            ttk.Button(F, text="特征列表", command=self.show_features).pack(fill="x", pady=2)
            ttk.Button(F, text="剖切（Z 阈值）…", command=self.set_section).pack(
                fill="x", pady=2)
            ttk.Button(F, text="测量（两点距离）", command=self.measure).pack(
                fill="x", pady=2)
        elif domain == "circuit":
            ttk.Button(F, text="导入板文件…", command=self.import_board).pack(
                fill="x", pady=2)
            ttk.Button(F, text="画走线（在画布拖拽）",
                       command=lambda: self.set_tool("route")).pack(fill="x", pady=2)
            ttk.Button(F, text="连通性检查", command=self.check_connectivity).pack(
                fill="x", pady=2)
            ttk.Button(F, text="跳线候选", command=self.jumper_candidates).pack(
                fill="x", pady=2)
            ttk.Button(F, text="自动布局候选…", command=self.auto_place).pack(
                fill="x", pady=2)
            ttk.Button(F, text="自动布线（单网络）…", command=self.auto_route).pack(
                fill="x", pady=2)
            ttk.Button(F, text="禁布区…", command=self.manage_keepouts).pack(
                fill="x", pady=2)
            ttk.Button(F, text="撤销 / 重做", command=self.undo).pack(fill="x", pady=2)
        elif domain == "mrc":
            ttk.Button(F, text="运行检查并评分", command=self.run_mrc).pack(
                fill="x", pady=2)
            ttk.Button(F, text="违规热力图", command=self.toggle_heatmap).pack(
                fill="x", pady=2)
            ttk.Button(F, text="AI 收益排序…", command=self.show_ai_status).pack(
                fill="x", pady=2)
            ttk.Button(F, text="导出问题列表…", command=self.export_issues).pack(
                fill="x", pady=2)
            ttk.Button(F, text="规则阈值…", command=self.edit_rules).pack(fill="x", pady=2)
        elif domain == "slicing":
            ttk.Button(F, text="工艺参数…", command=self.edit_params).pack(fill="x", pady=2)
            ttk.Button(F, text="材料库…", command=self.edit_materials).pack(
                fill="x", pady=2)
            ttk.Button(F, text="切片", command=self.slice_now,
                       style="Accent.TButton").pack(fill="x", pady=2)
            ttk.Button(F, text="逐层播放 / 路径回放", command=self.play_layers).pack(
                fill="x", pady=2)
            ttk.Button(F, text="查看排程", command=self.show_schedule).pack(fill="x", pady=2)
            ttk.Button(F, text="导出 G-code…", command=self.export_gcode).pack(
                fill="x", pady=2)
        elif domain == "print":
            ttk.Button(F, text="连接虚拟打印机", command=self.connect_device).pack(
                fill="x", pady=2)
            ttk.Button(F, text="开始打印", command=self.start_print,
                       style="Accent.TButton").pack(fill="x", pady=2)
            ttk.Button(F, text="故障注入…", command=self.inject_fault).pack(fill="x", pady=2)
            ttk.Button(F, text="暂停/急停", command=self.pause_estop).pack(fill="x", pady=2)
            ttk.Button(F, text="断电续打…", command=self.resume_from_power_loss).pack(
                fill="x", pady=2)
            ttk.Button(F, text="缺陷记录与归因", command=self.show_defects).pack(
                fill="x", pady=2)
            ttk.Button(F, text="作业历史…", command=self.show_job_history).pack(
                fill="x", pady=2)

    def _build_menu(self):
        m = tk.Menu(self, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT)
        fm = tk.Menu(m, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT)
        fm.add_command(label="新建工程…", command=self.new_project)
        fm.add_command(label="打开示例工程", command=self.open_sample)
        fm.add_command(label="打开工程…", command=self.open_project_dialog)
        fm.add_command(label="保存（提交版本）", command=self.commit_version,
                       accelerator="Ctrl+S")
        fm.add_separator()
        fm.add_command(label="导出工程包…", command=self.export_package)
        fm.add_command(label="导入工程包…", command=self.import_package)
        fm.add_separator()
        fm.add_command(label="重命名工程…", command=self.rename_project)
        fm.add_command(label="复制工程…", command=self.copy_project)
        fm.add_command(label="删除工程（进回收站）…", command=self.delete_project)
        fm.add_command(label="回收站…", command=self.show_trash)
        fm.add_separator()
        fm.add_command(label="校验工程完整性", command=self.check_integrity)
        fm.add_command(label="网格容差…", command=self.set_tess_tol)
        fm.add_command(label="设置…", command=self.show_settings)
        fm.add_separator()
        fm.add_command(label="退出", command=self._on_close)
        m.add_cascade(label="工程", menu=fm)
        em = tk.Menu(m, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT)
        em.add_command(label="撤销", command=self.undo, accelerator="Ctrl+Z")
        em.add_command(label="重做", command=self.redo, accelerator="Ctrl+Y")
        em.add_separator()
        em.add_command(label="导入 STEP…", command=self.import_step)
        em.add_command(label="导入板文件…", command=self.import_board)
        m.add_cascade(label="编辑", menu=em)
        vm = tk.Menu(m, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT)
        vm.add_command(label="三维视图", command=lambda: self.set_view("3d"))
        vm.add_command(label="二维布线视图", command=lambda: self.set_view("2d"))
        vm.add_separator()
        vm.add_command(label="适配窗口", command=self.fit_view, accelerator="Ctrl+0")
        vm.add_command(label="层显隐", command=self.toggle_layer_visible,
                       accelerator="Ctrl+L")
        vm.add_command(label="飞线显示", command=self.toggle_ratlines)
        vm.add_command(label="按网络高亮…", command=self.highlight_net_dialog)
        m.add_cascade(label="视图", menu=vm)
        hm = tk.Menu(m, tearoff=0, bg=PANEL, fg=TEXT, activebackground=ACCENT)
        hm.add_command(label="术语表与快捷键", command=self.show_help)
        hm.add_command(label="查看运行日志", command=self.show_log)
        m.add_cascade(label="帮助", menu=hm)
        self.config(menu=m)
        self.bind_all("<Control-z>", lambda e: self.undo())
        self.bind_all("<Control-y>", lambda e: self.redo())
        self.bind_all("<Control-s>", lambda e: self.commit_version())
        self.bind_all("<Escape>", lambda e: self.cancel_tool())
        # UI-006 快捷键：删除、切换视图、适配窗口（与菜单行为一致）
        self.bind_all("<Delete>", lambda e: self.delete_selected_route())
        self.bind_all("<Tab>", lambda e: self.set_view(
            "2d" if self.view_mode == "3d" else "3d"))
        self.bind_all("<Control-f>", lambda e: self.fit_view())

    def show_help(self):
        """帮助（UI-010）：静态文本 + 全文搜索。"""
        if self.hidden:
            self._show_text("帮助（UI-010）", self.help_text())
            return
        dlg = tk.Toplevel(self)
        dlg.title("帮助：术语、错误码、快捷键（UI-010）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        top = ttk.Frame(dlg, bg=PANEL)
        top.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(top, text="搜索", style="Muted.TLabel").pack(side="left")
        q = tk.StringVar()
        ttk.Entry(top, textvariable=q, width=28).pack(side="left", padx=6)
        body = tk.Text(dlg, bg="#1b1f24", fg=TEXT, relief="flat", wrap="word",
                       font=("Consolas", 10), width=88, height=24)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        cnt = ttk.Label(dlg, text="", style="Muted.TLabel")
        cnt.pack(anchor="w", padx=12)

        def render(*_a):
            kw = q.get().strip().lower()
            body.configure(state="normal")
            body.delete("1.0", "end")
            hits = 0
            for title, items in self.HELP_SECTIONS:
                shown = [(k, v) for k, v in items
                         if not kw or kw in k.lower() or kw in v.lower()
                         or kw in title.lower()]
                if not shown:
                    continue
                body.insert("end", f"【{title}】" + chr(10))
                for k, v in shown:
                    body.insert("end", f"  {k}：{v}" + chr(10))
                    hits += 1
                body.insert("end", chr(10))
            if kw and not hits:
                body.insert("end", f"没有找到与「{kw}」相关的内容。" + chr(10))
            body.configure(state="disabled")
            cnt.configure(text=f"匹配 {hits} 条" if kw else "共 "
                               f"{sum(len(v) for _t, v in self.HELP_SECTIONS)} 条")

        q.trace_add("write", render)
        render()
        ttk.Button(dlg, text="关闭", command=dlg.destroy).pack(padx=12, pady=(0, 12),
                                                               anchor="e")

    def _refresh_print_tab(self):
        if not self.ws:
            return
        dev = self.ws.store.one("SELECT * FROM device ORDER BY online DESC LIMIT 1")
        if dev:
            calib = {}
            try:
                calib = json.loads(dev["calib_json"]) if dev.get("calib_json") else {}
            except (json.JSONDecodeError, TypeError):
                calib = {}
            self.lbl_pr_device.configure(
                text=f"设备：{dev['name']}（{dev['protocol']}）"
                     f"{'在线' if dev['online'] else '离线'}，"
                     f"对位误差 {calib.get('align_err_mm', '—')} mm")
        else:
            self.lbl_pr_device.configure(text="设备：未连接")
        row = self.ws.store.one(
            "SELECT * FROM print_job ORDER BY started_at DESC LIMIT 1")
        if row:
            sp = "—"
            if row.get("safe_point_json"):
                try:
                    d = json.loads(row["safe_point_json"])
                    sp = f"道次 {d['pass_no']}（已打印 {d.get('printed_mm', 0):.1f} mm）"
                except (json.JSONDecodeError, KeyError, TypeError):
                    sp = "记录损坏"
            self.lbl_pr_job.configure(text=f"作业：{row['id'][:8]}  状态 {row['state']}")
            self.lbl_pr_safe.configure(text=f"安全点：{sp}")
        else:
            self.lbl_pr_job.configure(text="作业：无")
            self.lbl_pr_safe.configure(text="安全点：—")
        tree = self.pr_defect_tree
        tree.delete(*tree.get_children())
        if row:
            for d in self.ws.store.query(
                    "SELECT type, pass_no, severity FROM defect WHERE job_id=? "
                    "ORDER BY pass_no", (row["id"],)):
                tree.insert("", "end", values=(d["type"], f"道次 {d['pass_no']}",
                                              d["severity"]))
        self.pr_progress["value"] = (self.last_job or {}).get("progress", 0) \
            if isinstance(self.last_job, dict) else 0

    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL,
                     bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        st.configure("H1.TLabel", background=PANEL, foreground=TEXT,
                     font=("Microsoft YaHei UI", 12, "bold"))
        st.configure("TButton", background="#333941", foreground=TEXT, borderwidth=0,
                     focusthickness=0, padding=(10, 6))
        st.map("TButton", background=[("active", "#3d4550")])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0d1116")
        st.map("Accent.TButton", background=[("active", "#6bb2ff")])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background="#22262c", foreground=MUTED,
                     padding=(12, 6))
        st.map("TNotebook.Tab", background=[("selected", PANEL)],
               foreground=[("selected", TEXT)])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                     foreground=TEXT, rowheight=22, borderwidth=0)
        st.configure("Treeview.Heading", background="#333941", foreground=TEXT,
                     borderwidth=0)
        st.map("Treeview", background=[("selected", "#2f5c8f")])
        st.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                     foreground=TEXT, arrowcolor=TEXT)
        st.configure("Horizontal.TProgressbar", background=ACCENT,
                     troughcolor="#1a1e23", borderwidth=0)

    def _build_print_tab(self):
        """打印面板（UI-004）：设备状态、作业进度、缺陷摘要。"""
        info = ttk.Frame(self.tab_print, style="Panel.TFrame")
        info.pack(fill="x")
        self.lbl_pr_device = ttk.Label(info, text="设备：未连接", style="Muted.TLabel",
                                       wraplength=340)
        self.lbl_pr_device.pack(anchor="w")
        self.lbl_pr_job = ttk.Label(info, text="作业：无", style="Muted.TLabel",
                                    wraplength=340)
        self.lbl_pr_job.pack(anchor="w", pady=(2, 0))
        self.lbl_pr_safe = ttk.Label(info, text="安全点：—", style="Muted.TLabel",
                                     wraplength=340)
        self.lbl_pr_safe.pack(anchor="w", pady=(2, 0))
        self.pr_progress = ttk.Progressbar(self.tab_print, mode="determinate",
                                           maximum=100, value=0)
        self.pr_progress.pack(fill="x", pady=8)

        cols = ("type", "layer", "sev")
        self.pr_defect_tree = ttk.Treeview(self.tab_print, columns=cols,
                                           show="headings", height=8)
        for c, w, t in (("type", 110, "缺陷类型"), ("layer", 80, "层/道次"),
                        ("sev", 50, "级别")):
            self.pr_defect_tree.heading(c, text=t)
            self.pr_defect_tree.column(c, width=w, anchor="w")
        self.pr_defect_tree.pack(fill="both", expand=True)
        row = ttk.Frame(self.tab_print, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="连接设备", command=self.connect_device).pack(side="left")
        ttk.Button(row, text="开始打印", command=self.start_print).pack(side="left", padx=4)
        ttk.Button(row, text="断电续打", command=self.resume_from_power_loss).pack(
            side="left")

    def _target_label(self, issue: dict) -> str:
        """把内部 id 翻成人能看懂的对象名（UI-008：不用内部代号作唯一解释）。"""
        tid = issue.get("target_id", "")
        ttype = issue.get("target_type", "")
        try:
            if ttype == "route":
                r = next((x for x in self.ws.mrc_state()["routes"]
                          if x["id"] == tid), None)
                if r:
                    return f"走线 {r['net']}"
            elif ttype == "face":
                return f"面 {tid}"
            elif ttype in ("trench", "net") and "|" in tid:
                # R2/R7 的 target 是 "走线abc|焊盘R1.1" 这类标签组合
                return tid.replace("走线", "走线 ")[:22]
            elif ttype == "pad":
                p = next((x for x in self.ws.pad_state() if x["id"] == tid), None)
                if p:
                    return f"焊盘 {p['ref']}.{p['pad_no']}"
        except Exception:
            pass
        return tid[:14]

    def _build_layer_tab(self):
        """层列表（UI-004）：物理层 / 道次一览，双击跳到该层播放。"""
        cols = ("pass", "kind", "mat", "z", "h", "paths")
        self.layer_tree = ttk.Treeview(self.tab_layer, columns=cols, show="headings",
                                       height=14)
        for c, w, t in (("pass", 40, "道次"), ("kind", 78, "类型"),
                        ("mat", 56, "材料"), ("z", 52, "Z(mm)"),
                        ("h", 48, "厚(mm)"), ("paths", 48, "路径")):
            self.layer_tree.heading(c, text=t)
            self.layer_tree.column(c, width=w, anchor="w")
        self.layer_tree.pack(fill="both", expand=True)
        self.layer_tree.bind("<Double-Button-1>", self.on_layer_activate)
        row = ttk.Frame(self.tab_layer, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="上一层", command=lambda: self.step_layer(-1)).pack(side="left")
        ttk.Button(row, text="下一层", command=lambda: self.step_layer(1)).pack(
            side="left", padx=4)
        ttk.Button(row, text="播放此层", command=self.play_selected_layer).pack(side="left")
        self.lbl_layer_info = ttk.Label(self.tab_layer, text="尚未切片",
                                        style="Muted.TLabel", wraplength=340)
        self.lbl_layer_info.pack(fill="x", pady=(6, 0))

    def _update_layer_label(self, ws):
        """底栏层/道次信息（UI-001）：显示当前预览道次与总道次，无切片时说明。"""
        if not self.slices:
            self.lbl_layer.configure(text="层：未切片")
            return
        pds = self.slices.get("pass_details") or []
        total = len(pds)
        if self.play_index is not None and pds:
            idx = min(self.play_index, total - 1)
            p = pds[idx]
            kind_cn = {"bottom": "下保护层", "circuit": "电路层", "top": "上保护层"}
            mat_cn = {"A": "银浆", "B": "介电"}
            self.lbl_layer.configure(
                text=f"层：道次 {p['pass_no']}/{total - 1}｜"
                     f"{kind_cn.get(p['kind'], p['kind'])}｜"
                     f"{mat_cn.get(p['mat'], p['mat'])}｜Z={p['z_mm']} mm")
        else:
            self.lbl_layer.configure(
                text=f"层：{self.slices['layer_count']} 物理层 / {total} 道次（未预览）")

    def _fmt_value(self, key: str, v) -> str:
        if isinstance(v, bool) or key == "locked":
            return "是" if v else "否"
        if isinstance(v, float):
            return f"{v:.3f}".rstrip("0").rstrip(".")
        if isinstance(v, (list, tuple)):
            # 路径点只显示点数 + 首尾，避免一屏乱码
            if v and isinstance(v[0], (list, tuple)):
                if len(v) <= 3:
                    return " → ".join(
                        "(" + ",".join(f"{c:.1f}" if isinstance(c, (int, float))
                                       else str(c) for c in p) + ")" for p in v)
                p0, p1 = v[0], v[-1]
                return (f"{len(v)} 个点："
                        f"({','.join(f'{c:.1f}' for c in p0[:2])}) → "
                        f"({','.join(f'{c:.1f}' for c in p1[:2])})")
            return "、".join(str(x) for x in v)
        return str(v)

    def show_log(self):
        """查看运行日志（NFR-006）：关键路径的操作与错误都记录在这里。"""
        from ..logging_setup import log_path
        p = log_path(self.data_root.data_dir)
        if not p or not p.exists():
            self._show_text("运行日志（NFR-006）",
                            f"日志文件尚未产生。\n预期位置：\n"
                            f"{self.data_root.data_dir / 'logs' / 'ame.log'}")
            return
        try:
            lines = p.read_text("utf-8", errors="replace").splitlines()
        except OSError as e:
            self._show_text("运行日志（NFR-006）", f"读取失败：{e}")
            return
        tail = lines[-300:]
        self._show_text(f"运行日志（NFR-006，文件 {p}，共 {len(lines)} 行，"
                        f"显示最近 {len(tail)} 行）", "\n".join(tail))

    def _refresh_layer_tab(self):
        tree = self.layer_tree
        tree.delete(*tree.get_children())
        if not self.slices:
            self.lbl_layer_info.configure(text="尚未切片（在「切片」域点「执行切片」）")
            return
        pds = self.slices.get("pass_details") or []
        for i, p in enumerate(pds):
            tree.insert("", "end", iid=str(i),
                        values=(p["pass_no"], p["kind"], p["mat"],
                                f"{p['z_mm']:.3f}", f"{p['h_mm']:.3f}",
                                len(p["paths"])))
        self.lbl_layer_info.configure(
            text=f"{self.slices['layer_count']} 物理层 / {len(pds)} 道次，"
                 f"总厚 {self.slices.get('total_thickness_mm', 0)} mm"
                 "（双击某层可跳到该层播放）")

    def delete_selected_route(self):
        """Delete 键：删除选中的走线（UI-006，与命令总线一致，可撤销）。"""
        if not self.ws or not self.selected:
            return
        sel = self.selected
        if sel.get("type") == "route":
            try:
                self.ws.cmd_delete_route(sel["data"]["id"])
            except CmdError as e:
                messagebox.showerror("删除失败", str(e))
                return
            self.selected = None
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status("走线已删除（可撤销）", "ok")

    def _build_prop_tab(self):
        self.prop_text = tk.Text(self.tab_prop, bg=PANEL, fg=TEXT, relief="flat",
                                 wrap="word", font=("Consolas", 10))
        self.prop_text.pack(fill="both", expand=True)
        self.prop_text.insert("1.0", "在画布上点选器件、走线或面，这里显示属性。")
        self.prop_text.configure(state="disabled")
        # CIR-010：改线宽 / 网络类 / 封装（修改前先显示影响范围）
        row = ttk.Frame(self.tab_prop, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="编辑属性…", command=self.edit_selected_prop).pack(side="left")

    def _build_issue_tab(self):
        cols = ("sev", "rule", "obj", "reason")
        self.issue_tree = ttk.Treeview(self.tab_issue, columns=cols, show="headings",
                                       height=14)
        for c, w, t in (("sev", 44, "级别"), ("rule", 52, "规则"),
                        ("obj", 90, "对象"), ("reason", 200, "原因")):
            self.issue_tree.heading(c, text=t)
            self.issue_tree.column(c, width=w, anchor="w")
        self.issue_tree.pack(fill="both", expand=True)
        self.issue_tree.bind("<<TreeviewSelect>>", self.on_issue_select)
        row = ttk.Frame(self.tab_issue, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="解释", command=self.explain_selected).pack(side="left")
        ttk.Button(row, text="建议", command=self.suggest_selected).pack(side="left", padx=4)
        ttk.Button(row, text="定位", command=self.locate_selected).pack(side="left")

    def _build_plan_tab(self):
        self.plan_text = tk.Text(self.tab_plan, bg=PANEL, fg=TEXT, relief="flat",
                                 wrap="word", font=("Consolas", 10), height=16)
        self.plan_text.pack(fill="both", expand=True)
        self.plan_text.insert("1.0", "运行设计 Agent 后，这里显示计划与对比卡片。")
        self.plan_text.configure(state="disabled")
        row = ttk.Frame(self.tab_plan, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.auth_var = tk.StringVar(value="step_confirm")
        ttk.Label(row, text="授权", style="Muted.TLabel").pack(side="left")
        cb = ttk.Combobox(row, textvariable=self.auth_var, width=12, state="readonly",
                          values=["suggest", "step_confirm", "auto"])
        cb.pack(side="left", padx=4)
        ttk.Button(row, text="运行", command=self.run_agent).pack(side="left")

    def _build_chat_tab(self):
        self.chat_text = tk.Text(self.tab_chat, bg="#1b1f24", fg=TEXT, relief="flat",
                                 wrap="word", font=("Microsoft YaHei UI", 10))
        self.chat_text.pack(fill="both", expand=True)
        self.chat_text.insert("1.0", "助手：可以问我“当前分数为什么低”“这条走线能不能打”。\n")
        self.chat_text.configure(state="disabled")
        row = ttk.Frame(self.tab_chat, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.chat_entry = ttk.Entry(row)
        self.chat_entry.pack(side="left", fill="x", expand=True)
        self.chat_entry.bind("<Return>", lambda e: self.send_chat())
        ttk.Button(row, text="发送", command=self.send_chat).pack(side="left", padx=4)

    def _fill_issues(self):
        self.issue_tree.delete(*self.issue_tree.get_children())
        self._issue_rows = []
        for i in self.mrc.get("issues", []):
            item = self.issue_tree.insert(
                "", "end",
                values=(f"S{i['severity']}", i["rule_code"],
                        self._target_label(i), i["reason"]))
            self._issue_rows.append((item, i))

    def _show_prop(self, obj: dict):
        lines = []
        for k, v in obj.items():
            if k in self.PROP_HIDDEN:
                continue
            label = self.PROP_LABELS.get(k, k)
            lines.append(f"{label}：{self._fmt_value(k, v)}")
        txt = "\n".join(lines) or "（无属性）"
        self.prop_text.configure(state="normal")
        self.prop_text.delete("1.0", "end")
        self.prop_text.insert("1.0", txt)
        self.prop_text.configure(state="disabled")
        self.nb.select(self.tab_prop)

    def _show_text(self, title, body):
        self.last_dialog = {"title": title, "body": body}
        if self.hidden:
            return          # 自动化/测试模式：不弹出窗口
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.configure(bg=PANEL)
        dlg.geometry("760x560")
        dlg.transient(self)
        t = tk.Text(dlg, bg="#1b1f24", fg=TEXT, relief="flat", wrap="word",
                    font=("Consolas", 10))
        t.pack(fill="both", expand=True, padx=12, pady=12)
        t.insert("1.0", body)
        t.configure(state="disabled")

    def _record_box(self, kind):
        def _f(title, message="", **kw):
            self.dialogs.append({"kind": kind, "title": title, "message": message})
            if len(self.dialogs) > self.DIALOG_LOG_MAX:
                del self.dialogs[: len(self.dialogs) - self.DIALOG_LOG_MAX]
            return "ok"
        return _f

    def status(self, text, kind="info"):
        color = {"info": MUTED, "warn": WARN, "error": BAD, "ok": GOOD}.get(kind, MUTED)
        self.lbl_status.configure(text=text, foreground=color)
        self.update_idletasks()

    def progress_set(self, pct: float):
        self.progress["value"] = max(0.0, min(100.0, pct))
        self.update_idletasks()

    def enter_domain(self, key: str):
        self._render_domain_actions(key)
        if key == "mrc" and self.ws:
            self.run_mrc()
        if key == "slicing":
            self.set_view("2d")
        self.status(f"进入 {key} 域")

    @classmethod
    def help_text(cls) -> str:
        out = []
        for title, items in cls.HELP_SECTIONS:
            out.append(title)
            for k, v in items:
                out.append(f"  {k}：{v}")
            out.append("")
        return chr(10).join(out).rstrip()

    def on_layer_activate(self, _event=None):
        sel = self.layer_tree.selection()
        if not sel:
            return
        self.play_index = int(sel[0])
        self.redraw()
        self.status(f"已切到道次 {self.play_index + 1}（双击层列表）", "ok")

    def play_selected_layer(self):
        if not self.slices:
            messagebox.showinfo("层", "尚未切片。")
            return
        self.on_layer_activate()

    def step_layer(self, delta: int):
        """上一个/下一个道次（SLC-006 逐层播放）。"""
        if not self.slices:
            return
        n = len(self.slices.get("pass_details") or [])
        if n == 0:
            return
        cur = self.play_index if self.play_index is not None else 0
        self.play_index = max(0, min(n - 1, cur + delta))
        pds = self.slices.get("pass_details") or []
        p = pds[self.play_index]
        self.redraw()
        self.status(f"道次 {p['pass_no']}（{p['kind']}/{p['mat']}）"
                    f"路径 {len(p['paths'])} 条", "ok")

    def _on_close(self):
        try:
            if self.ws:
                self.ws.close()
                self.ws.store.close()
        except Exception:
            pass
        self.destroy()

