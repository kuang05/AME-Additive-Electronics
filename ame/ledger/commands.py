"""工程命令总线（写入口）：所有 `cmd_*` 都在这里。

从 `workspace.py` 拆出。`Workspace` 通过继承获得这些方法，对外接口不变。

为什么单独成文件
----------------
命令方法是**唯一的写入口**（需求 AGT-009：人与 Agent 同源）。它们共同遵守
三条不变量，集中在一处才能一眼看出是否被违反：

1. 先校验、后写库（非法输入抛 ``CmdError``，绝不半途落库）；
2. 每次写都登记 undo/redo 闭包到 `_log`（PRJ-005 撤销栈）；
3. 写后 ``invalidate()``（视图缓存必须失效，否则界面显示旧状态）。

业务域划分见各方法的 docstring；状态字段与只读视图仍在 `Workspace`。
"""
from __future__ import annotations

import json

from .. import new_id
from ..circuit import connectivity as conn
from ..circuit.kicad import import_kicad_pcb, import_netlist_json
from ..db.migrations import CURRENT_SCHEMA_VERSION  # noqa: F401
from ..db.store import now_iso
from ..geometry import analysis
from ..geometry.stepio import read_step
from ..logging_setup import get_logger
from .errors import CmdError

_LOG = get_logger("workspace")


