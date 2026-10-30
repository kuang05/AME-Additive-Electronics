"""工程管理与版本账本（PRJ-001..009 / DAT） —— 从 app.py 拆出的域动作（Mixin）。

本文件只含方法体（逐字搬运），`App` 继承后对外接口不变。
拆分动机：原 app.py 3500 行 131 个方法，单个域的对话框代码
与其它域混在一处，定位和评审都要跨越大量无关代码。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog as sd, ttk

from ...ledger.workspace import CmdError, Workspace, create_project
from ..theme import ACCENT, PANEL, TEXT


class ProjectMixin:
    def show_settings(self):
        """软件设置（PRJ-009）：日志级别 / 自动保存间隔 / 云端开关 / 数据目录。

        早期只有存储层，没有任何界面，也没有代码读取这些值——设置形同虚设。
        现在至少让日志级别实时生效，并把其余项显示清楚（标注哪些是首版未接线的）。
        """
        s = self.data_root.settings
        if self.hidden:
            self._show_text("软件设置（PRJ-009）",
                            "\n".join(f"{k} = {v}" for k, v in s.items()))
            return
        dlg = tk.Toplevel(self)
        dlg.title("软件设置（PRJ-009）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()

        vars_: dict[str, object] = {}
        rows = [
            ("log_level", "日志级别", ["debug", "info", "warning", "error"]),
            ("unit", "单位", ["mm"]),
            ("cloud_enabled", "云端增强", ["关闭", "开启"]),
        ]
        for i, (key, label, choices) in enumerate(rows):
            ttk.Label(dlg, text=label, style="Panel.TLabel").grid(
                row=i, column=0, padx=12, pady=6, sticky="w")
            cur = s.get(key)
            if key == "cloud_enabled":
                cur = "开启" if cur else "关闭"
            v = tk.StringVar(value=str(cur))
            vars_[key] = v
            ttk.Combobox(dlg, textvariable=v, values=choices, state="readonly",
                         width=16).grid(row=i, column=1, padx=12, pady=6)

        ttk.Label(dlg, text="自动保存间隔（秒）", style="Panel.TLabel").grid(
            row=len(rows), column=0, padx=12, pady=6, sticky="w")
        save_v = tk.StringVar(value=str(s.get("autosave_interval_s", 60)))
        vars_["autosave_interval_s"] = save_v
        ttk.Entry(dlg, textvariable=save_v, width=18).grid(
            row=len(rows), column=1, padx=12, pady=6)

        ttk.Label(dlg, text=f"数据目录：{self.data_root.data_dir}",
                  style="Muted.TLabel", wraplength=420).grid(
            row=len(rows) + 1, column=0, columnspan=2, padx=12, pady=(0, 6),
            sticky="w")
        ttk.Label(dlg, text="说明：所有命令即时落库（自动保存已内建），间隔项保留备用；"
                            "AI 收益排序默认用本地模型（离线可用）。开启云端增强后，"
                            "若已配置华为云通道则优先用云端推理，失败自动降级到本地。",
                  style="Muted.TLabel", wraplength=420).grid(
            row=len(rows) + 2, column=0, columnspan=2, padx=12, pady=(0, 8),
            sticky="w")

        def save():
            try:
                interval = int(float(vars_["autosave_interval_s"].get()))
                if interval <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("设置", "自动保存间隔必须是正整数秒", parent=dlg)
                return
            self.data_root.save_settings(
                log_level=vars_["log_level"].get(), unit="mm",
                cloud_enabled=(vars_["cloud_enabled"].get() == "开启"),
                autosave_interval_s=interval)
            # 日志级别立即生效（其他项在下次启动时读取）
            try:
                from ...logging_setup import configure as _configure_log
                self.log_file = _configure_log(
                    self.data_root.data_dir,
                    level=self.data_root.settings.get("log_level", "info"))
            except Exception:
                pass
            dlg.destroy()
            self.status("设置已保存（日志级别已立即生效）", "ok")

        bar = tk.Frame(dlg, bg=PANEL)
        bar.grid(row=len(rows) + 3, column=0, columnspan=2, sticky="e",
                 padx=12, pady=(0, 12))
        ttk.Button(bar, text="保存", command=save, style="Accent.TButton").pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right", padx=6)

    def open_sample(self):
        """一键打开示例工程（NFR-003：新用户无需自备文件即可看到完整链路）。"""
        from ...ledger.workspace import build_sample_project

        try:
            self.ws = build_sample_project(self.data_root)
        except CmdError as e:
            messagebox.showerror("打开示例失败", str(e))
            self.status(str(e), "error")
            return
        self.mrc = self.ws.run_mrc()
        self.slices = None
        self.play_index = None
        self._refresh_all()
        self.fit_view()
        self.status(f"示例工程已打开：{len(self.ws.comps())} 器件 / "
                    f"{len(self.ws.routes())} 走线 / 初始 {self.mrc['score']} 分。"
                    "可继续运行检查、切片与虚拟打印", "ok")

    def create_named_project(self, name: str, bed=(200.0, 200.0)) -> bool:
        """创建工程的**动作本身**（与"向用户要名字/幅面"分离）。

        同 `apply_tess_tol` 的理由：把参数获取与业务执行拆开后，
        自动化与测试可以直接调用创建逻辑，不必伪造对话框
        （AGT-009「人与 Agent 同源」：界面上能做的，脚本也能做）。
        """
        try:
            self.ws = create_project(self.data_root,
                                     (name or "").strip() or "新工程", bed=bed)
        except Exception as e:
            messagebox.showerror("创建失败", str(e))
            return False
        self.mrc = {"issues": [], "score": None}
        self.slices = None
        self.play_index = None
        self._refresh_all()
        self.status("工程已创建，版本 0。请导入 STEP（STR-001）", "ok")
        return True

    def new_project(self):
        if self.hidden:
            # 自动化环境无交互输入：按默认值直接创建（不构造模态对话框）
            self.create_named_project("新工程")
            return
        dlg = tk.Toplevel(self)
        dlg.title("新建工程")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        ttk.Label(dlg, text="工程名称", style="Panel.TLabel").grid(
            row=0, column=0, padx=12, pady=8, sticky="w")
        name = tk.StringVar(value="新工程")
        ttk.Entry(dlg, textvariable=name, width=32).grid(row=0, column=1, padx=12, pady=8)
        ttk.Label(dlg, text="幅面 (mm)", style="Panel.TLabel").grid(
            row=1, column=0, padx=12, pady=8, sticky="w")
        bed = tk.StringVar(value="200x200")
        ttk.Entry(dlg, textvariable=bed, width=32).grid(row=1, column=1, padx=12, pady=8)

        def ok():
            try:
                bx, by = [float(v) for v in bed.get().lower().split("x")]
            except ValueError:
                messagebox.showerror("参数错误", "幅面格式应为 200x200")
                return
            if self.create_named_project(name.get(), (bx, by)):
                dlg.destroy()
        ttk.Button(dlg, text="创建", command=ok, style="Accent.TButton").grid(
            row=2, column=1, padx=12, pady=12, sticky="e")

    def open_project_dialog(self):
        projects = self.data_root.list_projects()
        if not projects:
            messagebox.showinfo("最近工程", "还没有工程。请先新建工程。")
            return
        dlg = tk.Toplevel(self)
        dlg.title("打开工程")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        lb = tk.Listbox(dlg, width=56, height=12, bg="#1b1f24", fg=TEXT,
                        selectbackground=ACCENT, relief="flat")
        for p in projects:
            lb.insert("end", f"{p['name']}    {p['created_at']}    {p['id'][:8]}")
        lb.pack(padx=12, pady=12)

        def ok():
            sel = lb.curselection()
            if not sel:
                return
            p = projects[sel[0]]
            try:
                self.ws = Workspace(self.data_root.open_project(p["id"]), p["id"])
            except Exception as e:
                messagebox.showerror("打开失败", str(e))
                return
            dlg.destroy()
            self.mrc = self.ws.run_mrc()
            self.slices = None
            self._refresh_all()
            # DAT-005：打开时校验内容寻址文件是否被外部改动，有问题立刻告知
            try:
                bad = [x for x in self.ws.integrity_check() if not x["ok"]]
            except Exception:
                bad = []
            if bad:
                detail = "\n".join(f"· {x['kind']}：{x['detail']}" for x in bad)
                messagebox.showwarning(
                    "工程数据完整性（DAT-005）",
                    "以下文件与工程记录不一致，可能已被外部修改：\n\n"
                    f"{detail}\n\n建议重新导入对应文件，或在「文件 → 校验工程完整性」查看详情。")
                self.status(f"完整性告警：{len(bad)} 个文件与记录不符", "error")
            else:
                self.status(f"已打开 {p['name']}（恢复到最新版本，PRJ-002）", "ok")
        ttk.Button(dlg, text="打开", command=ok, style="Accent.TButton").pack(
            padx=12, pady=(0, 12), anchor="e")

    def commit_version(self):
        if not self._require_ws():
            return
        self.ws.commit("界面提交")
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        self.status("已提交新版本（撤销不产生版本，提交才产生版本）", "ok")

    def export_package(self):
        if not self._require_ws():
            return
        p = filedialog.asksaveasfilename(defaultextension=".amepkg",
                                         filetypes=[("AME 工程包", "*.amepkg")])
        if not p:
            return
        self.data_root.export_project(self.ws.store, p)
        self.status(f"工程包已导出：{p}", "ok")

    def import_package(self):
        p = filedialog.askopenfilename(filetypes=[("AME 工程包", "*.amepkg")])
        if not p:
            return
        as_copy = False
        try:
            st = self.data_root.import_project(p)
        except FileExistsError:
            # 同机已有同 id 工程：询问是否导入为副本（NFR-008/PRJ-006）
            if not messagebox.askyesno(
                    "工程已存在",
                    "本机已有同编号工程。\n\n"
                    "是否导入为副本（会分配新编号，两份互不影响）？"):
                return
            as_copy = True
            try:
                st = self.data_root.import_project(p, as_copy=True)
            except Exception as e:
                messagebox.showerror("导入失败", str(e))
                return
        except Exception as e:
            messagebox.showerror("导入失败", str(e))
            return
        try:
            pid = st.one("SELECT id FROM project")["id"]
            self.ws = Workspace(st, pid)
        except Exception as e:
            messagebox.showerror("打开失败", str(e))
            return
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        self.status("工程包已导入为副本" if as_copy else "工程包已导入", "ok")

    def _simple_ask(self, title, label, default="") -> str | None:
        """隐藏模式下直接返回默认值；正常模式弹输入框。"""
        if self.hidden:
            return default
        import tkinter.simpledialog as sd
        return sd.askstring(title, label, initialvalue=default, parent=self)

    def apply_rename_project(self, new_name: str) -> bool:
        """重命名的**动作本身**（与"问用户要什么名字"分离，同 apply_tess_tol）。"""
        if not self.ws:
            return False
        old = self.ws.project["name"]
        new = (new_name or "").strip()
        if not new or new == old:
            return False
        try:
            self.data_root.rename_project(self.ws.project_id, new)
        except (ValueError, FileNotFoundError) as e:
            messagebox.showerror("重命名失败", str(e))
            return False
        self._refresh_all()
        self.status(f"工程已重命名：{old} → {new}", "ok")
        return True

    def rename_project(self):
        if not self._require_ws():
            return
        old = self.ws.project["name"]
        new = self._simple_ask("重命名工程", "新名称：", old)
        self.apply_rename_project(new or old)

    def copy_project(self):
        if not self._require_ws():
            return
        old = self.ws.project["name"]
        new = self._simple_ask("复制工程", "副本名称：", f"{old} 副本")
        if new is None:
            return
        try:
            info = self.data_root.copy_project(self.ws.project_id, new)
        except Exception as e:
            messagebox.showerror("复制失败", str(e))
            return
        self.status(f"已复制为「{info['name']}」（{info['versions']} 个版本，"
                    f"与原工程互不影响）", "ok")

    def delete_project(self):
        if not self._require_ws():
            return
        name = self.ws.project["name"]
        if not self.hidden and not messagebox.askyesno(
                "删除工程", f"确定删除「{name}」吗？\n\n"
                "工程将移入回收站，可从 工程 → 回收站 恢复。"):
            return
        pid = self.ws.project_id
        try:
            self.ws.close()
            self.ws.store.close()
            self.data_root.delete_project(pid)
        except Exception as e:
            messagebox.showerror("删除失败", str(e))
            return
        self.ws = None
        self.mrc = {"issues": [], "score": None}
        self._refresh_all()
        self.status(f"工程「{name}」已移入回收站（PRJ-007，可恢复）", "warn")

    def show_trash(self):
        rows = self.data_root.list_trash()
        if not rows:
            messagebox.showinfo("回收站", "回收站是空的。")
            return
        if self.hidden:
            # 测试/无界面模式不创建窗口，改为记录文本（与 _show_text 一致）
            self._show_text(f"回收站（{len(rows)} 项，PRJ-007）",
                            "\n".join(f"{r['name']}  {r['file']}" for r in rows))
            return
        dlg = tk.Toplevel(self)
        dlg.title("回收站（PRJ-007：可恢复到工程列表）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        tk.Label(dlg, text=f"共 {len(rows)} 项，选中后点「恢复」移回工程列表",
                 bg=PANEL, fg=TEXT).pack(anchor="w", padx=12, pady=(12, 6))
        lb = tk.Listbox(dlg, width=64, height=12, bg="#1b1f24", fg=TEXT,
                        selectbackground=ACCENT, relief="flat")
        for r in rows:
            lb.insert("end", f"{r['name']}    {r['created_at']}    {r.get('file', '')}")
        lb.pack(padx=12, pady=(0, 8))

        def restore():
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("回收站", "请先选中一项。", parent=dlg)
                return
            r = rows[sel[0]]
            try:
                pid = self.data_root.restore_project(r["file"])
            except Exception as e:
                messagebox.showerror("恢复失败", str(e), parent=dlg)
                return
            dlg.destroy()
            self.status(f"已从回收站恢复工程 {r['name']}（id {str(pid)[:8]}）", "ok")

        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(bar, text="恢复", command=restore,
                   style="Accent.TButton").pack(side="right")
        ttk.Button(bar, text="关闭", command=dlg.destroy).pack(side="right", padx=6)

    def check_integrity(self):
        """校验 STEP / 板文件 / G-code 是否被外部修改（DAT-005）。"""
        if not self._require_ws():
            return
        try:
            rows = self.ws.integrity_check()
        except Exception as e:
            messagebox.showerror("校验失败", str(e))
            return
        if not rows:
            self._show_text("工程完整性（DAT-005）", "该工程还没有可校验的文件。")
            return
        lines = [("✓" if r["ok"] else "✗") + f"  {r['kind']}：{r['detail']}"
                 for r in rows]
        bad = [r for r in rows if not r["ok"]]
        head = ("全部一致。" if not bad
                else f"发现 {len(bad)} 处不一致，相关文件可能被外部修改或删除。")
        self._show_text(f"工程完整性（DAT-005）— {head}", "\n".join(lines))
        self.status(head, "ok" if not bad else "error")

    def apply_tess_tol(self, val: float) -> bool:
        """设置网格容差的**动作本身**（与"问用户要多少"分离）。

        拆开的理由（AGT-009「人与 Agent 同源」）：原先取值与执行写在一个
        函数里，隐藏模式下直接 return，导致自动化无法执行这个操作，
        "容差真的生效"这件事也就无法被测试覆盖。
        """
        if not self.ws:
            return False
        try:
            self.ws.cmd_set_tess_tol(val)
        except Exception as e:
            messagebox.showerror("设置失败", str(e))
            return False
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        self.status(f"网格容差已改为 {val} mm"
                    "（重新离散圆弧，显示与切片共用同一份网格）", "ok")
        return True

    def set_tess_tol(self):
        """网格容差（STR-008）：默认 0.02 mm，调大降面数，调小提精度。"""
        if not self._require_ws():
            return
        s = self.ws.structure()
        if not s:
            messagebox.showinfo("网格容差", "还没有结构，导入 STEP 后再设置。")
            return
        cur = s["tess_tol"]
        if self.hidden:
            # 自动化环境无交互输入：用当前值执行一次（幂等），
            # 让这条动作路径本身可被覆盖，而不是被 return 掉。
            self.apply_tess_tol(cur)
            return
        val = sd.askfloat(
            "网格容差（STR-008）",
            "弦高容差（mm）：默认 0.02，调大（如 0.05）可减少面数、加快大模型显示；\n"
            "调小（如 0.005）可提高圆弧精度。允许范围 0.001–1.0。\n\n"
            f"当前值：{cur}",
            initialvalue=cur, minvalue=0.001, maxvalue=1.0, parent=self)
        if val is None:
            return
        self.apply_tess_tol(val)

