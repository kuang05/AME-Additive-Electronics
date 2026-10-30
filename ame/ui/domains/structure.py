"""结构域动作与对话框（STR-001..009） —— 从 app.py 拆出的域动作（Mixin）。

本文件只含方法体（逐字搬运），`App` 继承后对外接口不变。
拆分动机：原 app.py 3500 行 131 个方法，单个域的对话框代码
与其它域混在一处，定位和评审都要跨越大量无关代码。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ...ledger.workspace import CmdError
from ..theme import PANEL


class StructureMixin:
    def import_step(self):
        if not self._require_ws():
            return
        p = filedialog.askopenfilename(
            filetypes=[("STEP", "*.step *.stp"), ("全部文件", "*.*")])
        if not p:
            return
        self.progress_set(15)
        try:
            # 显式 with：Windows 上未关闭的句柄会锁住文件，
            # 用户随后保存/删除该文件会被拒绝（PermissionError）。
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            self.progress_set(0)
            messagebox.showerror("读取失败", f"E-FILE-READ\n{e}")
            return
        self.progress_set(55)
        try:
            r = self.ws.cmd_import_step(text, p.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        except CmdError as e:
            self.progress_set(0)
            messagebox.showerror("导入失败", f"E-STEP-IMPORT\n{e}")
            self.status(str(e), "error")
            return
        self.progress_set(100)
        # UI-008：诊断码要配中文说明，不能只给内部代号。
        # 诊断可能有多条（每个面级问题都会汇总成一条），状态栏放不下，
        # 因此状态栏只给条数，完整清单放进弹窗，用户能逐条看影响与建议。
        if r["diagnostics"]:
            n = len(r["diagnostics"])
            self.status(f"STEP 已导入：{r['face_count']} 面，{n} 项诊断"
                        f"（点「查看诊断」看详情）", "warn")
            self._show_import_diagnostics(r)
        else:
            self.status(f"STEP 已导入：{r['face_count']} 面，无诊断问题", "ok")
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        self.fit_view()
        self.progress_set(0)

    def _show_import_diagnostics(self, r: dict):
        """把 STEP 导入诊断列成清单（STR-002：不静默，且告诉用户怎么办）。"""
        lines = []
        for d in r["diagnostics"]:
            lines.append(f"[{d['code']}]\n  {d['msg']}\n  建议：{d.get('advice', '—')}")
        self._show_text(
            f"STEP 导入诊断（{len(r['diagnostics'])} 项）",
            "以下问题已如实记录，软件不会替你猜着修补（STR-002）。\n\n"
            + "\n\n".join(lines))

    def show_face_analysis(self):
        if not self._require_ws():
            return
        m = self.ws.step_model()
        if not m:
            messagebox.showinfo("面分析", "还没有导入 STEP。")
            return
        from ...geometry.analysis import analyze_faces
        rows = analyze_faces(m, 45.0)
        bad = [r for r in rows if not r.printable]
        text = (f"共 {len(rows)} 个面；不利于打印 {len(bad)} 个。\n\n"
                + "\n".join(f"{r.fid}  {r.kind}  法向{tuple(round(v, 2) for v in r.normal)}  "
                            f"面积 {r.area:.0f} mm²  坡度 {r.slope_deg:.0f}°  {r.hint}"
                            for r in rows[:40]))
        self._show_text("面分析（STR-004）", text)

    def show_boolean(self):
        if not self._require_ws():
            return
        m = self.ws.step_model()
        if not m:
            messagebox.showinfo("布尔建议", "还没有导入 STEP。")
            return
        from ...geometry.analysis import suggest_booleans
        outline, _ = self.ws.board_outline()
        if not outline:
            messagebox.showwarning("布尔建议", "需要先定义板框。")
            return
        sugs = suggest_booleans(m, outline)
        if self.hidden:
            self.feature_preview = [dict(s.params, prim=s.prim, kind=s.kind,
                                         desc=s.desc) for s in sugs]
            self.view_mode = "3d"
            self.redraw()
            self._show_text("布尔建议（STR-005 预览）",
                            "\n".join(f"{s.prim} {s.kind} {s.desc}" for s in sugs))
            return

        dlg = tk.Toplevel(self)
        dlg.title("布尔建议（STR-005：参数可调，可预览，不执行）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()

        editors: list[dict] = []
        for i, s in enumerate(sugs):
            box = ttk.LabelFrame(dlg, text=f"[{i + 1}] {s.kind.upper()} {s.prim} — {s.desc}",
                                 padding=6)
            box.pack(fill="x", padx=12, pady=4)
            vars_: dict[str, tk.StringVar] = {}
            for j, (k, v) in enumerate(s.params.items()):
                ttk.Label(box, text=self.PROP_LABELS.get(k, k), style="Muted.TLabel").grid(
                    row=j // 3, column=(j % 3) * 2, padx=(0, 2), pady=2, sticky="e")
                var = tk.StringVar(value=str(v))
                vars_[k] = var
                ttk.Entry(box, textvariable=var, width=9).grid(
                    row=j // 3, column=(j % 3) * 2 + 1, padx=(0, 8), pady=2)
            editors.append({"sug": s, "vars": vars_})
            bar = ttk.Frame(box)
            bar.grid(row=99, column=0, columnspan=6, sticky="e", pady=(4, 0))
            ttk.Button(bar, text="预览",
                       command=lambda i=i: self._preview_feature(i, editors,
                                                                 dlg)).pack(side="left")
            ttk.Button(bar, text="接受（加入特征列表）",
                       command=lambda i=i: self._accept_feature(i, editors,
                                                                dlg)).pack(
                side="left", padx=4)
            ttk.Button(bar, text="拒绝",
                       command=lambda i=i: self._reject_feature(i, editors,
                                                               dlg)).pack(side="left")

        note = ttk.Label(dlg, text="预览只在画布上画线框，不改变模型与分数"
                                   "（首版不执行布尔 —— STR-005）",
                         style="Muted.TLabel", wraplength=520)
        note.pack(fill="x", padx=12, pady=(4, 2))
        ttk.Button(dlg, text="关闭", command=lambda: self._clear_preview(dlg)).pack(
            padx=12, pady=(0, 12), anchor="e")

    def _feature_params(self, ed: dict) -> dict:
        out = {}
        for k, var in ed["vars"].items():
            try:
                out[k] = float(var.get())
            except ValueError as e:
                raise CmdError(
                    f"参数 {self.PROP_LABELS.get(k, k)} 不是数字："
                    f"{var.get()!r}") from e
        return out

    def _preview_feature(self, idx: int, editors: list, dlg):
        try:
            params = self._feature_params(editors[idx])
        except CmdError as e:
            messagebox.showerror("参数错误", str(e), parent=dlg)
            return
        s = editors[idx]["sug"]
        self.feature_preview = [dict(params, prim=s.prim, kind=s.kind, desc=s.desc)]
        if self.view_mode != "3d":
            self.view_mode = "3d"     # 布尔体是三维几何，在二维视图里看不到
            self.lbl_view.configure(text="三维（等轴测）")
        self.redraw()
        self.status(f"预览：{s.desc}（绿=加料/红=减料 线框，未写入工程）", "ok")

    def _accept_feature(self, idx: int, editors: list, dlg):
        try:
            params = self._feature_params(editors[idx])
        except CmdError as e:
            messagebox.showerror("参数错误", str(e), parent=dlg)
            return
        s = editors[idx]["sug"]
        self.ws.cmd_add_feature(s.prim, s.kind, params, s.desc)
        self._clear_preview(dlg)
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        self.status("已加入特征列表（待执行，V1 才执行布尔）", "ok")

    def _reject_feature(self, idx: int, editors: list, dlg):
        try:
            params = self._feature_params(editors[idx])
        except CmdError as e:
            messagebox.showerror("参数错误", str(e), parent=dlg)
            return
        s = editors[idx]["sug"]
        f = self.ws.cmd_add_feature(s.prim, s.kind, params, s.desc)
        self.ws.cmd_reject_feature(f["id"])
        self._clear_preview(dlg)
        self.status("已拒绝布尔建议：模型与分数不变", "ok")

    def _clear_preview(self, dlg=None):
        self.feature_preview = []
        self.redraw()
        if dlg is not None:
            dlg.destroy()

    def manual_outline(self):
        if not self._require_ws():
            return
        m = self.ws.step_model()
        from ...geometry.analysis import manual_outline
        if not m:
            messagebox.showinfo("手工板框", "还没有导入 STEP。")
            return
        x0, y0, _z0, x1, y1, _z1 = m.bbox
        r = self.ws.cmd_set_outline(manual_outline(
            round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)))
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        if r.get("warning"):
            self.status(f"板框已设置，但需确认：{r['warning']}", "warn")
        else:
            self.status(f"板框已设为包围盒 ({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f})", "ok")

    def show_features(self):
        """特征列表（STR-006）：启用/停用、上移/下移、删除。"""
        if not self._require_ws():
            return
        fs = self.ws.features()
        if not fs:
            messagebox.showinfo("特征列表", "还没有布尔特征。")
            return
        if self.hidden:
            self._show_text("特征列表（STR-006）",
                            "\n".join(f"{f['id'][:8]}  {f['kind']}:{f['prim']}  "
                                      f"状态 {f['status']}  启用 {f['enabled']}  {f['note']}"
                                      for f in fs))
            return
        dlg = tk.Toplevel(self)
        dlg.title("特征列表（STR-006：布尔按列表顺序执行）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        cols = ("seq", "prim", "kind", "status", "enabled", "note")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=10)
        for c, w, t in (("seq", 40, "序"), ("prim", 70, "类型"),
                        ("kind", 50, "加减"), ("status", 70, "状态"),
                        ("enabled", 50, "启用"), ("note", 240, "说明")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=12, pady=12)

        def refresh():
            tree.delete(*tree.get_children())
            for i, f in enumerate(self.ws.features()):
                tree.insert("", "end", iid=f["id"],
                            values=(i, f["prim"],
                                    "加料" if f["kind"] == "add" else "减料",
                                    f["status"],
                                    "是" if f["enabled"] else "否", f["note"]))

        refresh()

        def _sel():
            s = tree.selection()
            if not s:
                messagebox.showinfo("特征列表", "请先选中一项", parent=dlg)
                return None
            return s[0]

        def toggle():
            fid = _sel()
            if not fid:
                return
            row = next((f for f in self.ws.features() if f["id"] == fid), None)
            self.ws.cmd_set_feature_enabled(fid, not bool(row["enabled"]))
            refresh()
            self.status(f"特征已{'停用' if row['enabled'] else '启用'}", "ok")

        def move(delta):
            fid = _sel()
            if not fid:
                return
            order = [f["id"] for f in self.ws.features()]
            i = order.index(fid)
            j = i + delta
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                self.ws.cmd_reorder_features(order)
                refresh()
                tree.selection_set(fid)
                self.status("特征顺序已调整（布尔按此顺序执行）", "ok")

        def remove():
            fid = _sel()
            if not fid:
                return
            self.ws.cmd_delete_feature(fid)
            refresh()
            self.status("特征已删除（可 Ctrl+Z 撤销）", "ok")

        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(bar, text="启用/停用", command=toggle).pack(side="left")
        ttk.Button(bar, text="上移", command=lambda: move(-1)).pack(side="left", padx=4)
        ttk.Button(bar, text="下移", command=lambda: move(1)).pack(side="left")
        ttk.Button(bar, text="删除", command=remove).pack(side="left", padx=4)
        ttk.Button(bar, text="关闭", command=dlg.destroy).pack(side="right")

    def set_section(self):
        """STR-003 剖切：按 Z 阈值裁掉上方几何，只显示剖面以下部分。"""
        if not self._require_ws():
            return
        m = self.ws.step_model()
        if not m:
            messagebox.showinfo("剖切", "还没有导入 STEP。")
            return
        dlg = tk.Toplevel(self)
        dlg.title("剖切（STR-003）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        z0, z1 = m.bbox[2], m.bbox[5]
        ttk.Label(dlg, text=f"剖切高度 Z（{z0:.2f} ~ {z1:.2f} mm）",
                  style="Panel.TLabel").grid(row=0, column=0, padx=10, pady=8)
        val = tk.StringVar(value=f"{z1:.2f}")
        ttk.Entry(dlg, textvariable=val, width=12).grid(row=0, column=1, padx=10, pady=8)

        def apply(section):
            self.section_z = section
            self.redraw()
            self.status(f"剖切高度 Z={section:.2f} mm" if section is not None else "已取消剖切")

        def ok():
            try:
                z = float(val.get())
            except ValueError:
                messagebox.showerror("参数错误", "剖切高度必须是数字")
                return
            dlg.destroy()
            apply(z)
        row = ttk.Frame(dlg, style="Panel.TFrame")
        row.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 12), sticky="e")
        ttk.Button(row, text="取消剖切", command=lambda: (dlg.destroy(), apply(None))).pack(
            side="left", padx=4)
        ttk.Button(row, text="应用", command=ok, style="Accent.TButton").pack(side="left")

    def measure(self):
        """STR-003 测量：在二维视图点两点，给出距离。"""
        if not self._require_ws():
            return
        self.set_view("2d")
        self.measure_pts = []
        self.set_tool("measure")
        self.status("测量：在画布上点击两个点，显示两点距离（STR-003）")

    def play_layers(self):
        """SLC-006 逐层播放：按道次逐步显示路径，可显示起停点。"""
        if not self.slices:
            messagebox.showinfo("逐层播放", "请先切片。")
            return
        passes = self.slices.get("pass_details") or []
        self.play_index = 0
        self.play_window = tk.Toplevel(self)
        self.play_window.title("逐层播放（SLC-006）")
        self.play_window.configure(bg=PANEL)
        self.play_window.geometry("360x150")
        if self.hidden:
            self.play_window.withdraw()
        ttk.Label(self.play_window, text="道次 / 物理层 / 材料 / 路径数",
                  style="Panel.TLabel").pack(pady=(12, 4))
        self.play_label = ttk.Label(self.play_window, text="", style="Panel.TLabel",
                                   font=("Consolas", 10))
        self.play_label.pack()
        row = ttk.Frame(self.play_window, style="Panel.TFrame")
        row.pack(pady=12)

        def show(idx):
            idx = max(0, min(len(passes) - 1, idx))
            self.play_index = idx
            p = passes[idx]
            self.play_label.configure(
                text=f"{p['pass_no']}/{len(passes) - 1}  {p['kind']}  "
                     f"材料 {p['mat']}  Z={p['z_mm']}  路径 {len(p['paths'])} 条")
            self._update_layer_label(self.ws)
            self.redraw()

        ttk.Button(row, text="上一道次", command=lambda: show(self.play_index - 1)).pack(
            side="left", padx=4)
        ttk.Button(row, text="下一道次", command=lambda: show(self.play_index + 1)).pack(
            side="left", padx=4)
        show(0)

