"""走线域动作与对话框（CIR-001..012） —— 从 app.py 拆出的域动作（Mixin）。

本文件只含方法体（逐字搬运），`App` 继承后对外接口不变。
拆分动机：原 app.py 3500 行 131 个方法，单个域的对话框代码
与其它域混在一处，定位和评审都要跨越大量无关代码。
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog as sd, ttk

from ...ledger.workspace import CmdError
from ..theme import ACCENT, PANEL, TEXT


class CircuitMixin:
    def import_board(self):
        if not self._require_ws():
            return
        p = filedialog.askopenfilename(
            filetypes=[("KiCad 板文件", "*.kicad_pcb"), ("网表 JSON", "*.json"),
                       ("全部文件", "*.*")])
        if not p:
            return
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            messagebox.showerror("读取失败", "E-FILE-READ" + chr(10) + str(e))
            return
        fmt = "netlist+xy" if p.lower().endswith(".json") else "kicad_pcb"
        try:
            r = self.ws.cmd_import_board(text, fmt)
        except (CmdError, ValueError) as e:
            messagebox.showerror("导入失败", f"E-BOARD-IMPORT\n{e}")
            self.status(str(e), "error")
            return
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        self.fit_view()
        self.status(f"板文件已导入：{r['comps']} 器件 / {r['pads']} 焊盘 / "
                    f"{r['nets']} 网络 / {r.get('routes', 0)} 走线", "ok")

    def set_tool(self, tool):
        self.tool = tool
        self._drag_pts = []
        self.route_net = None
        if tool == "route":
            self.set_view("2d")
            self.status("布线工具：先点一个焊盘选网络，再依次点落点，"
                        "双击结束；Esc 取消（CIR-004）")
        elif tool is None:
            self.status("已取消当前工具")

    def _pad_near(self, wx: float, wy: float, tol: float = 2.5):
        """找 (wx, wy) 附近容差内的焊盘（CIR-004 选网络用）。"""
        if not self.ws:
            return None
        best, bd = None, tol
        for p in self.ws.pad_state():
            d = math.hypot(p["x"] - wx, p["y"] - wy)
            if d < bd:
                best, bd = p, d
        return best

    def finish_route(self, _event=None):
        """双击结束布线：落库并校验（CIR-004）。

        Tk 在双击时会先发两次 <Button-1> 再发 <Double-Button-1>，所以
        _drag_pts 末尾通常有两个几乎重合的点；去掉重复点再落线，
        否则会被 CIR-011 的"重复点/零长度段"校验拒掉。
        """
        if self.tool != "route":
            return
        pts = []
        for p in self._drag_pts:
            if not pts or math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) > 1e-6:
                pts.append(p)
        net = self.route_net
        self._drag_pts = []
        self.tool = None
        if len(pts) < 2 or not net:
            self.status("布线取消：至少需要 2 个落点且已选网络（CIR-004）", "warn")
            self.redraw()
            return
        try:
            self.ws.cmd_add_route(net, [list(p) for p in pts], 0.6, "top")
        except CmdError as e:
            messagebox.showerror("布线未通过校验（CIR-011）", str(e))
            self.status(f"布线失败：{e}", "error")
            self.redraw()
            return
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        self.status(f"已布 1 条走线（网络 {net}，{len(pts)} 点）"
                    f"（CIR-004，可 Ctrl+Z 撤销）", "ok")

    def cancel_tool(self):
        if self.tool:
            self._drag_pts = []
            self.route_net = None
            self.tool = None
            self.status("已取消当前工具")
            self.redraw()

    def check_connectivity(self):
        if not self._require_ws():
            return
        from ...circuit.connectivity import check_connectivity
        st = self.ws.mrc_state()
        nets = sorted({p["net"] for p in st["pads"] if p["net"]} |
                      {r["net"] for r in st["routes"]})
        res = check_connectivity(nets, st["pads"], st["routes"], st["jumpers"])
        if res["ok"]:
            self.status(f"连通性通过：{len(nets)} 个网络全部连通，无短路", "ok")
            messagebox.showinfo("连通性检查", f"通过：{len(nets)} 个网络全部连通，无短路。")
        else:
            lines = [f"断路：{o['net']} 在 {o['at']}" for o in res["opens"]]
            lines += [f"短路：{s['nets']} 在 {s['at']}" for s in res["shorts"]]
            self.status(f"连通性失败：{len(res['opens'])} 断路 / {len(res['shorts'])} 短路",
                        "error")
            messagebox.showerror("连通性检查", "\n".join(lines))

    def jumper_candidates(self):
        if not self._require_ws():
            return
        from ...circuit.connectivity import jumper_candidates
        st = self.ws.mrc_state()
        cands = jumper_candidates(st["pads"], st["routes"], st["jumpers"])
        if not cands:
            messagebox.showinfo("跳线候选", "当前没有断开的网络，无需跳线。")
            return
        dlg = tk.Toplevel(self)
        dlg.title("跳线候选（CIR-007：只建议，不自动应用）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        lb = tk.Listbox(dlg, width=80, height=10, bg="#1b1f24", fg=TEXT,
                        selectbackground=ACCENT, relief="flat")
        for c in cands:
            lb.insert("end", c["reason"])
        lb.pack(padx=12, pady=12)

        def apply_sel():
            sel = lb.curselection()
            if not sel:
                return
            c = cands[sel[0]]
            self.ws.cmd_add_jumper(c["net"], c["from_pad"], c["to_pad"], c["reason"])
            dlg.destroy()
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status("跳线已应用（0Ω），请复查连通性", "ok")
        ttk.Button(dlg, text="应用选中跳线", command=apply_sel,
                   style="Accent.TButton").pack(padx=12, pady=(0, 12), anchor="e")

    def manage_keepouts(self):
        """禁布区管理（CIR-006）：查看列表、手动输入矩形禁区、删除。"""
        if not self._require_ws():
            return
        if self.hidden:
            self._show_text("禁布区（CIR-006）",
                            "\n".join(f"{k['name']}（{len(k['pts'])} 点）"
                                      for k in self.ws.keepouts()) or "暂无禁布区")
            return
        dlg = tk.Toplevel(self)
        dlg.title("禁布区（CIR-006：自动布线与手动落线都会避开）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        lb = tk.Listbox(dlg, width=56, height=8, bg="#1b1f24", fg=TEXT,
                        selectbackground=ACCENT, relief="flat")
        for k in self.ws.keepouts():
            lb.insert("end", f"{k['name']}  （{len(k['pts'])} 个顶点）")
        lb.pack(padx=12, pady=(12, 6))

        row = ttk.Frame(dlg, bg=PANEL)
        row.pack(fill="x", padx=12, pady=4)
        ttk.Label(row, text="矩形禁区 x0,y0,x1,y1：", style="Muted.TLabel").pack(side="left")
        x0v = tk.StringVar(value="30")
        y0v = tk.StringVar(value="30")
        x1v = tk.StringVar(value="60")
        y1v = tk.StringVar(value="50")
        for v in (x0v, y0v, x1v, y1v):
            ttk.Entry(row, textvariable=v, width=6).pack(side="left", padx=2)
        namev = tk.StringVar(value="禁布区")
        ttk.Entry(row, textvariable=namev, width=12).pack(side="left", padx=(8, 2))

        def add():
            try:
                x0, y0, x1, y1 = (float(x0v.get()), float(y0v.get()),
                                  float(x1v.get()), float(y1v.get()))
            except ValueError:
                messagebox.showerror("参数错误", "四个坐标都必须是数字", parent=dlg)
                return
            if x1 <= x0 or y1 <= y0:
                messagebox.showerror("参数错误", "需满足 x1>x0 且 y1>y0", parent=dlg)
                return
            try:
                r = self.ws.cmd_add_keepout(
                    [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], namev.get())
            except CmdError as e:
                messagebox.showerror("新增失败", str(e), parent=dlg)
                return
            dlg.destroy()
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status(f"已新增禁布区 {r['name']}（{r['area_mm2']} mm²），"
                        "自动布线与手动落线都会避开（CIR-006）", "ok")

        def remove():
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("禁布区", "请先选中一项", parent=dlg)
                return
            kid = self.ws.keepouts()[sel[0]]["id"]
            try:
                self.ws.cmd_delete_keepout(kid)
            except CmdError as e:
                messagebox.showerror("删除失败", str(e), parent=dlg)
                return
            dlg.destroy()
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status("禁布区已删除（可 Ctrl+Z 撤销）", "ok")

        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Button(bar, text="新增矩形禁区", command=add,
                   style="Accent.TButton").pack(side="left")
        ttk.Button(bar, text="删除选中", command=remove).pack(side="left", padx=6)
        ttk.Button(bar, text="关闭", command=dlg.destroy).pack(side="right")

    def auto_route(self):
        """单网络自动布线（CIR-006）：选网络 → 看避障候选 → 选择后落线（可撤销）。"""
        if not self._require_ws():
            return
        nets = [n["name"] for n in self.ws.nets()]
        if not nets:
            messagebox.showinfo("自动布线", "还没有导入板文件。")
            return
        dlg = tk.Toplevel(self)
        dlg.title("自动布线（CIR-006：单网络，失败给出原因）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text="网络", style="Panel.TLabel").grid(row=0, column=0,
                                                               padx=10, pady=8)
        var = tk.StringVar(value=nets[0])
        ttk.Combobox(dlg, textvariable=var, values=nets, state="readonly",
                     width=20).grid(row=0, column=1, padx=10, pady=8)
        lb = tk.Listbox(dlg, width=76, height=8, bg="#1b1f24", fg=TEXT,
                        selectbackground=ACCENT, relief="flat")
        lb.grid(row=1, column=0, columnspan=2, padx=10, pady=4)
        state = {"cands": []}

        def gen():
            lb.delete(0, "end")
            state["cands"] = []
            try:
                cands = self.ws.cmd_auto_route_candidates(var.get())
            except CmdError as e:
                messagebox.showerror("自动布线", str(e))
                return
            state["cands"] = cands
            for c in cands:
                if c.get("failed"):
                    lb.insert("end", f"✗ 无法布通：{c['reason']}")
                else:
                    lb.insert("end", f"✓ 长度 {c['length_mm']} mm，"
                                     f"{c['turns']} 个转弯（可撤销后重试其他候选）")
        ttk.Button(dlg, text="生成候选", command=gen).grid(row=2, column=0,
                                                          padx=10, pady=4)

        def apply_sel():
            sel = lb.curselection()
            if not sel:
                return
            c = state["cands"][sel[0]]
            if c.get("failed"):
                messagebox.showwarning("无法布通", c["reason"])
                return
            try:
                r = self.ws.cmd_add_route(c["net"], c["pts"], c["width"])
            except CmdError as e:
                messagebox.showerror("落线被拒", str(e))
                return
            dlg.destroy()
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status(f"自动布线 {c['net']} 完成（长度 {c['length_mm']} mm，"
                        f"走线 {r['id'][:8]}，可撤销）", "ok")
        ttk.Button(dlg, text="应用选中候选", command=apply_sel,
                   style="Accent.TButton").grid(row=2, column=1, padx=10, pady=4,
                                                sticky="e")

    def auto_place(self):
        """自动布局候选（CIR-005）：列出候选与飞线变化，用户选择后应用（可撤销）。"""
        if not self._require_ws():
            return
        try:
            cands = self.ws.cmd_place_candidates()
        except CmdError as e:
            messagebox.showerror("自动布局", str(e))
            return
        if not cands:
            messagebox.showinfo("自动布局",
                                "当前布局已是最优（或所有网络都已布线），没有更优候选。")
            return
        dlg = tk.Toplevel(self)
        dlg.title("自动布局候选（CIR-005：只建议，选择后应用）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        lb = tk.Listbox(dlg, width=76, height=8, bg="#1b1f24", fg=TEXT,
                        selectbackground=ACCENT, relief="flat")
        for c in cands:
            lb.insert("end", f"{c['name']}：飞线 {c['baseline_mm']:.1f} → "
                             f"{c['ratline_mm']:.1f} mm（{c['delta_mm']:+.1f}），"
                             f"重叠 {c['overlaps']} 处")
        lb.pack(padx=12, pady=12)

        def apply_sel():
            sel = lb.curselection()
            if not sel:
                return
            c = cands[sel[0]]
            r = self.ws.cmd_apply_placement(c["positions"])
            dlg.destroy()
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status(f"布局已应用（{r['applied']} 器件移动，"
                        f"飞线 {c['baseline_mm']:.1f}→{c['ratline_mm']:.1f} mm，可撤销）",
                        "ok")
        ttk.Button(dlg, text="应用选中布局", command=apply_sel,
                   style="Accent.TButton").pack(padx=12, pady=(0, 12), anchor="e")

    def undo(self):
        if not self.ws:
            return
        msg = self.ws.undo()
        self.mrc = self.ws.run_mrc() if self.ws.routes() else self.mrc
        self._refresh_all()
        self.status(msg, "ok")

    def redo(self):
        if not self.ws:
            return
        msg = self.ws.redo()
        self.mrc = self.ws.run_mrc() if self.ws.routes() else self.mrc
        self._refresh_all()
        self.status(msg, "ok")

    def edit_selected_prop(self):
        """改线宽 / 网络类 / 封装（CIR-010），先展示影响范围再执行。"""
        if not self._require_ws() or not self.selected:
            messagebox.showinfo("编辑属性", "请先在画布上选中一个器件或走线。")
            return
        data = self.selected.get("data") or {}
        kind = self.selected.get("type")
        if self.hidden:
            self._show_text("编辑属性（CIR-010）",
                            f"选中类型 {kind}；隐藏模式不做交互输入。")
            return
        if kind == "route":
            cur = data.get("width") or data.get("width_mm")
            val = sd.askfloat("改线宽（CIR-010）",
                              f"网络 {data.get('net')} 当前线宽 {cur} mm。\n"
                              f"工艺下限 {0.5} mm，低于下限会被拒绝。",
                              initialvalue=float(cur or 0.6), minvalue=0.01,
                              maxvalue=5.0, parent=self)
            if val is None:
                return
            try:
                self.ws.cmd_change_width(data["id"], val)
            except CmdError as e:
                messagebox.showerror("改线宽失败", str(e))
                return
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status(f"线宽已改为 {val} mm（影响：该走线所在的网络 {data.get('net')}）", "ok")
            return
        if kind == "comp":
            footprint = data.get("footprint") or ""
            new_fp = self._simple_ask("改封装（CIR-010）",
                                      f"器件 {data.get('ref')} 当前封装：\n{footprint}\n\n"
                                      "输入新封装名：", footprint)
            if not new_fp:
                return
            try:
                scope = self.ws.cmd_set_footprint(data["id"], new_fp, dry_run=True)["scope"]
            except CmdError as e:
                messagebox.showerror("改封装失败", str(e))
                return
            if not messagebox.askyesno(
                    "确认影响范围（CIR-010）",
                    f"封装将改为：{new_fp}\n"
                    f"影响：{scope['ref']} 的 {scope['pads']} 个焊盘（几何需重新确认）。\n\n"
                    "确定应用吗？"):
                return
            try:
                self.ws.cmd_set_footprint(data["id"], new_fp)
            except CmdError as e:
                messagebox.showerror("改封装失败", str(e))
                return
            self.mrc = self.ws.run_mrc()
            self._refresh_all()
            self.status(f"封装已改为 {new_fp}（影响 {scope['pads']} 个焊盘）", "ok")
            return
        messagebox.showinfo("编辑属性", f"该对象类型（{kind}）暂不支持编辑。")

