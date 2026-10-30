"""智能体面板与对话（AGT-001..012） —— 从 app.py 拆出的域动作（Mixin）。

本文件只含方法体（逐字搬运），`App` 继承后对外接口不变。
拆分动机：原 app.py 3500 行 131 个方法，单个域的对话框代码
与其它域混在一处，定位和评审都要跨越大量无关代码。
"""
from __future__ import annotations

from tkinter import messagebox



class AgentMixin:
    def run_agent(self):
        if not self._require_ws():
            return
        from ...agent.loop import run_designer
        auth = self.auth_var.get()
        self.progress_set(20)
        try:
            res = run_designer(self.ws, auth=auth, max_rounds=8, auto_confirm=True,
                               cloud_enabled=self._cloud_enabled())
        except PermissionError as e:
            self.progress_set(0)
            messagebox.showinfo("需要逐步确认", f"{e}\n\n请在界面中逐步确认每一步。")
            return
        self.progress_set(100)
        self.mrc = self.ws.run_mrc()
        self._refresh_all()
        lines = [f"目标：提升可打性分数（起始 {res['score_start']}）",
                 f"授权：{auth}｜轮数 {res['rounds']}｜结果 {res['score_start']} → "
                 f"{res['score_final']}（{res['score_final'] - res['score_start']:+d}）",
                 f"裁决：{res['verdict']}　{res['arbiter_note']}",
                 f"候选排序：{self._ai_src_cn(res.get('ai_ranking'))}"
                 + (f"（{res['ai_error']}）" if res.get("ai_error") else ""),
                 f"耗时 {res['elapsed_s']}s", ""]
        action_cn = {"change_width": "调整线宽", "delete_route": "删除走线",
                     "add_route": "重新布线", "add_jumper": "加跳线",
                     "move_comp": "移动器件"}
        for c in res["cards"]:
            lines.append(f"〔对比卡片 第 {c['round']} 轮〕{c['before']} → {c['after']}")
            for act in c["actions"]:
                lines.append(f"    · {action_cn.get(act, act)}")
            for i in c["top_issues"]:
                lines.append(f"    ! {i[:90]}")
            lines.append("")
        lines.append("执行明细：")
        for a in res["audit"]:
            if "note" in a:
                lines.append(f"  第 {a.get('round', '?')} 轮：{a['note']}")
                continue
            act = action_cn.get(a.get("action"), a.get("action", ""))
            if a.get("error"):
                lines.append(f"  第 {a.get('round', '?')} 轮 · 第 {a.get('seq', '?')} 步"
                             f"｜{act}｜未执行：{a['error']}")
            else:
                lines.append(f"  第 {a.get('round', '?')} 轮 · 第 {a.get('seq', '?')} 步"
                             f"｜{act}｜{a.get('expected', '已完成')}")
        self.plan_text.configure(state="normal")
        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("1.0", "\n".join(lines))
        self.plan_text.configure(state="disabled")
        self.nb.select(self.tab_plan)
        self.progress_set(0)
        self.status(f"设计 Agent 完成：{res['score_start']} → {res['score_final']}"
                    f"（+{res['score_final'] - res['score_start']}），{res['rounds']} 轮",
                    "ok")
        self.show_replay(res)

    def show_replay(self, res):
        from ...agent.loop import clone_workspace, replay
        from ...ledger.services import do_slice as _slice
        snap = clone_workspace(self.ws, "ui-replay")
        rp = replay(snap, res["applied_steps"], self.ws, _slice,
                    source_commit=res["base_commit"])
        self.plan_text.configure(state="normal")
        self.plan_text.insert("end",
                              "\n重放校验（AGT-008）："
                              f"设计一致={rp['design_match']}，"
                              f"G-code 哈希一致={rp['gcode_match']}")
        self.plan_text.configure(state="disabled")

    def _ai_src_cn(self, src: str | None) -> str:
        """AI 排序来源的中文名（界面不暴露内部代号）。"""
        return {"cloud": "云端模型（华为云）", "local": "本地模型（离线）",
                "heuristic": "规则先验"}.get(src or "", "—")

    def send_chat(self):
        q = self.chat_entry.get().strip()
        if not q:
            return
        self.chat_entry.delete(0, "end")
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", f"\n我：{q}\n")
        self.chat_text.insert("end", f"助手：{self._answer(q)}\n")
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def _answer(self, q: str) -> str:
        if not self.ws:
            return "还没有打开工程，请先新建或打开工程。"
        if "分" in q or "为什么" in q:
            issues = self.mrc.get("issues", [])
            if not issues:
                return f"当前分数 {self.mrc.get('score')}，没有命中任何规则。"
            top = issues[:3]
            return (f"当前分数 {self.mrc.get('score')}，共 {len(issues)} 条问题。"
                    f"最严重的是：" + "；".join(f"{i['rule_code']} {i['reason']}"
                                              for i in top))
        if "R2" in q or "间距" in q:
            return "R2 检查异网最小距离，默认下限 0.5 mm；同网络不误报。"
        if "R1" in q or "线宽" in q:
            return "R1 检查线宽/槽宽下限，默认 0.5 mm（工艺基线 0.6 mm）。"
        if "切" in q:
            return "切片需要：有板框、连通性通过、材料启用、参数在范围内。"
        return "可以问：为什么分数低、R1/R2 是什么意思、能不能切片。"