class CommandsMixin:
    def cmd_set_tess_tol(self, tol: float) -> dict:
        """设置网格容差（STR-008）：0.02 默认，可调大到 0.05 降面数。"""
        try:
            tol = float(tol)
        except (TypeError, ValueError) as e:
            raise CmdError(f"网格容差必须是数值，收到 {tol!r}（STR-008）") from e
        if not (0.001 <= tol <= 1.0):
            raise CmdError(f"网格容差 {tol} 超出允许范围 0.001–1.0 mm（STR-008）")
        s = self.structure()
        if not s:
            raise CmdError("无结构，无法设置网格容差")
        old = s["tess_tol"]
        self.store.execute("UPDATE structure SET tess_tol=? WHERE id=?", (tol, s["id"]))
        self.invalidate()             # 容差变了，模型缓存必须失效

        def undo():
            self.store.execute("UPDATE structure SET tess_tol=? WHERE id=?", (old, s["id"]))
            self.invalidate()

        self._log("set_tess_tol", {"tol": tol}, (undo, ()),
                  label=f"网格容差 {old}→{tol} mm")
        return {"ok": True, "tess_tol": tol}

    def cmd_import_step(self, text: str, filename="model.step") -> dict:
        s = self.structure()
        if s:
            raise CmdError("工程已有结构，重复导入请先删除（避免隐式替换）")
        model = read_step(text)
        if not model.faces:
            # 分情况给原因：空文件 / 只有不支持的结构 / 有三角化但解析失败，
            # 一律说"请确认 AP203/214/242 格式"会误导用户（文件可能格式完全正确）。
            diags = {d.get("code") for d in model.diagnostics}
            if "G-TESS-INDEX-INVALID" in diags:
                hint = ("文件是三角化网格，但三角形索引越界或坐标缺失。"
                        "请检查导出是否完整，或在 CAD 中重新导出网格")
            elif text.count("ADVANCED_FACE") or text.count("TRIANGULATED_FACE_SET"):
                hint = ("文件里有面定义但都未能解析出有效几何（可能是退化面/"
                        "全部顶点共线）。请在 CAD 中检查模型或重新导出")
            else:
                hint = ("文件里没有任何面定义（ADVANCED_FACE / TRIANGULATED_FACE_SET 均未找到）。"
                        "请确认导出的是实体/曲面模型，而不是只含装配或空模型")
            raise CmdError(f"STEP 解析失败：未识别到任何面（诊断码 G-NO-FACE）。{hint}")
        # 解析通过后再落 blob：失败路径不留孤儿文件与索引记录（NFR-002）
        h, rel = self.store.put_blob(text.encode("utf-8"))
        outline, onote = analysis.find_board_outline(model)
        sid = new_id()
        self.store.execute(
            "INSERT INTO structure(id,project_id,source_step,step_hash,unit,tess_tol,"
            "bbox_json,outline_json,holes_json,diag_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, self.project_id, rel, h, model.unit, 0.02,
             json.dumps(list(model.bbox)),
             json.dumps(outline) if outline else "",
             json.dumps(analysis.board_holes(model)),
             json.dumps(model.diagnostics, ensure_ascii=False)))

        def undo():
            self.store.execute("DELETE FROM structure WHERE id=?", (sid,))
            self.store.execute("DELETE FROM blob WHERE sha256=?", (h,))

        self._log("import_step", {"file": filename, "hash": h},
                  (undo, ()), label=f"导入 STEP {filename}")
        return {"id": sid, "hash": h, "bbox": list(model.bbox),
                "face_count": len(model.faces), "solid_count": model.solid_count,
                "diagnostics": model.diagnostics,
                "outline": outline, "outline_note": onote}

    def cmd_set_outline(self, pts: list) -> dict:
        s = self.structure()
        if not s:
            raise CmdError("无结构，无法设置板框")
        model = self.step_model()
        clean, warning = self._validate_outline(pts, model.bbox if model else None)
        old = s["outline_json"]
        self.store.execute("UPDATE structure SET outline_json=? WHERE id=?",
                           (json.dumps(clean), s["id"]))

        def undo():
            self.store.execute("UPDATE structure SET outline_json=? WHERE id=?",
                               (old, s["id"]))

        self._log("set_outline", {"pts": clean}, (undo, ()), label="设置板框")
        return {"ok": True, "outline": clean, "warning": warning}

    def cmd_import_board(self, text: str, fmt="kicad_pcb") -> dict:
        if self.nets():
            raise CmdError("工程已有电路数据，重复导入请先删除")
        bd = import_kicad_pcb(text) if fmt == "kicad_pcb" else import_netlist_json(text)
        cid = new_id()
        from .. import sha256_text
        # 板文件原始内容也要入库（DAT-004/005）：早期只算了哈希没落 blob，
        # 结果哈希是悬空的——既无法校验外部改动，导出工程包时也丢板文件。
        board_hash, _rel = self.store.put_blob(text.encode("utf-8"))
        assert board_hash == sha256_text(text)
        # 整个导入放在一个事务里（CIR-012/NFR-002）：中途任何异常都回滚，
        # 不会留下"半个工程"（早期实现逐条 execute 各自提交，解析到一半
        # 出错会留下 circuit/comp/net 半套数据，且后续导入被"已有电路"挡住）。
        with self.store.batch():
            self.store.execute("INSERT INTO circuit(id,project_id,netlist_source,netlist_hash,"
                               "format,imported_at) VALUES (?,?,?,?,?,?)",
                               (cid, self.project_id, fmt, board_hash, bd.format,
                                now_iso()))
            comp_ids, pad_ids = {}, []
            for c in bd.comps:
                cidx = new_id()
                comp_ids[c["ref"]] = cidx
                self.store.execute("INSERT INTO comp(id,circuit_id,ref,footprint,x_mm,y_mm,"
                                   "surface,rot_deg,height_mm,locked) VALUES (?,?,?,?,?,?,?,?,?,0)",
                                   (cidx, cid, c["ref"], c["footprint"], c["x"], c["y"],
                                    c["surface"], c["rot"], c["height"]))
            net_ids = {}
            for name in sorted(bd.nets):
                nid = new_id()
                net_ids[name] = nid
                self.store.execute("INSERT INTO net(id,circuit_id,name,klass,pad_count)"
                                   " VALUES (?,?,?,?,0)", (nid, cid, name, bd.nets[name]["klass"]))
            for p in bd.pads:
                if p["ref"] not in comp_ids:
                    raise CmdError(f"焊盘所属器件 {p['ref']} 不存在，板文件不一致")
                pid = new_id()
                pad_ids.append(pid)
                self.store.execute("INSERT INTO pad(id,comp_id,net_id,pad_no,x_mm,y_mm,"
                                   "w_mm,h_mm,shape) VALUES (?,?,?,?,?,?,?,?,?)",
                                   (pid, comp_ids[p["ref"]],
                                    net_ids.get(p["net"]), p["pad_no"], p["x"], p["y"],
                                    p.get("w", 1.5), p.get("h", 1.0), p["shape"]))
                if p["net"]:
                    self.store.execute("UPDATE net SET pad_count=pad_count+1 WHERE id=?",
                                       (net_ids[p["net"]],))
            # 导入走线原样入库：宽度不做下限校验（合规交给 MRC；CIR-010 只拦交互式修改）
            imported_routes = 0
            for r in bd.routes:
                nid = net_ids.get(r["net"])
                if not nid:
                    continue
                self.store.execute("INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,"
                                   "surface,layer_index,strategy) VALUES (?,?,?,?,?,?,0,'imported')",
                                   (new_id(), cid, nid, json.dumps(r["pts"]),
                                    r["width"], r["surface"]))
                imported_routes += 1
            # 板框：结构有就用结构的；否则把板文件 Edge.Cuts 记为回退板框（STR-007/CIR-001）
            outline_note = ""
            if bd.outline:
                s = self.structure()
                if s and not s["outline_json"]:
                    self.cmd_set_outline(bd.outline)
                    outline_note = "板框取自板文件 Edge.Cuts"
                elif not s:
                    # 用标准 UPSERT（SQLite 3.24+ 与 PostgreSQL 都支持），
                    # 不用 INSERT OR REPLACE——后者是 SQLite 方言，在 RDS 上会直接报错，
                    # 而这个分支恰好是"无结构但板文件带板框"的常见路径。
                    self.store.execute(
                        "INSERT INTO board_outline(project_id,outline_json,source)"
                        " VALUES (?,?, 'board_file')"
                        " ON CONFLICT(project_id) DO UPDATE SET"
                        " outline_json=excluded.outline_json, source=excluded.source",
                        (self.project_id, json.dumps([[round(p[0], 3), round(p[1], 3)]
                                                      for p in bd.outline])))
                    outline_note = "板框取自板文件 Edge.Cuts（无结构，使用板文件板框）"

        def undo():
            # FK 依赖序：route/jumper→pad/comp/net→circuit
            self.store.execute("DELETE FROM route WHERE circuit_id=?", (cid,))
            self.store.execute("DELETE FROM jumper WHERE net_id IN "
                               "(SELECT id FROM net WHERE circuit_id=?)", (cid,))
            self.store.execute("DELETE FROM pad WHERE comp_id IN "
                               "(SELECT id FROM comp WHERE circuit_id=?)", (cid,))
            self.store.execute("DELETE FROM comp WHERE circuit_id=?", (cid,))
            self.store.execute("DELETE FROM net WHERE circuit_id=?", (cid,))
            self.store.execute("DELETE FROM circuit WHERE id=?", (cid,))
            self.store.execute("DELETE FROM blob WHERE sha256=?", (board_hash,))

        self._log("import_board", {"fmt": fmt, "comps": len(bd.comps),
                                   "routes": imported_routes},
                  (undo, ()), label=f"导入板文件（{len(bd.comps)} 器件）")
        return {"circuit_id": cid, "comps": len(bd.comps), "pads": len(bd.pads),
                "nets": len(bd.nets), "routes": imported_routes,
                "warnings": bd.warnings, "outline": outline_note}

    def cmd_add_route(self, net_name: str, pts: list, width=0.6, surface="top") -> dict:
        net = self.store.one("SELECT n.id FROM net n JOIN circuit c ON n.circuit_id=c.id "
                             "WHERE c.project_id=? AND n.name=?", (self.project_id, net_name))
        if not net:
            raise CmdError(f"网络 {net_name} 不存在")
        outline, _ = self.board_outline()
        if not outline:
            raise CmdError("无板框，不能布线（STR-007：无板框不能进入切片/布线）")
        ok, why = conn.validate_route(pts, width, outline,
                                      keepouts=self.keepouts())
        if not ok:
            raise CmdError(why)
        cid = self.store.one("SELECT id FROM circuit WHERE project_id=?",
                             (self.project_id,))["id"]
        rid = new_id()
        z3 = [[p[0], p[1], p[2] if len(p) > 2 else 0.0] for p in pts]
        self.store.execute("INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,"
                           "surface,layer_index,strategy) VALUES (?,?,?,?,?,?,0,?)",
                           (rid, cid, net["id"], json.dumps(z3), width, surface, "manual"))

        def undo():
            self.store.execute("DELETE FROM route WHERE id=?", (rid,))

        def redo():
            self.store.execute("INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,"
                               "surface,layer_index,strategy) VALUES (?,?,?,?,?,?,0,?)",
                               (rid, cid, net["id"], json.dumps(z3), width, surface, "manual"))

        self._log("add_route", {"net": net_name, "width": width},
                  (undo, ()), (redo, ()), label=f"布线 {net_name}")
        return {"id": rid}

    def cmd_delete_route(self, route_id: str) -> dict:
        r = self.store.one("SELECT * FROM route WHERE id=?", (route_id,))
        if not r:
            raise CmdError("走线不存在")

        def undo():
            self.store.execute("INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,"
                               "surface,layer_index,strategy) VALUES (?,?,?,?,?,?,?,?)",
                               (r["id"], r["circuit_id"], r["net_id"], r["pts_json"],
                                r["width_mm"], r["surface"], r["layer_index"], r["strategy"]))

        def redo():
            self.store.execute("DELETE FROM route WHERE id=?", (route_id,))

        self.store.execute("DELETE FROM route WHERE id=?", (route_id,))
        self._log("delete_route", {"id": route_id}, (undo, ()), (redo, ()), label="删除走线")
        return {"ok": True}

    def cmd_change_width(self, route_id: str, width: float) -> dict:
        r = self.store.one("SELECT * FROM route WHERE id=?", (route_id,))
        if not r:
            raise CmdError("走线不存在")
        if width < conn.PROCESS_MIN_WIDTH:
            raise CmdError(f"线宽 {width:.2f} mm 低于工艺下限 "
                           f"{conn.PROCESS_MIN_WIDTH} mm（CIR-010）")
        self.store.execute("UPDATE route SET width_mm=? WHERE id=?", (width, route_id))

        self._log("change_width", {"id": route_id, "width": width},
                  (self._setter("route", "width_mm", r["width_mm"], "id", route_id), ()),
                  (self._setter("route", "width_mm", width, "id", route_id), ()),
                  label="修改线宽")
        return {"ok": True}

    def cmd_set_net_class(self, net_name: str, klass: str, dry_run=False) -> dict:
        """改网络类（CIR-010）：先给出影响范围，再执行。

        dry_run=True 时只返回受影响对象，不写库（"修改前显示影响范围"）。
        """
        allowed = ("signal", "power", "high_speed", "analog")
        if klass not in allowed:
            raise CmdError(f"网络类 {klass!r} 非法，只允许 {'/'.join(allowed)}（CIR-010）")
        net = self.store.one(
            "SELECT n.* FROM net n JOIN circuit c ON n.circuit_id=c.id "
            "WHERE c.project_id=? AND n.name=?", (self.project_id, net_name))
        if not net:
            raise CmdError(f"网络 {net_name} 不存在")
        routes = self.store.query("SELECT id, width_mm FROM route WHERE net_id=?",
                                  (net["id"],))
        scope = {"net": net_name, "old_class": net["klass"], "new_class": klass,
                 "routes": len(routes), "pads": net["pad_count"]}
        if dry_run:
            return {"scope": scope, "applied": False}
        old = net["klass"]
        self.store.execute("UPDATE net SET klass=? WHERE id=?", (klass, net["id"]))

        def undo():
            self.store.execute("UPDATE net SET klass=? WHERE id=?", (old, net["id"]))

        self._log("set_net_class", {"net": net_name, "klass": klass}, (undo, ()),
                  label=f"网络类 {net_name}: {old}→{klass}")
        return {"scope": scope, "applied": True}

    def cmd_set_footprint(self, comp_id: str, footprint: str, dry_run=False) -> dict:
        """改封装（CIR-010）：封装变化可能影响焊盘尺寸，先报告影响范围。"""
        c = self.store.one("SELECT * FROM comp WHERE id=?", (comp_id,))
        if not c:
            raise CmdError("器件不存在")
        footprint = (footprint or "").strip()
        if not footprint:
            raise CmdError("封装名不能为空（CIR-010）")
        pads = self.store.query("SELECT id, pad_no FROM pad WHERE comp_id=?", (comp_id,))
        scope = {"ref": c["ref"], "old_footprint": c["footprint"],
                 "new_footprint": footprint, "pads": len(pads)}
        if dry_run:
            return {"scope": scope, "applied": False}
        old = c["footprint"]
        self.store.execute("UPDATE comp SET footprint=? WHERE id=?", (footprint, comp_id))

        def undo():
            self.store.execute("UPDATE comp SET footprint=? WHERE id=?", (old, comp_id))

        self._log("set_footprint", {"ref": c["ref"], "footprint": footprint}, (undo, ()),
                  label=f"封装 {c['ref']}: {old}→{footprint}")
        return {"scope": scope, "applied": True}

    def cmd_move_comp(self, comp_id: str, x: float, y: float) -> dict:
        c = self.store.one("SELECT * FROM comp WHERE id=?", (comp_id,))
        if not c:
            raise CmdError("器件不存在")
        if c["locked"]:
            raise CmdError(f"器件 {c['ref']} 已锁定，不能移动（CIR-002）")
        dx, dy = x - c["x_mm"], y - c["y_mm"]
        pads0 = self.store.query(
            "SELECT id, x_mm, y_mm FROM pad WHERE comp_id=?", (comp_id,))

        def move(abs_x, abs_y, shift_x, shift_y):
            """把器件放到 (abs_x, abs_y)，焊盘 = 原始坐标 + (shift_x, shift_y)。

            以 pads0 为基准算绝对坐标（而不是在库里累加），前进/撤销都精确可逆；
            库内累加会引入浮点误差（22.8 → 22.799999999999997）。
            """
            with self.store.batch():
                self.store.execute("UPDATE comp SET x_mm=?, y_mm=? WHERE id=?",
                                   (abs_x, abs_y, comp_id))
                # 焊盘随器件平移，保持相对位置（CIR-002）。
                # 漏掉这一步会让器件与焊盘脱节，后续连通性/MRC/切片全部基于错位坐标。
                for p in pads0:
                    self.store.execute(
                        "UPDATE pad SET x_mm=?, y_mm=? WHERE id=?",
                        (p["x_mm"] + shift_x, p["y_mm"] + shift_y, p["id"]))

        move(x, y, dx, dy)

        self._log("move_comp", {"ref": c["ref"], "x": x, "y": y},
                  (lambda: move(c["x_mm"], c["y_mm"], 0.0, 0.0), ()),
                  (lambda: move(x, y, dx, dy), ()),
                  label=f"移动 {c['ref']}")
        return {"ok": True}

    def cmd_place_candidates(self, top_k: int = 3) -> list[dict]:
        """自动布局候选（CIR-005）：只返回候选，不修改工程。"""
        st = self.mrc_state()
        outline, note = self.board_outline()
        if not outline:
            raise CmdError(f"无板框，无法自动布局（{note}）")
        from ..circuit.connectivity import count_overlaps, place_candidates
        cands = place_candidates(st["comps"], st["pads"], st["routes"], outline,
                                 top_k=top_k)
        for c in cands:
            c["overlaps"] = count_overlaps(c["positions"])
            c["delta_mm"] = round(c["ratline_mm"] - c["baseline_mm"], 1)
        # 可打性优先：无重叠的排前面，其次飞线更短
        cands.sort(key=lambda c: (c["overlaps"], c["ratline_mm"]))
        return cands

    def cmd_apply_placement(self, positions: dict[str, list[float]]) -> dict:
        """应用布局候选（CIR-005 用户选择后执行）：一次事务，可撤销。"""
        comps = {c["ref"]: c for c in self.comps()}
        old: dict[str, tuple] = {}
        applied = 0
        for ref, _xy in positions.items():
            c = comps.get(ref)
            if not c:
                raise CmdError(f"器件 {ref} 不存在")
            if c["locked"]:
                continue                      # 锁定器件不移动（CIR-002）
            old[ref] = (c["x_mm"], c["y_mm"])
            applied += 1
        if not applied:
            raise CmdError("没有可移动的器件（全部锁定）")

        def move_to(mapping: dict[str, tuple]):
            def _f():
                with self.store.batch():
                    for ref, (x, y) in mapping.items():
                        c = comps[ref]
                        self.store.execute(
                            "UPDATE comp SET x_mm=?, y_mm=? WHERE id=?",
                            (x, y, c["id"]))
                        # 焊盘随器件平移，保持相对位置（CIR-002 焊盘随动）
                        self.store.execute(
                            "UPDATE pad SET x_mm = x_mm + ?, y_mm = y_mm + ? "
                            "WHERE comp_id=?", (x - old[ref][0], y - old[ref][1], c["id"]))
            return _f

        new = {ref: tuple(xy) for ref, xy in positions.items() if ref in old}
        move_to(new)()
        # 撤销时需把焊盘移回：直接用原坐标差值
        def undo():
            with self.store.batch():
                for ref, (x, y) in old.items():
                    c = comps[ref]
                    cur = self.store.one("SELECT x_mm,y_mm FROM comp WHERE id=?", (c["id"],))
                    self.store.execute("UPDATE comp SET x_mm=?, y_mm=? WHERE id=?",
                                       (x, y, c["id"]))
                    self.store.execute(
                        "UPDATE pad SET x_mm = x_mm + ?, y_mm = y_mm + ? WHERE comp_id=?",
                        (x - cur["x_mm"], y - cur["y_mm"], c["id"]))

        def redo():
            with self.store.batch():
                for ref, (x, y) in new.items():
                    c = comps[ref]
                    cur = self.store.one("SELECT x_mm,y_mm FROM comp WHERE id=?", (c["id"],))
                    self.store.execute("UPDATE comp SET x_mm=?, y_mm=? WHERE id=?",
                                       (x, y, c["id"]))
                    self.store.execute(
                        "UPDATE pad SET x_mm = x_mm + ?, y_mm = y_mm + ? WHERE comp_id=?",
                        (x - cur["x_mm"], y - cur["y_mm"], c["id"]))

        self._log("apply_placement", {"comps": applied}, (undo, ()), (redo, ()),
                  label=f"应用布局（{applied} 器件）")
        return {"applied": applied, "skipped_locked": len(positions) - applied}

    def cmd_auto_route_candidates(self, net_name: str, width=0.6) -> list[dict]:
        """单网络自动布线候选（CIR-006）：避障（含禁布区）、给原因，不自动应用。"""
        net = self.store.one("SELECT n.id FROM net n JOIN circuit c ON n.circuit_id=c.id "
                             "WHERE c.project_id=? AND n.name=?", (self.project_id, net_name))
        if not net:
            raise CmdError(f"网络 {net_name} 不存在")
        outline, note = self.board_outline()
        if not outline:
            raise CmdError(f"无板框，不能布线（{note}）")
        st = self.mrc_state()
        from ..circuit.connectivity import auto_route_candidates
        return auto_route_candidates(net_name, st["pads"], st["routes"], st["comps"],
                                     outline, width=width,
                                     keepouts=self.keepouts())

    def cmd_add_keepout(self, pts: list, name="", note="") -> dict:
        """新增禁布区（CIR-006）：自动布线会绕开，且不允许在区内落线。"""
        if not isinstance(pts, (list, tuple)) or len(pts) < 3:
            raise CmdError("禁布区至少需要 3 个顶点")
        try:
            clean = [[round(float(p[0]), 3), round(float(p[1]), 3)] for p in pts]
        except (TypeError, ValueError, IndexError) as e:
            raise CmdError("禁布区顶点必须是 (x, y) 数值对") from e
        try:
            from shapely.geometry import Polygon
            g = Polygon(clean)
            if not g.is_valid:
                g = g.buffer(0)
        except Exception as e:
            raise CmdError(f"禁布区多边形无效：{e}") from e
        if g.is_empty or g.area <= 0:
            raise CmdError("禁布区面积为零，请检查顶点顺序")
        kid = new_id()
        self.store.execute(
            "INSERT INTO keepout(id,project_id,name,pts_json,note) VALUES (?,?,?,?,?)",
            (kid, self.project_id, name or "禁布区", json.dumps(clean), note))

        def undo():
            self.store.execute("DELETE FROM keepout WHERE id=?", (kid,))

        def redo():
            self.store.execute(
                "INSERT INTO keepout(id,project_id,name,pts_json,note) "
                "VALUES (?,?,?,?,?)",
                (kid, self.project_id, name or "禁布区", json.dumps(clean), note))

        self._log("add_keepout", {"name": name, "pts": len(clean)}, (undo, ()),
                  (redo, ()),
                  label=f"新增禁布区 {name or ''}".strip())
        return {"id": kid, "name": name or "禁布区", "area_mm2": round(g.area, 2)}

    def cmd_delete_keepout(self, keepout_id: str) -> dict:
        """删除禁布区（CIR-006）。"""
        row = self.store.one("SELECT * FROM keepout WHERE id=? AND project_id=?",
                             (keepout_id, self.project_id))
        if not row:
            raise CmdError("禁布区不存在")
        self.store.execute("DELETE FROM keepout WHERE id=?", (keepout_id,))

        def undo():
            self.store.execute(
                "INSERT INTO keepout(id,project_id,name,pts_json,note) VALUES (?,?,?,?,?)",
                (row["id"], row["project_id"], row["name"], row["pts_json"], row["note"]))

        self._log("delete_keepout", {"name": row["name"]}, (undo, ()),
                  label=f"删除禁布区 {row['name']}")
        return {"ok": True}

    def cmd_lock_comp(self, comp_id: str, locked: bool) -> dict:
        old = self.store.one("SELECT locked FROM comp WHERE id=?", (comp_id,))
        if not old:
            raise CmdError("器件不存在")
        self.store.execute("UPDATE comp SET locked=? WHERE id=?", (1 if locked else 0, comp_id))

        self._log("lock_comp", {"id": comp_id, "locked": locked},
                  (self._setter("comp", "locked", old["locked"], "id", comp_id), ()),
                  (self._setter("comp", "locked", 1 if locked else 0, "id", comp_id), ()),
                  label=("锁定器件" if locked else "解锁器件"))
        return {"ok": True}

    def cmd_add_jumper(self, net_name: str, from_pad: str, to_pad: str, reason="") -> dict:
        net = self.store.one("SELECT n.id FROM net n JOIN circuit c ON n.circuit_id=c.id "
                             "WHERE c.project_id=? AND n.name=?", (self.project_id, net_name))
        if not net:
            raise CmdError(f"网络 {net_name} 不存在")
        jid = new_id()
        self.store.execute("INSERT INTO jumper(id,net_id,from_pad_id,to_pad_id,body,reason)"
                           " VALUES (?,?,?,?, '0R', ?)",
                           (jid, net["id"], from_pad, to_pad, reason))

        def undo():
            self.store.execute("DELETE FROM jumper WHERE id=?", (jid,))

        def redo():
            self.store.execute("INSERT INTO jumper(id,net_id,from_pad_id,to_pad_id,body,"
                               "reason) VALUES (?,?,?,?, '0R', ?)",
                               (jid, net["id"], from_pad, to_pad, reason))

        self._log("add_jumper", {"net": net_name}, (undo, ()), (redo, ()),
                  label=f"加跳线 {net_name}")
        return {"id": jid}

    def cmd_add_feature(self, prim: str, kind: str, params: dict, note="") -> dict:
        s = self.structure()
        if not s:
            raise CmdError("无结构，无法添加特征")
        fid = new_id()
        self.store.execute("INSERT INTO feature(id,structure_id,kind,prim,dims_json,"
                           "transform_json,enabled,status,note) VALUES (?,?,?,?,?,?,1,"
                           "'pending',?)",
                           (fid, s["id"], kind, prim, json.dumps(params, ensure_ascii=False),
                            '{"t":[0,0,0],"rz":0}', note))

        def undo():
            self.store.execute("DELETE FROM feature WHERE id=?", (fid,))

        def redo():
            self.store.execute("INSERT INTO feature(id,structure_id,kind,prim,dims_json,"
                               "transform_json,enabled,status,note) VALUES (?,?,?,?,?,?,1,"
                               "'pending',?)",
                               (fid, s["id"], kind, prim,
                                json.dumps(params, ensure_ascii=False),
                                '{"t":[0,0,0],"rz":0}', note))

        self._log("add_feature", {"prim": prim, "kind": kind}, (undo, ()), (redo, ()),
                  label=f"布尔建议 {kind}:{prim}（仅预览）")
        return {"id": fid}

    def cmd_reject_feature(self, fid: str) -> dict:
        """拒绝布尔建议：模型与分数不变（STR-005 验收）。"""
        self.store.execute("UPDATE feature SET status='rejected', enabled=0 WHERE id=?", (fid,))
        self._log("reject_feature", {"id": fid}, (None, ()), label="拒绝布尔建议")
        return {"ok": True}

    def cmd_set_feature_enabled(self, fid: str, enabled: bool) -> dict:
        """启用/停用特征（STR-006）。停用后该特征不参与后续布尔/V1 重建。"""
        row = self.store.one("SELECT * FROM feature WHERE id=?", (fid,))
        if not row:
            raise CmdError("特征不存在")
        old = row["enabled"]
        new = 1 if enabled else 0
        self.store.execute("UPDATE feature SET enabled=? WHERE id=?", (new, fid))

        def undo():
            self.store.execute("UPDATE feature SET enabled=? WHERE id=?", (old, fid))

        self._log("set_feature_enabled", {"id": fid, "enabled": enabled}, (undo, ()),
                  label=f"{'启用' if enabled else '停用'}特征")
        return {"ok": True, "enabled": bool(new)}

    def cmd_delete_feature(self, fid: str) -> dict:
        """删除特征（STR-006）。"""
        row = self.store.one("SELECT * FROM feature WHERE id=?", (fid,))
        if not row:
            raise CmdError("特征不存在")
        self.store.execute("DELETE FROM feature WHERE id=?", (fid,))

        def undo():
            self.store.execute(
                "INSERT INTO feature(id,structure_id,kind,prim,dims_json,transform_json,"
                "enabled,status,note) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], row["structure_id"], row["kind"], row["prim"],
                 row["dims_json"], row["transform_json"], row["enabled"],
                 row["status"], row["note"]))

        self._log("delete_feature", {"id": fid}, (undo, ()),
                  label=f"删除特征（{row['note'] or row['prim']}）")
        return {"ok": True}

    def cmd_reorder_features(self, order: list[str]) -> dict:
        """调整特征顺序（STR-006）：布尔按列表顺序执行，先加后减会影响结果。"""
        rows = {r["id"]: r for r in self.features()}
        missing = [f for f in order if f not in rows]
        if missing:
            raise CmdError(f"特征不存在：{missing[:3]}")
        # 撤销必须恢复到**原来的 seq 值**，不能按字典顺序重新编号——
        # 若特征之前已排过序（如 seq = [2,0,1]），按枚举顺序写会静默变成 [0,1,2]。
        old_seq: dict[str, int] = {}
        for fid, r in rows.items():
            try:
                tf = json.loads(r["transform_json"])
            except (json.JSONDecodeError, TypeError):
                tf = {}
            # 没有 seq 字段的老数据按查询顺序（即当前显示顺序）对待
            old_seq[fid] = int(tf.get("seq", list(rows).index(fid)))

        # 用 transform_json 里的 seq 记录顺序（不新增列，向后兼容）
        def apply(seq_map):
            with self.store.batch():
                for fid, seq in seq_map.items():
                    row = rows[fid]
                    try:
                        tf = json.loads(row["transform_json"])
                    except (json.JSONDecodeError, TypeError):
                        tf = {"t": [0, 0, 0], "rz": 0}
                    tf["seq"] = seq
                    self.store.execute(
                        "UPDATE feature SET transform_json=? WHERE id=?",
                        (json.dumps(tf, ensure_ascii=False), fid))

        # order 是"期望的显示顺序"：第一个应得 seq=0
        new_map = {fid: i for i, fid in enumerate(order)}
        apply(new_map)

        def undo():
            apply(old_seq)

        self._log("reorder_features", {"order": order}, (undo, ()),
                  label=f"特征排序（{len(order)} 项）")
        return {"ok": True, "order": order}

    def cmd_set_params(self, **kv) -> dict:
        p = self.params()
        kv = {k: v for k, v in kv.items() if k in p and k != "id"}
        if not kv:
            raise CmdError("无有效参数")
        kv = self._validate_params(kv)
        old = {k: p[k] for k in kv}
        sets = ", ".join(f"{k}=?" for k in kv)
        vals = [kv[k] for k in kv]
        self.store.execute(f"UPDATE process_params SET {sets} WHERE id=?", (*vals, p["id"]))

        def apply(vals_map):
            def _f():
                if vals_map:
                    us = ", ".join(f"{k}=?" for k in vals_map)
                    self.store.execute(f"UPDATE process_params SET {us} WHERE id=?",
                                       (*vals_map.values(), p["id"]))
            return _f

        self._log("set_params", kv, (apply(old), ()),
                  (apply({k: kv[k] for k in kv if k in p}), ()), label="修改工艺参数")
        return {"ok": True, "changed": list(old)}

    def cmd_set_rule(self, code: str, threshold: dict | None = None, enabled=None) -> dict:
        r = self.store.one("SELECT * FROM mrc_rule WHERE code=?", (code,))
        if not r:
            raise CmdError(f"规则 {code} 不存在")
        old_thr, old_en = r["threshold_json"], r["enabled"]
        if threshold is not None:
            threshold = self._validate_rule_threshold(code, threshold)
        new_thr = json.dumps({**json.loads(old_thr), **(threshold or {})}, ensure_ascii=False)
        ver = r["rule_version"] + 1 if threshold else r["rule_version"]
        # enabled=None 表示"不改启停状态"：早期实现写成 1，导致只调阈值就把
        # 已禁用的规则偷偷重新启用。
        new_en = r["enabled"] if enabled is None else (1 if enabled else 0)
        self.store.execute("UPDATE mrc_rule SET threshold_json=?, enabled=?, rule_version=? "
                           "WHERE code=?",
                           (new_thr, new_en, ver, code))

        def undo():
            self.store.execute("UPDATE mrc_rule SET threshold_json=?, enabled=?, "
                               "rule_version=? WHERE code=?",
                               (old_thr, old_en, r["rule_version"], code))

        self._log("set_rule", {"code": code, "threshold": threshold},
                  (undo, ()), label=f"修改规则 {code}（新版本 v{ver}）")
        return {"rule_version": ver}

    def cmd_add_material(self, name: str, role: str, recipe: dict | None = None,
                         batch: str = "", price_cny_ml: float = 0.0) -> dict:
        """新增材料（SLC-013：新增后可用于切片）。role: conductor/dielectric/protect。"""
        if role not in ("conductor", "dielectric", "protect"):
            raise CmdError(f"材料角色 {role} 不合法（conductor/dielectric/protect）")
        if not name.strip():
            raise CmdError("材料名称不能为空")
        mid = f"MT-{new_id()[:8]}"
        self.store.execute(
            "INSERT INTO material_profile(id,name,role,recipe_json,batch,arrived_at,"
            "price_cny_ml,enabled) VALUES (?,?,?,?,?,?,?,1)",
            (mid, name.strip(), role,
             json.dumps(recipe or {}, ensure_ascii=False), batch, now_iso(),
             price_cny_ml))

        def undo():
            self.store.execute("DELETE FROM material_profile WHERE id=?", (mid,))

        def redo():
            self.store.execute(
                "INSERT INTO material_profile(id,name,role,recipe_json,batch,arrived_at,"
                "price_cny_ml,enabled) VALUES (?,?,?,?,?,?,?,1)",
                (mid, name.strip(), role,
                 json.dumps(recipe or {}, ensure_ascii=False), batch, now_iso(),
                 price_cny_ml))

        self._log("add_material", {"id": mid, "name": name, "role": role},
                  (undo, ()), (redo, ()), label=f"新增材料 {name}")
        return {"id": mid}

    def cmd_set_material_enabled(self, material_id: str, enabled: bool) -> dict:
        """启用/停用材料（SLC-013：停用材料不再出现在切片选择列表；
        若正被工艺参数引用，同时拒绝停用——停用会导致切片材料缺失）。"""
        m = self.store.one("SELECT * FROM material_profile WHERE id=?", (material_id,))
        if not m:
            raise CmdError(f"材料 {material_id} 不存在")
        p = self.params()
        if not enabled and material_id in (p["material_a_id"], p["material_b_id"]):
            raise CmdError(f"材料 {m['name']} 正被当前工艺参数使用，"
                           "请先在工艺参数中更换材料再停用")
        self.store.execute("UPDATE material_profile SET enabled=? WHERE id=?",
                           (1 if enabled else 0, material_id))

        self._log("set_material_enabled", {"id": material_id, "enabled": enabled},
                  (self._setter("material_profile", "enabled", m["enabled"], "id",
                                material_id), ()),
                  (self._setter("material_profile", "enabled",
                                1 if enabled else 0, "id", material_id), ()),
                  label=f"{'启用' if enabled else '停用'}材料 {m['name']}")
        return {"ok": True}

