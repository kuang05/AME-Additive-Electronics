"""打印域动作、缺陷与作业历史（PRT-001..014） —— 从 app.py 拆出的域动作（Mixin）。

本文件只含方法体（逐字搬运），`App` 继承后对外接口不变。
拆分动机：原 app.py 3500 行 131 个方法，单个域的对话框代码
与其它域混在一处，定位和评审都要跨越大量无关代码。
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ...ledger.services import (defect_report, job_history, resumable_jobs,
                                resume_print, run_print, set_defect_status,
                                set_device_online)
from ..theme import ACCENT, GOOD, PANEL, TEXT


class PrintingMixin:
    def connect_device(self):
        if not self._require_ws():
            return
        set_device_online(self.ws, True)
        self.lbl_device.configure(text="设备：虚拟打印机 已连接", foreground=GOOD)
        self.status("虚拟打印机已连接（PRT-001）", "ok")

    def start_print(self):
        if not self._require_ws():
            return
        try:
            self.last_job = run_print(self.ws)
        except RuntimeError as e:
            messagebox.showerror("打印前检查未通过（PRT-002）", str(e))
            self.status(str(e), "error")
            return
        self.progress_set(100)
        self.status(f"虚拟打印完成：{self.last_job['state']}，"
                    f"耗时 {self.last_job['elapsed_s']}s，"
                    f"缺陷 {len(self.last_job['defects'])} 条（PRT-006）", "ok")
        if self.last_job["defects"]:
            self.show_defects()

    def inject_fault(self):
        if not self._require_ws():
            return
        from ...printing.virtual import FAULT_CN
        dlg = tk.Toplevel(self)
        dlg.title("故障注入（PRT-007：同参数重复运行结果一致）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text="故障类型", style="Panel.TLabel").grid(row=0, column=0,
                                                                  padx=10, pady=6)
        ft = tk.StringVar(value="open_circuit")
        ttk.Combobox(dlg, textvariable=ft, state="readonly", width=20,
                     values=list(FAULT_CN.keys())).grid(row=0, column=1, padx=10, pady=6)
        ttk.Label(dlg, text="道次", style="Panel.TLabel").grid(row=1, column=0,
                                                              padx=10, pady=6)
        pn = tk.StringVar(value="4")
        ttk.Entry(dlg, textvariable=pn, width=20).grid(row=1, column=1, padx=10, pady=6)
        ttk.Label(dlg, text="打印长度 (mm)", style="Panel.TLabel").grid(
            row=2, column=0, padx=10, pady=6)
        ln = tk.StringVar(value="50")
        ttk.Entry(dlg, textvariable=ln, width=20).grid(row=2, column=1, padx=10, pady=6)

        def ok():
            try:
                plan = [{"type": ft.get(), "pass_no": int(pn.get()),
                         "at_len_mm": float(ln.get())}]
            except ValueError:
                messagebox.showerror("参数错误", "道次与长度必须是数字")
                return
            dlg.destroy()
            try:
                self.last_job = run_print(self.ws, fault_plan=plan)
            except RuntimeError as e:
                messagebox.showerror("打印失败", str(e))
                return
            self.status(f"故障已注入：{self.last_job['state']}，"
                        f"{len(self.last_job['defects'])} 条缺陷", "warn")
            self.show_defects()
        ttk.Button(dlg, text="注入并执行", command=ok,
                   style="Accent.TButton").grid(row=3, column=1, padx=10, pady=12,
                                                sticky="e")

    def pause_estop(self):
        """暂停/急停（PRT-003/004/011）。

        有真实作业时对真实作业操作；没有时用一份演示副本说明语义，
        并在标题里明确标注"演示"，避免用户以为操作了真实作业。
        """
        from ...printing.virtual import JobState, VirtualPrinter
        pr = VirtualPrinter()
        job = self._live_job()
        demo = job is None
        if job is None:
            job = JobState(job_id="演示", slice_set_id="—", device_id="—",
                           gcode_hash="—", state="printing", safe_point=(3, 20.0))
        msg = (pr.pause(job, job.state or "printing") + "\n\n" + pr.resume(job) + "\n\n"
               + pr.pause(job, "curing") + "\n\n" + pr.estop(job))
        title = ("暂停与急停（PRT-003/004/011）— 演示，当前无进行中的作业"
                 if demo else f"暂停与急停 — 作业 {job.job_id[:8]}")
        self._show_text(title, msg)

    def _live_job(self):
        """当前可操作的作业：优先本会话刚打印的，其次库里未结束的（PRT-003）。"""
        if getattr(self, "last_job", None):
            row = self.ws.store.one("SELECT * FROM print_job WHERE id=?",
                                    (self.last_job["job_id"],)) if self.ws else None
            if row and row["state"] not in ("done", "cancelled"):
                return self._job_state(row)
            if row and self.last_job.get("state") in ("paused", "fault"):
                return self._job_state(row)
        if not self.ws:
            return None
        row = self.ws.store.one(
            "SELECT * FROM print_job WHERE state NOT IN ('done','cancelled') "
            "ORDER BY started_at DESC LIMIT 1")
        return self._job_state(row) if row else None

    def _job_state(self, row):
        from ...printing.virtual import JobState
        sp = None
        if row.get("safe_point_json"):
            try:
                d = json.loads(row["safe_point_json"])
                sp = (d["pass_no"], d.get("printed_mm", 0.0))
            except (json.JSONDecodeError, KeyError, TypeError):
                sp = None
        return JobState(job_id=row["id"], slice_set_id=row["slice_set_id"],
                        device_id=row["device_id"], gcode_hash=row["gcode_hash"],
                        state=row["state"], layer_cursor=row["layer_cursor"] or 0,
                        safe_point=sp)

    def resume_from_power_loss(self):
        """断电续打（PRT-014）：列出有安全点的作业，从该道次继续。"""
        if not self._require_ws():
            return
        jobs = resumable_jobs(self.ws)
        if not jobs:
            messagebox.showinfo(
                "断电续打（PRT-014）",
                "没有可续打的作业。\n\n"
                "续打条件：作业有过安全点记录且未正常结束"
                "（例如打印过程中被中断/断电后重开软件）。")
            return
        if self.hidden:
            job = jobs[0]
            try:
                r = resume_print(self.ws, job["id"])
                self.last_job = r
                self.status(f"已从道次 {r['resumed_from']['pass_no']} 续打：{r['state']}", "ok")
            except RuntimeError as e:
                self.status(str(e), "error")
            return
        dlg = tk.Toplevel(self)
        dlg.title("断电续打（PRT-014）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        tk.Label(dlg, text="选择要续打的作业（将从记录的最近安全点继续）：",
                 bg=PANEL, fg=TEXT).pack(anchor="w", padx=12, pady=(12, 6))
        lb = tk.Listbox(dlg, width=62, height=8, bg="#1b1f24", fg=TEXT,
                        selectbackground=ACCENT, relief="flat")
        for j in jobs:
            sp = j["safe_point"]
            lb.insert("end", f"{j['id'][:8]}  状态 {j['state']}  安全点 道次 "
                             f"{sp['pass_no']}（已打印 {sp.get('printed_mm', 0):.1f} mm）")
        lb.pack(padx=12, pady=(0, 8))
        lb.selection_set(0)

        def go():
            sel = lb.curselection()
            if not sel:
                return
            job = jobs[sel[0]]
            try:
                r = resume_print(self.ws, job["id"])
            except RuntimeError as e:
                messagebox.showerror("续打失败", str(e), parent=dlg)
                return
            dlg.destroy()
            self.last_job = r
            self.status(f"已从道次 {r['resumed_from']['pass_no']} 续打：{r['state']}，"
                        f"耗时 {r['elapsed_s']}s", "ok")

        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(bar, text="续打", command=go,
                   style="Accent.TButton").pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right", padx=6)

    def show_job_history(self):
        """作业历史（PRT-010）：可按时间区间与状态筛选。"""
        if not self._require_ws():
            return
        if self.hidden:
            rows = job_history(self.ws)
            self._show_text(f"作业历史（{len(rows)} 条，PRT-010）",
                            "\n".join(f"{r['started_at']} {r['state']}"
                                      for r in rows) or "无记录")
            return
        dlg = tk.Toplevel(self)
        dlg.title("作业历史（PRT-010：可按时间与状态筛选）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()

        frm = ttk.Frame(dlg, bg=PANEL)
        frm.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(frm, text="从（YYYY-MM-DD）", style="Muted.TLabel").pack(side="left")
        since = tk.StringVar()
        ttk.Entry(frm, textvariable=since, width=12).pack(side="left", padx=(2, 8))
        ttk.Label(frm, text="到", style="Muted.TLabel").pack(side="left")
        until = tk.StringVar()
        ttk.Entry(frm, textvariable=until, width=12).pack(side="left", padx=(2, 8))
        ttk.Label(frm, text="状态", style="Muted.TLabel").pack(side="left")
        state = tk.StringVar(value="全部")
        ttk.Combobox(frm, textvariable=state, width=10, state="readonly",
                     values=["全部", "done", "fault", "cancelled", "printing"]
                     ).pack(side="left", padx=2)

        cols = ("time", "state", "layers", "defects", "est", "ver")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=12)
        for c, w, t in (("time", 150, "时间"), ("state", 70, "状态"),
                        ("layers", 44, "层"), ("defects", 50, "缺陷"),
                        ("est", 80, "预计打印"), ("ver", 150, "版本/G-code")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, padx=12, pady=4)
        lbl = ttk.Label(dlg, text="", style="Muted.TLabel")
        lbl.pack(anchor="w", padx=12)

        def refresh():
            st = None if state.get() == "全部" else state.get()
            rows = job_history(self.ws, since=since.get().strip() or None,
                               until=until.get().strip() or None, state=st)
            tree.delete(*tree.get_children())
            for r in rows:
                tree.insert("", "end", values=(
                    r["started_at"] or "—", r["state"], r["layers"], r["defects"],
                    f"{r['estimated_print_s'] or 0:.0f}s",
                    f"{(r['source_commit'] or '')[:8]}/{r['gcode_hash']}"))
            lbl.configure(text=f"共 {len(rows)} 条")

        ttk.Button(frm, text="筛选", command=refresh).pack(side="left", padx=(8, 0))
        refresh()
        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(bar, text="关闭", command=dlg.destroy).pack(side="right")

    def show_defects(self):
        if not self.last_job:
            messagebox.showinfo("缺陷", "还没有打印作业。")
            return
        rows = defect_report(self.ws, self.last_job["job_id"]) if self.last_job["defects"] else []
        if not rows:
            self._show_text("缺陷记录", "本次作业无缺陷。")
            return
        if self.hidden:
            lines = [f"{d['type']}｜道次 {d['pass_no']}｜状态 {d['status']}"
                     for d in rows]
            self._show_text("缺陷记录与归因（PRT-008/009）", "\n".join(lines))
            return
        dlg = tk.Toplevel(self)
        dlg.title("缺陷记录与归因（PRT-008/009）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        cols = ("type", "pass", "sev", "status", "advice")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=10)
        for c, w, t in (("type", 110, "类型"), ("pass", 60, "道次"),
                        ("sev", 50, "级别"), ("status", 70, "状态"),
                        ("advice", 260, "归因建议")):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        for d in rows:
            tree.insert("", "end", iid=d["id"],
                        values=(d["type"], d["pass_no"], d["severity"],
                                d["status"], d.get("note", "")))
        tree.pack(fill="both", expand=True, padx=12, pady=12)

        def mark(status):
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("缺陷", "请先选中一条缺陷。", parent=dlg)
                return
            try:
                r = set_defect_status(self.ws, sel[0], status)
            except Exception as e:
                messagebox.showerror("标记失败", str(e), parent=dlg)
                return
            tree.set(sel[0], "status", r["status"])
            self.status(f"缺陷已标记为 {status}（PRT-008）", "ok")

        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        for text, st in (("标记已修复", "fixed"), ("忽略", "ignored"),
                         ("重设为待处理", "open")):
            ttk.Button(bar, text=text, command=lambda s=st: mark(s)).pack(
                side="left", padx=(0, 6))
        ttk.Button(bar, text="导出 CSV…", command=self.export_defects).pack(
            side="right")

    def export_defects(self):
        """导出缺陷记录（PRT-008："可导出"）。"""
        if not self.last_job:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not p:
            return
        import csv
        rows = defect_report(self.ws, self.last_job["job_id"])
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["类型", "道次", "层高(mm)", "严重度", "状态",
                        "位置", "归因", "建议"])
            for d in rows:
                w.writerow([d["type"], d["pass_no"], d["layer_z_mm"],
                            d["severity"], d["status"], d["bbox_json"],
                            d["attribution"]["class"], d["note"]])
        self.status(f"缺陷记录已导出：{p}（{len(rows)} 条）", "ok")

