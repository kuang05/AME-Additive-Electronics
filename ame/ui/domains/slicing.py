"""切片域动作与参数/材料对话框（SLC-001..013） —— 从 app.py 拆出的域动作（Mixin）。

本文件只含方法体（逐字搬运），`App` 继承后对外接口不变。
拆分动机：原 app.py 3500 行 131 个方法，单个域的对话框代码
与其它域混在一处，定位和评审都要跨越大量无关代码。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ...ledger.services import (do_slice, gcode_of,
                                latest_slice)
from ...ledger.workspace import CmdError
from ..theme import PANEL


class SlicingMixin:
    def edit_materials(self):
        """材料库（SLC-013）：新增材料、启停；停用材料不出现在切片选择列表。"""
        if not self._require_ws():
            return
        dlg = tk.Toplevel(self)
        dlg.title("材料库（SLC-013）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        tree = ttk.Treeview(dlg, columns=("id", "name", "role", "batch", "en"),
                            show="headings", height=8)
        for c, w, t in (("id", 90, "编号"), ("name", 150, "名称"), ("role", 90, "角色"),
                        ("batch", 100, "批次"), ("en", 60, "启用")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")

        def reload_rows():
            tree.delete(*tree.get_children())
            for m in self.ws.materials():
                tree.insert("", "end", iid=m["id"],
                            values=(m["id"], m["name"], m["role"], m["batch"],
                                    "是" if m["enabled"] else "否"))
        reload_rows()
        tree.pack(padx=12, pady=12, fill="x")
        row = ttk.Frame(dlg, style="Panel.TFrame")
        row.pack(fill="x", padx=12, pady=(0, 8))
        name_v = tk.StringVar()
        role_v = tk.StringVar(value="conductor")
        ttk.Label(row, text="名称", style="Muted.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=name_v, width=18).pack(side="left", padx=4)
        ttk.Label(row, text="角色", style="Muted.TLabel").pack(side="left")
        ttk.Combobox(row, textvariable=role_v, state="readonly", width=12,
                     values=["conductor", "dielectric", "protect"]).pack(
            side="left", padx=4)

        def add():
            if not name_v.get().strip():
                messagebox.showwarning("材料库", "请输入材料名称")
                return
            try:
                r = self.ws.cmd_add_material(name_v.get(), role_v.get())
            except CmdError as e:
                messagebox.showerror("新增失败", str(e))
                return
            name_v.set("")
            reload_rows()
            self.status(f"材料已新增（{r['id']}），可用于切片", "ok")

        def toggle():
            sel = tree.selection()
            if not sel:
                return
            try:
                mid = sel[0]
                cur = self.ws.store.one("SELECT enabled FROM material_profile WHERE id=?",
                                        (mid,))["enabled"]
                self.ws.cmd_set_material_enabled(mid, not cur)
            except CmdError as e:
                messagebox.showerror("停用失败", str(e))
                return
            reload_rows()
            self.status("材料状态已更新", "ok")
        ttk.Button(row, text="新增材料", command=add).pack(side="left", padx=4)
        ttk.Button(row, text="启用/停用选中", command=toggle).pack(side="left", padx=4)
        ttk.Button(dlg, text="关闭", command=dlg.destroy).pack(pady=(0, 12))

    def edit_params(self):
        if not self._require_ws():
            return
        p = self.ws.params()
        fields = ["layer_height_mm", "line_width_comp", "z_comp_mm", "speed_mm_s",
                  "flow_factor", "pregel_temp_c", "set_temp_c", "cure_temp_c",
                  "cure_min", "ramp_c_min", "final_cure_min", "cool_to_c"]
        dlg = tk.Toplevel(self)
        dlg.title("工艺参数（SLC-003）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        vars_ = {}
        for i, k in enumerate(fields):
            ttk.Label(dlg, text=k, style="Panel.TLabel").grid(row=i, column=0,
                                                              padx=10, pady=3, sticky="w")
            v = tk.StringVar(value=str(p[k]))
            ttk.Entry(dlg, textvariable=v, width=18).grid(row=i, column=1, padx=10, pady=3)
            vars_[k] = v
        two = tk.IntVar(value=p["silver_two_fills"])
        ttk.Checkbutton(dlg, text="银浆分两次填（8 道次）", variable=two).grid(
            row=len(fields), column=1, padx=10, pady=6, sticky="w")

        def ok():
            kv = {}
            for k, v in vars_.items():
                try:
                    kv[k] = float(v.get())
                except ValueError:
                    messagebox.showerror("参数错误", f"{k} 必须是数字")
                    return
            kv["silver_two_fills"] = int(two.get())
            self.ws.cmd_set_params(**kv)
            dlg.destroy()
            self.status("工艺参数已更新，请重新切片（旧切片会标记过期）", "ok")
        ttk.Button(dlg, text="保存", command=ok, style="Accent.TButton").grid(
            row=len(fields) + 1, column=1, padx=10, pady=12, sticky="e")

    def slice_now(self):
        """切片（UI-005：重任务转后台，界面不冻结；可取消）。"""
        if not self._require_ws():
            return

        def work():
            return do_slice(self.ws)

        def done(sl):
            self.slices = sl
            self.set_view("2d")
            # 切片完成后直接进入"道次 0 预览"：用户要看切片结果，而不是原设计
            self.play_index = 0
            self._refresh_all()
            est = sl["estimate"]
            thick = sl.get("total_thickness_mm")
            warn = next((w for w in sl["warnings"] if "总厚" in w), "")
            msg = (f"切片完成：{sl['passes']} 道次 / 3 层，总厚 {thick:.3f} mm，"
                   f"预计打印 {est['time_s']['print']:.0f}s + 固化 "
                   f"{est['time_s']['cure']:.0f}s，G-code {sl['gcode_hash'][:12]}")
            if warn:
                self.status(msg + "｜⚠ 总厚超出目标区间，见警告", "warn")
            else:
                self.status(msg + "｜画布已切到道次 0 预览", "ok")

        def fail(e):
            messagebox.showerror("切片失败", f"E-SLICE\n{e}")
            self.status(str(e), "error")

        self._run_async("切片", work, done, fail)

    def show_schedule(self):
        if not self.slices:
            messagebox.showinfo("排程", "请先切片。")
            return
        lines = []
        for s in self.slices["schedule"]:
            lines.append(f"t={s['t_start_s']:>8.0f}s  {s['phase']:<16}"
                         f"{s['temp_c']:>6.0f}℃ {s['duration_s']:>7.0f}s  {s['note']}")
        self._show_text("固化排程（SLC-005）", "\n".join(lines))

    def export_gcode(self):
        if not self._require_ws():
            return
        sl = latest_slice(self.ws)
        if not sl:
            messagebox.showinfo("导出 G-code", "没有有效切片（或已过期），请先切片。")
            return
        p = filedialog.asksaveasfilename(defaultextension=".gcode",
                                         filetypes=[("G-code", "*.gcode")])
        if not p:
            return
        text = gcode_of(self.ws, sl["id"])
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.status(f"G-code 已导出（内容哈希 {sl['gcode_hash'][:12]}，含材料批次号）", "ok")

