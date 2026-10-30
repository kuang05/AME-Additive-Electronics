"""可打性检查、解释与 AI 排序（MRC-001..011 / AGT-012） —— 从 app.py 拆出的域动作（Mixin）。

本文件只含方法体（逐字搬运），`App` 继承后对外接口不变。
拆分动机：原 app.py 3500 行 131 个方法，单个域的对话框代码
与其它域混在一处，定位和评审都要跨越大量无关代码。
"""
from __future__ import annotations

import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ...mrc.engine import explain_issue
from ..theme import PANEL


class MrcMixin:
    def run_mrc(self):
        if not self.ws:
            self.status("未打开工程", "warn")
            return
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        n = len(self.mrc["issues"])
        self.status(f"可打性检查完成：分数 {self.mrc['score']}，{n} 条问题"
                    f"（规则版本 {self.mrc['rule_versions']}）",
                    "ok" if self.mrc["score"] >= 80 else "warn")

    def on_issue_select(self, _evt=None):
        sel = self.issue_tree.selection()
        if not sel:
            return
        idx = self.issue_tree.index(sel[0])
        issue = self._issue_rows[idx][1]
        self.selected = {"type": "issue", "data": issue}
        self._show_prop(issue)

    def explain_selected(self):
        if not self.selected or self.selected.get("type") != "issue":
            messagebox.showinfo("解释", "请先在问题列表里选一条。")
            return
        issue = self.selected["data"]
        # AI 增强解释（AGT-012）：规则原因 + AI 判断的修复优先级。
        # AI 层不可用时自动降级为规则先验，输出仍完整（AGT-010）。
        try:
            from ...ai.service import explain_issue_ai
            st = self.ws.mrc_state() if self.ws else {}
            text = explain_issue_ai(issue, st, self._cloud_enabled())
        except Exception as e:
            text = explain_issue(issue, self.ws.mrc_state() if self.ws else {})
            text += f"\n\n（AI 解释不可用，已降级为规则解释：{type(e).__name__}）"
        self._show_text(f"解释：{issue['rule_code']}（MRC-009）", text)

    def suggest_selected(self):
        if not self.selected or self.selected.get("type") != "issue":
            messagebox.showinfo("建议", "请先在问题列表里选一条。")
            return
        issue = self.selected["data"]
        self._show_text(f"建议：{issue['rule_code']}（建议不自动执行）",
                        f"对象：{issue['target_type']} {issue['target_id']}\n\n"
                        f"原因：{issue['reason']}\n\n可执行动作：{issue['suggestion']}")

    def locate_selected(self):
        if not self.selected or self.selected.get("type") != "issue":
            return
        self.status("已在画布高亮对应对象（MRC-010 定位）", "ok")
        self.redraw()

    def export_issues(self):
        if not self.ws:
            return
        p = filedialog.asksaveasfilename(defaultextension=".json",
                                         filetypes=[("JSON", "*.json"),
                                                    ("CSV", "*.csv")])
        if not p:
            return
        if p.lower().endswith(".csv"):
            # 用户选了 CSV 就必须真的写 CSV（早期实现一律写 JSON，名不副实）
            import csv
            names = {r["code"]: r["name"] for r in self.ws.rules()}
            with open(p, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["规则", "规则名", "对象类型", "对象", "严重度",
                            "扣分", "原因", "建议"])
                for it in self.mrc["issues"]:
                    w.writerow([it["rule_code"], names.get(it["rule_code"], ""),
                                it["target_type"], it["target_id"],
                                it["severity"], it["penalty"],
                                it["reason"], it.get("suggestion", "")])
            self.status(f"问题列表已导出为 CSV：{p}（{len(self.mrc['issues'])} 条）", "ok")
            return
        data = {"score": self.mrc["score"], "issues": self.mrc["issues"],
                "rule_versions": self.mrc["rule_versions"]}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.status(f"问题列表已导出：{p}", "ok")

    def edit_rules(self):
        if not self._require_ws():
            return
        rules = self.ws.rules()
        dlg = tk.Toplevel(self)
        dlg.title("规则阈值（MRC-011：改阈值后重新评分，旧记录保留）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        vars_ = {}
        for i, r in enumerate(rules):
            ttk.Label(dlg, text=f"{r['code']} {r['name']} v{r['rule_version']}",
                      style="Panel.TLabel").grid(row=i, column=0, padx=10, pady=4,
                                                 sticky="w")
            v = tk.StringVar(value=json.dumps(r["threshold"], ensure_ascii=False))
            ttk.Entry(dlg, textvariable=v, width=40).grid(row=i, column=1, padx=10, pady=4)
            vars_[r["code"]] = v

        def ok():
            for code, v in vars_.items():
                try:
                    thr = json.loads(v.get())
                except json.JSONDecodeError:
                    messagebox.showerror("阈值格式错误", f"{code} 的阈值不是合法 JSON")
                    return
                self.ws.cmd_set_rule(code, thr)
            dlg.destroy()
            self.run_mrc()
            self.status("阈值已更新并重新评分（旧分记录保留在版本历史）", "ok")
        ttk.Button(dlg, text="保存并重新评分", command=ok,
                   style="Accent.TButton").grid(row=len(rules), column=1, padx=10,
                                                pady=12, sticky="e")

    def show_ai_status(self):
        """AI 能力状态与候选修复收益排序（AGT-012，比赛材料现场证据）。"""
        if not self._require_ws():
            return
        from ...ai.service import ai_status, rank_proposals

        cloud = self._cloud_enabled()
        status = ai_status(cloud)
        src_cn = {"cloud": "云端模型（华为云在线推理）",
                  "local": "本地模型（随包权重，离线可用）",
                  "heuristic": "规则先验（无模型降级）"}
        lines = [
            f"当前打分来源：{src_cn.get(status['source'], status['source'])}",
            f"特征版本 v{status['feature_version']}，维度 {status['feature_dim']}",
            f"云端开关（设置）：{'开启' if cloud else '关闭'}",
            "",
            "本地模型：",
        ]
        loc = status["local"]
        lines.append(f"  · 权重可用：{'是' if loc['available'] else '否'}")
        lines.append(f"  · 来源标记：{loc['source']}")
        for k, label in (("samples", "训练样本"), ("mae", "样本内 MAE"),
                         ("r2", "样本内 R²"), ("trained_at", "训练时间")):
            if loc.get(k) is not None:
                lines.append(f"  · {label}：{loc[k]}")
        if loc.get("load_error"):
            lines.append(f"  · 权重加载问题：{loc['load_error']}")
        c = status["cloud"]
        lines.append("云端适配器：")
        lines.append(f"  · 可用：{'是' if c.get('available') else '否'}"
                     f"{'' if c.get('available') else '（未注入 transport，离线运行）'}")
        if c.get("transport"):
            lines.append(f"  · 通道：{c['transport']}")

        # 现场演示：给当前问题排个序
        try:
            from ...ai.features import group_proposals
            from ...agent.loop import designer_propose
            st = self.ws.mrc_state()
            steps = designer_propose(self.ws, self.mrc, st)
            props = group_proposals(self.mrc.get("issues", []), steps)
            ranked = rank_proposals(props, st, self.mrc, cloud_enabled=cloud)
            if ranked["ordered"]:
                from ...agent.loop import _SRC_CN
                lines += ["", f"候选修复按期望收益排序"
                              f"（{_SRC_CN.get(ranked['source'], ranked['source'])}）："]
                for p in ranked["ordered"][:10]:
                    code = (p.get("issue") or {}).get("rule_code", "—")
                    reason = (p.get("reason") or "")[:44]
                    lines.append(f"  {p['_rank']}. [{code}] 评分 {p['_score']:.3f}"
                                 f" — {reason}")
                if ranked.get("error"):
                    lines.append(f"（云端失败已降级：{ranked['error']}）")
            else:
                lines.append("\n当前没有可执行的修复建议。")
        except Exception as e:
            lines.append(f"\n排序计算失败：{type(e).__name__}: {e}")
        self._show_text("AI 收益排序（AGT-012）", "\n".join(lines))

    def _cloud_enabled(self) -> bool:
        """云端增强是否开启（读设置；比赛版默认开启，产品版默认关）。"""
        try:
            return bool(self.data_root.settings.get("cloud_enabled", False))
        except Exception:
            return False

