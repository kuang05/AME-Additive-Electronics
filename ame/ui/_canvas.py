"""画布与视图（Mixin）。

从 `app.py` 拆出。`App` 通过多重继承获得这些方法，对外接口与拆分前
完全一致——分离动机只是让单个文件可读：原 `app.py` 3500 行、131 个方法，
定位任何一个面板都要在千行范围内搜索。

本文件只包含方法体（逐字搬运），新增状态字段仍写在 `App.__init__`，
避免 mixin 之间出现隐式依赖。
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk

from shapely.geometry import LineString

from ..ledger.services import heatmap

from .theme import ACCENT, GOOD, MUTED, PANEL, TEXT, WARN


class CanvasMixin:
    def redraw(self):
        c = self.canvas
        self._cam_cache = None        # 本次重绘重新计算相机（窗口/缩放可能变了）
        c.delete("all")
        w = c.winfo_width() or 800
        h = c.winfo_height() or 600
        c.create_text(12, 12, anchor="nw", fill=MUTED,
                      text=f"{'三维' if self.view_mode == '3d' else '二维'}｜"
                           f"缩放 {self.zoom:.2f}｜滚轮缩放"
                           + (f"｜剖切 Z≤{self.section_z:.2f}" if self.section_z is not None
                              else "")
                           + (f"｜道次 {self.play_index}" if self.play_index is not None
                              else ""))
        ws = self.ws
        if not ws:
            c.create_text(w / 2, h / 2, fill=MUTED, justify="center",
                          text="还没有工程\n\n"
                               "工程 → 打开示例工程（推荐，一键看到完整链路）\n"
                               "工程 → 新建工程（用自己的 STEP 与板文件）\n\n"
                               "核心功能离线可用")
            return
        outline, note = ws.board_outline()
        if not outline:
            c.create_text(w / 2, h / 2, fill=WARN, justify="center",
                          text=f"没有板框\n\n{note}\n\n结构域 → 导入 STEP 或手工板框\n"
                               "（无板框不能切片，STR-007）")
            return
        # 板框
        pts = [self._to_screen(p[0], p[1]) for p in outline]
        c.create_polygon(*[v for p in pts for v in p], outline=ACCENT, fill="",
                         width=2)
        # 结构几何（STR-003：三维视图显示模型本身；二维布线视图只看板框+器件+走线）
        if self.view_mode == "3d":
            self._draw_structure(c)
        self._draw_grid(c)

        st = ws.mrc_state()
        self._draw_passes_and_routes(c, st)

        self._draw_comps_and_pads(c, st)

        if self.tool == "route" and self._drag_pts:
            coords = []
            for p in self._drag_pts:
                coords.extend(self._to_screen(p[0], p[1]))
            # create_line 至少需要 4 个坐标（两个点）；只有 1 个点时画点标记。
            # 早期无条件 create_line，用户点第一下就抛
            # TclError: wrong # coordinates: expected at least 4, got 2。
            if len(coords) >= 4:
                c.create_line(*coords, fill=GOOD, width=2, dash=(4, 2))
            elif len(coords) == 2:
                c.create_oval(coords[0] - 4, coords[1] - 4,
                              coords[0] + 4, coords[1] + 4,
                              outline=GOOD, width=2)
            # 起点标记，提示"双击结束"
            s = self._to_screen(self._drag_pts[0][0], self._drag_pts[0][1])
            c.create_oval(s[0] - 5, s[1] - 5, s[0] + 5, s[1] + 5,
                          outline=GOOD, width=2)
        self._draw_heatmap(c)


    def _draw_grid(self, c):
        """画 10 mm 参考网格。"""
        x0, y0, x1, y1 = self._world_bbox()
        step = 10
        gx = x0
        while gx <= x1:
            p1 = self._to_screen(gx, y0)
            p2 = self._to_screen(gx, y1)
            c.create_line(*p1, *p2, fill="#232830")
            gx += step
        gy = y0
        while gy <= y1:
            p1 = self._to_screen(x0, gy)
            p2 = self._to_screen(x1, gy)
            c.create_line(*p1, *p2, fill="#232830")
            gy += step

    def _draw_passes_and_routes(self, c, st: dict):
        """画切片道次路径（逐层播放时）或设计走线。"""
        nsel = self.selected["data"].get("target_id", "") if (
            self.selected and self.selected.get("type") == "issue") else None
        # 逐层播放时只显示当前道次的路径
        play_pts = None
        if self.slices and self.play_index is not None and self.layers_visible:
            pds = self.slices.get("pass_details") or []
            pa = pds[min(self.play_index, len(pds) - 1)] if pds else None
            play_pts = pa["paths"] if pa else []
            if pa:
                c.create_text(12, 30, anchor="nw", fill=GOOD,
                              text=f"播放道次 {pa['pass_no']}（{pa['kind']}/{pa['mat']}）"
                                   f"路径 {len(pa['paths'])} 条")
        elif self.slices and self.play_index is not None:
            # 层显隐关闭时明确说明，否则用户会以为切片结果为空（UI-002）
            c.create_text(12, 30, anchor="nw", fill=MUTED,
                          text="切片道次已隐藏（再按一次「层显隐」恢复显示）")
        if play_pts is not None:
            for path in play_pts:
                if len(path["pts"]) < 2:
                    continue
                coords = []
                for p in path["pts"]:
                    coords.extend(self._to_screen(p[0], p[1], 0.34))
                c.create_line(*coords, fill="#8fe3b0", width=2)
                # 起停点（SLC-006）
                s0 = self._to_screen(path["pts"][0][0], path["pts"][0][1], 0.34)
                s1 = self._to_screen(path["pts"][-1][0], path["pts"][-1][1], 0.34)
                c.create_oval(s0[0] - 3, s0[1] - 3, s0[0] + 3, s0[1] + 3,
                              outline=GOOD, fill="")
                c.create_rectangle(s1[0] - 3, s1[1] - 3, s1[0] + 3, s1[1] + 3,
                                   outline=WARN, fill="")
            return          # 播放态不画设计走线（两套几何会互相混淆）
        for r in st["routes"]:
            coords = []
            for p in r["pts"]:
                coords.extend(self._to_screen(p[0], p[1], 0.34))
            hl = nsel and r["id"][:8] in str(nsel)
            c.create_line(*coords, fill=(WARN if hl else "#d0a85c"),
                          width=max(1, int(r["width"] * 4 * self.zoom)),
                          capstyle="round")

    def _draw_comps_and_pads(self, c, st: dict):
        """画器件、焊盘与飞线（CIR-003）。"""
        # CIR-003 飞线：还没布线的网络用虚线连出连接关系（可按网络高亮）
        if self.show_ratlines:
            from ..circuit.connectivity import ratlines
            only = self.highlight_net or None
            for rl in ratlines(st["pads"], st["routes"], net=only):
                x0, y0 = self._to_screen(rl["a"][0], rl["a"][1])
                x1, y1 = self._to_screen(rl["b"][0], rl["b"][1])
                c.create_line(x0, y0, x1, y1, fill=(ACCENT if only else "#4a5a6a"),
                              width=1, dash=(3, 3))
                if only:
                    c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=rl["net"],
                                  fill=ACCENT, font=("Consolas", 7))
        for comp in st["comps"]:
            # STR-003 剖切：剖切高度以下的器件才显示（器件底面在 z=0 之上，按高度判定）
            if self.section_z is not None and comp.get("height", 0) > self.section_z:
                continue
            cx, cy = self._to_screen(comp["x"], comp["y"])
            sz = comp.get("size_dx", 3.0) * 0.6 * self.zoom * 4
            c.create_rectangle(cx - sz / 2, cy - sz / 2, cx + sz / 2, cy + sz / 2,
                               outline="#7f8b99", fill="#2c333c")
            c.create_text(cx, cy, text=comp["ref"], fill=TEXT,
                          font=("Consolas", 8))
        for p in st["pads"]:
            px, py = self._to_screen(p["x"], p["y"])
            hl_pad = bool(self.highlight_net) and p.get("net") == self.highlight_net
            rr = 3.4 if hl_pad else 2.2
            c.create_oval(px - rr, py - rr, px + rr, py + rr,
                          fill=(ACCENT if hl_pad else "#c8b273"), outline="")
            # CIR-003：焊盘编号（放大到一定层级才显示，避免密集时糊成一片）
            if self.zoom >= 1.4:
                c.create_text(px + 4, py - 4, anchor="sw",
                              text=f"{p['ref']}.{p['pad_no']}",
                              fill=(ACCENT if hl_pad else MUTED),
                              font=("Consolas", 6))

    def _draw_heatmap(self, c):
        """MRC-010 热力图：按对象累计扣分渲染半透明圆（红=重，黄=轻）。"""
        if not self.heatmap_on:
            return
        hm = self._heat_data()
        if not (hm and hm["points"]):
            return
        mx = hm["max_weight"] or 1.0
        for pt in hm["points"]:
            sx, sy = self._to_screen(pt["x"], pt["y"])
            r = 8 + 26 * (pt["weight"] / mx)
            ratio = pt["weight"] / mx
            color = "#ef5f5f" if ratio > 0.66 else (
                "#f0a84b" if ratio > 0.33 else "#e8d05c")
            c.create_oval(sx - r, sy - r, sx + r, sy + r,
                          outline=color, width=2, dash=(2, 2))
            c.create_text(sx, sy - r - 7, text=f"{pt['weight']:.1f}",
                          fill=color, font=("Consolas", 7))

    def _draw_structure(self, c):
        """绘制结构几何（STR-003）：光照 + 深度排序 + 背面剔除。

        相机为可旋转正交投影；着色 = Lambert 漫反射（立体感）
        + 坡度叠加（斜面橙色，传达可打性风险）。剖切高度以上不画。
        面数超出 DETAIL_LIMIT 时降为包围盒线框，保证交互帧率。
        """
        if not self.ws:
            return
        try:
            m = self.ws.step_model()
        except Exception:
            return
        if not m or not m.faces:
            return
        from .render import (LOD_BBOX_MAX, lod_level, render_faces)

        faces = m.faces
        cam, scale, origin = self._camera()
        level = lod_level(len(faces))

        # 只有超过 50 万面才退化为包围盒线框（STR-003）。
        # 早期版本超过 800 面就直接画线框，导致上万面的模型看不到着色几何——
        # 与需求"20 万面仍要显示"相差两个数量级。
        if level == "bbox" or len(faces) > LOD_BBOX_MAX:
            x0, y0, z0, x1, y1, z1 = m.bbox
            corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1)
                       for z in (z0, z1)]
            sp = [cam.project(p, scale, origin) for p in corners]
            for i, j in ((0, 1), (1, 3), (3, 2), (2, 0), (4, 5), (5, 7),
                         (7, 6), (6, 4), (0, 4), (1, 5), (2, 6), (3, 7)):
                c.create_line(*sp[i], *sp[j], fill="#3a434e")
            c.create_text(12, 48, anchor="nw", fill=MUTED,
                          text=f"结构 {len(faces)} 面（超过 50 万面，已降为包围盒线框）")
            self._lod_kept = []          # 线框模式没有 LOD 面，别留上一帧的
            return

        items = render_faces(faces, cam, scale, origin, section_z=self.section_z,
                             lod=level)
        # 记住 LOD 实际绘制了哪些面（顶面高亮只在这批里找，避免全量扫描）。
        # items 里带 _fid，一次构建集合是 O(n)，不要写成双层推导式。
        drawn = {it.get("_fid") for it in items}
        self._lod_kept = ([f for f in faces if f.fid in drawn]
                          if drawn else list(faces))
        if items:
            it0 = items[0]
            if it0.get("_lod") in ("lod", "coarse") and it0["_total"] > it0["_kept"]:
                c.create_text(12, 32, anchor="nw", fill=MUTED,
                              text=f"结构 {it0['_total']} 面（LOD 显示 {it0['_kept']} 面，"
                                   f"已按面积保留关键面）")
        self._paint_items(c, items)

        # 顶面高亮：最上面的大面 = 打印起始面。
        # 只在**已经过 LOD 的面**里找：大面必然被 LOD 保留（面积降序是首要判据），
        # 全量扫描 50 万面每帧要多花 40–90 ms（实测 50 万面档从 24 fps 掉到 10 fps）。
        scan = self._lod_kept or faces
        tops = [f for f in scan if f.normal[2] > 0.98 and f.area > 5]
        if tops and cam.facing((0, 0, 1)):
            t = max(tops, key=lambda f: f.area)
            if self.section_z is None or min(p[2] for p in t.loop) <= self.section_z + 1e-6:
                coords = []
                for pnt in t.loop:
                    coords.extend(cam.project(pnt, scale, origin))
                c.create_polygon(*coords, outline="#8fa3b8", fill="", width=2)

        # STR-005：布尔建议预览（线框，不写入工程）
        for pv in self.feature_preview:
            self._draw_feature_preview(c, cam, scale, origin, pv)

    def on_canvas_click(self, ev):
        # 三维视图：左键拖拽旋转相机（STR-003）
        if self.view_mode == "3d" and self.tool is None:
            self._orbit = (ev.x, ev.y, self.cam_az, self.cam_el)
            return
        if self.tool == "measure" and self.view_mode == "2d":
            self.measure_pts.append(self._from_screen(ev.x, ev.y))
            if len(self.measure_pts) >= 2:
                (x0, y0), (x1, y1) = self.measure_pts[-2], self.measure_pts[-1]
                d = math.hypot(x1 - x0, y1 - y0)
                self.measure_pts = []
                self.tool = None      # 先清理工具，再写状态，避免被“已取消”覆盖
                self.last_measure = {"from": [round(x0, 2), round(y0, 2)],
                                     "to": [round(x1, 2), round(y1, 2)],
                                     "distance_mm": round(d, 2)}
                self.status(f"测量：({x0:.2f},{y0:.2f}) → ({x1:.2f},{y1:.2f}) "
                            f"= {d:.2f} mm（STR-003）", "ok")
            else:
                self.status("测量：已选第一点，请点第二点")
            self.redraw()
            return
        if self.tool == "route" and self.view_mode == "2d":
            pt = self._from_screen(ev.x, ev.y)
            # 布线需要先知道画哪个网络：没选网络时，点焊盘就把该焊盘的网络
            # 定为本次布线网络（这是最短路径，不用先开对话框选网络）
            if not self.route_net and self.ws:
                hit = self._pad_near(pt[0], pt[1])
                if hit and hit.get("net"):
                    self.route_net = hit["net"]
                    self.status(f"布线网络：{hit['net']}（点焊盘选取），"
                                "继续点落点，双击结束")
                else:
                    self.status("请先点一个焊盘以选择网络（CIR-004）", "warn")
                    return
            self._drag_pts.append(pt)
            self.redraw()
            self.status(f"布线：已落 {len(self._drag_pts)} 点，双击结束"
                        f"（网络 {self.route_net}）")
            return
        # 选择：找最近的器件/焊盘/走线（UI-003）
        if not self.ws:
            return
        wx, wy = self._from_screen(ev.x, ev.y)
        best, bd = None, 1e9
        for comp in self.ws.comp_state():
            d = math.hypot(comp["x"] - wx, comp["y"] - wy)
            if d < bd:
                best, bd = ("comp", comp), d
        for p in self.ws.pad_state():
            d = math.hypot(p["x"] - wx, p["y"] - wy)
            if d < bd:
                best, bd = ("pad", p), d
        from shapely.geometry import Point as _Pt
        click = _Pt(wx, wy)
        for r in self.ws.mrc_state()["routes"]:
            line = LineString([(p[0], p[1]) for p in r["pts"]])
            d = line.distance(click)
            if d < bd:
                best, bd = ("route", r), d
        if best and bd < 8 / max(self.zoom, 0.2):
            self.selected = {"type": best[0], "data": best[1]}
            self._show_prop(best[1])
            self.redraw()
        else:
            self.selected = None

    def _draw_feature_preview(self, c, cam, scale, origin, pv: dict):
        """把一条布尔建议画成线框（box / cylinder / prism）。

        预览与用户实际会得到的几何对应：加减体都用加了顶/底面法向量的
        八点框表示，加减用不同颜色区分（绿=加料，红=减料）。
        """
        color = "#5fd18a" if pv.get("kind") == "add" else "#ef6f6f"
        x, y, z = pv.get("x", 0.0), pv.get("y", 0.0), pv.get("z", 0.0)
        prim = pv.get("prim", "box")
        if prim == "cylinder":
            r, dz = pv.get("r", 3.0), pv.get("dz", 2.0)
            pts = []
            for k in range(25):
                a = 2 * math.pi * k / 24
                pts.append((x + r * math.cos(a), y + r * math.sin(a), z))
            for zz in (z, z + dz):
                sp = [cam.project((p[0], p[1], zz), scale, origin) for p in pts]
                coords = []
                for s in sp:
                    coords.extend(s)
                c.create_line(*coords, fill=color, width=2)
            for k in (0, 6, 12, 18):
                sp = [cam.project((pts[k][0], pts[k][1], zz), scale, origin)
                      for zz in (z, z + dz)]
                c.create_line(*sp[0], *sp[1], fill=color, width=1, dash=(2, 2))
            c.create_text(*cam.project((x, y, z + dz), scale, origin),
                          text=pv.get("desc", "")[:12], fill=color,
                          font=("Consolas", 7))
            return
        dx, dy, dz = pv.get("dx", 10.0), pv.get("dy", 10.0), pv.get("dz", 2.0)
        x0, y0, x1, y1 = x, y, x + dx, y + dy
        corners = [(cx, cy, cz) for cx in (x0, x1) for cy in (y0, y1)
                   for cz in (z, z + dz)]
        sp = [cam.project(p, scale, origin) for p in corners]
        for i, j in ((0, 1), (1, 3), (3, 2), (2, 0), (4, 5), (5, 7),
                     (7, 6), (6, 4), (0, 4), (1, 5), (2, 6), (3, 7)):
            c.create_line(*sp[i], *sp[j], fill=color, width=2, dash=(3, 2))
        c.create_text(*cam.project((x0 + dx / 2, y0 + dy / 2, z + dz), scale, origin),
                      text=pv.get("desc", "")[:12], fill=color,
                      font=("Consolas", 7))

    def highlight_net_dialog(self):
        """CIR-003：选一个网络，只高亮该网络的飞线与焊盘。"""
        if not self._require_ws():
            return
        nets = [n["name"] for n in self.ws.nets()]
        if not nets:
            messagebox.showinfo("按网络高亮", "还没有导入板文件（无网络）。")
            return
        if self.hidden:
            self.set_highlight_net(nets[0])
            return
        dlg = tk.Toplevel(self)
        dlg.title("按网络高亮（CIR-003）")
        dlg.configure(bg=PANEL)
        dlg.transient(self)
        dlg.grab_set()
        tk.Label(dlg, text="选择网络（空选＝取消高亮）：", bg=PANEL, fg=TEXT).pack(
            anchor="w", padx=12, pady=(12, 6))
        lb = tk.Listbox(dlg, width=40, height=min(14, max(4, len(nets))),
                        bg="#1b1f24", fg=TEXT, selectbackground=ACCENT, relief="flat")
        for n in nets:
            lb.insert("end", n)
        lb.pack(padx=12, pady=(0, 8))
        cur = self.highlight_net
        if cur and cur in nets:
            lb.selection_set(nets.index(cur))

        def ok():
            sel = lb.curselection()
            self.set_highlight_net(nets[sel[0]] if sel else None)
            dlg.destroy()

        bar = tk.Frame(dlg, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(bar, text="确定", command=ok, style="Accent.TButton").pack(side="right")
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="right", padx=6)

    def _paint_items(self, c, items):
        """把绘制指令画到画布：优先光栅化贴图，不可用时逐多边形。

        实测（800×600，800 个四边形）：
          逐多边形 create_polygon + 重绘 : 72 ms/帧 = 14 fps
          PIL 光栅化 + PhotoImage 贴图   :  5 ms/帧 = 209 fps
        Tk 的软件光栅化对每个 canvas item 单独开销，是 STR-003 帧率的主要瓶颈。
        Pillow 缺失时（精简打包）自动回退，功能不受影响，只是帧率低一些。
        """
        if not items:
            return
        from .render import _pil_available, rasterize

        if _pil_available() and len(items) > 200:
            try:
                from PIL import ImageTk
                w = c.winfo_width() or 800
                h = c.winfo_height() or 600
                img = rasterize(items, w, h)
                # PhotoImage 必须保持引用，否则被 GC 回收后画布空白
                self._bg_photo = ImageTk.PhotoImage(img)
                c.create_image(0, 0, anchor="nw", image=self._bg_photo)
                return
            except Exception as e:
                # 光栅化失败不能静默：回退到多边形绘制，并把原因写到状态栏
                self.status(f"光栅化渲染失败，已回退：{type(e).__name__}", "warn")
        for it in items:
            c.create_polygon(*it["coords"], fill=it["fill"],
                             outline=it["outline"], width=it["width"],
                             dash=it["dash"] or ())

    def _to_screen(self, x, y, z=0.0):
        if self.view_mode == "3d":
            # 相机与比例在一次重绘内是常量，必须复用：
            # 网格线一次要调 400+ 次，每次重建相机会连带 step_model() 与
            # bounds_and_scale()，实测占 50 万面档每帧的 25 ms（1/3 帧时间）。
            cached = self._cam_cache
            if cached is None:
                cached = self._camera()
                self._cam_cache = cached
            cam, scale, origin = cached
            return cam.project((x, y, z), scale, origin)
        w = self.canvas.winfo_width() or 800
        h = self.canvas.winfo_height() or 600
        x0, y0, x1, y1 = self._world_bbox()
        span = max(x1 - x0, y1 - y0, 1e-6)
        s = 0.82 * min(w, h) / span * self.zoom
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return (w / 2 + (x - cx) * s + self.pan[0],
                h / 2 - (y - cy) * s + self.pan[1])

    def _model_bbox(self):
        """包含结构、板框与器件的三维包围盒（保证三者用同一投影不脱节）。"""
        ws = self.ws
        if not ws:
            return (0.0, 0.0, 0.0, 100.0, 100.0, 10.0)
        m = None
        try:
            m = ws.step_model()
        except Exception:
            m = None
        if m:
            return tuple(float(v) for v in m.bbox)
        x0, y0, x1, y1 = self._world_bbox()
        return (x0, y0, 0.0, x1, y1, 10.0)

    def toggle_heatmap(self):
        """MRC-010 热力图：按对象显示违规强度，可开关。"""
        if not self._require_ws():
            return
        if not self.mrc.get("issues"):
            self.run_mrc()
        self.heatmap_on = not self.heatmap_on
        self.redraw()
        if self.heatmap_on:
            hm = self._heat_data()
            n = len(hm["points"]) if hm else 0
            self.status(f"热力图已开启：{n} 个对象存在违规（红=重 / 黄=中 / 浅=轻）", "warn")
        else:
            self.status("热力图已关闭")

    def on_canvas_drag(self, ev):
        if self._orbit:
            x0, y0, az0, el0 = self._orbit
            self.cam_az = (az0 + (ev.x - x0) * 0.5) % 360.0
            self.cam_el = max(5.0, min(89.0, el0 + (ev.y - y0) * 0.4))
            self.redraw()
            return
        if self.tool == "route" and self._drag_pts:
            self.redraw()
            coords = []
            for p in self._drag_pts:
                coords.extend(self._to_screen(p[0], p[1]))
            coords.extend([ev.x, ev.y])
            self.canvas.create_line(*coords, fill=GOOD, width=2, dash=(4, 2))

    def on_canvas_release(self, ev):
        if self._orbit:
            self._orbit = None
            self.status(f"视角：方位 {self.cam_az:.0f}° 俯仰 {self.cam_el:.0f}°"
                        "（左键拖拽旋转 / 右键拖拽平移 / 滚轮缩放）")

    def _world_bbox(self):
        ws = self.ws
        if not ws:
            return (0, 0, 100, 100)
        outline, _ = ws.board_outline()
        if outline:
            xs = [p[0] for p in outline]
            ys = [p[1] for p in outline]
            return (min(xs), min(ys), max(xs), max(ys))
        m = ws.step_model()
        if m:
            return (m.bbox[0], m.bbox[1], m.bbox[3], m.bbox[4])
        return (0, 0, 100, 100)

    def _camera(self):
        """按当前窗口尺寸与工程范围构造相机；结果缓存供本次重绘复用。"""
        from .render import Camera, bounds_and_scale

        w = self.canvas.winfo_width() or 800
        h = self.canvas.winfo_height() or 600
        cam = Camera(az_deg=self.cam_az, el_deg=self.cam_el, zoom=self.zoom,
                     pan=(self.pan[0], self.pan[1]))
        bbox = self._model_bbox()
        cam.center = ((bbox[0] + bbox[3]) / 2, (bbox[1] + bbox[4]) / 2,
                      (bbox[2] + bbox[5]) / 2)
        scale, origin = bounds_and_scale(bbox, cam, w, h)
        return cam, scale, origin

    def _heat_data(self) -> dict | None:
        if not self.ws or not self.mrc.get("issues"):
            return None
        try:
            # 复用当前评分结果：画布每次重绘都会取热力图，现场再跑一遍
            # run_mrc 会与重绘叠加（308 走线实测 109 ms/次）。
            return heatmap(self.ws, self.mrc["issues"], self.mrc.get("score"))
        except Exception as e:
            # 不静默吞异常：热力图数据算不出来要让用户知道（UI-005 失败要有说明）
            self.status(f"热力图数据生成失败：{type(e).__name__}: {e}", "error")
            return None

    def _from_screen(self, px, py):
        w = self.canvas.winfo_width() or 800
        h = self.canvas.winfo_height() or 600
        x0, y0, x1, y1 = self._world_bbox()
        span = max(x1 - x0, y1 - y0, 1e-6)
        s = 0.82 * min(w, h) / span * self.zoom
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        sx = (px - w / 2 - self.pan[0]) / s
        sy = (py - h / 2 - self.pan[1]) / s
        return (cx + sx, cy - sy)

    def on_canvas_pan_start(self, ev):
        self._pan_drag = (ev.x, ev.y, self.pan[0], self.pan[1])

    def on_canvas_pan(self, ev):
        if not self._pan_drag:
            return
        x0, y0, px, py = self._pan_drag
        self.pan = [px + (ev.x - x0), py + (ev.y - y0)]
        self.redraw()

    def on_canvas_pan_end(self, ev):
        self._pan_drag = None

    def on_wheel(self, ev):
        self.zoom *= 1.1 if ev.delta > 0 else 1 / 1.1
        self.zoom = max(0.15, min(12.0, self.zoom))
        self.redraw()

    def fit_view(self):
        """适配窗口：重置缩放、平移与相机角度到默认等轴测视角。"""
        self.zoom = 1.0
        self.pan = [0.0, 0.0]
        self.cam_az = 45.0
        self.cam_el = 35.0
        self.redraw()

    def set_view(self, mode: str):
        self.view_mode = mode
        self.lbl_view.configure(text=("三维（等轴测）" if mode == "3d" else "二维俯视（布线）"))
        self.redraw()
        self.status("切换到三维视图（只显示与校验）" if mode == "3d"
                    else "切换到二维布线视图（CIR-004：首版在二维布线）")

    def toggle_layer_visible(self):
        self.layers_visible = not self.layers_visible
        self.status("层（切片道次）显示：开" if self.layers_visible else
                    "层（切片道次）显示：关")
        self.redraw()

    def toggle_ratlines(self):
        """CIR-003：飞线显示开关。"""
        self.show_ratlines = not self.show_ratlines
        self.status("飞线显示：开" if self.show_ratlines else "飞线显示：关")
        self.redraw()

    def set_highlight_net(self, net: str | None):
        self.highlight_net = net
        self.show_ratlines = True
        self.redraw()
        self.status(f"已高亮网络 {net}（飞线与焊盘）" if net else "已取消网络高亮", "ok")

