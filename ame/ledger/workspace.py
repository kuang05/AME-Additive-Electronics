"""工作区与命令总线（PRJ/DAT 域）。

- 命令总线是唯一写入口（AGT-009：人与 Agent 同源）；
- 命令即时落库（自动保存，PRJ-003），会话内可撤销（PRJ-005，撤销不产生版本）；
- commit() 产生版本：记录命令、diff 摘要、前后分数（DAT-002），并使旧切片过期（SLC-010）。
"""
from __future__ import annotations

import json

from .. import canon_json, digest, new_id
from ..db.store import Store, now_iso
from ..geometry import analysis
from ..geometry.stepio import read_step
from ..logging_setup import get_logger
from ..mrc.engine import DEFAULT_RULES, DEFAULT_TOOL, check as mrc_check
from .commands import CommandsMixin

UNDO_LIMIT = 200   # ≥20 步（PRJ-005）
_LOG = get_logger("ledger")


from .errors import CmdError  # noqa: E402  (定义在 errors.py，避免与 commands 循环)


def _route_sort_key(r: dict) -> tuple:
    """走线的内容排序键（不含 uuid）。

    同一份设计在不同工程里重建时 uuid 全变，但内容不变。MRC 问题清单
    与 Agent 的候选修复都按这个顺序处理，才能保证候选条数与优先级可复现。
    末尾用 (网络, 线宽, 层号) 兜底；几何点串只在前面全同时才参与比较，
    避免绝大多数情况下的长列表比较开销。
    """
    return (r["net"], r["layer_index"], round(float(r["width"]), 6),
            r["surface"], tuple(r["pts"]))


def _insert_or_ignore(store, sql: str, params=()) -> None:
    """幂等插入（表已存在同主键时跳过）。

    为什么不用 SQLite 的 INSERT-OR-忽略 语法：那是 SQLite 专有写法，
    PostgreSQL 会直接语法报错。`ON CONFLICT DO NOTHING` 是 SQL 标准，
    SQLite 3.24+ 与 PostgreSQL 9.5+ 都原生支持——一份 SQL 两个后端通用，
    不需要在 `pg_store.sqlish()` 里做字符串改写
    （改写逻辑越少，越不容易在复杂语句上出错）。

    调用方传 `INSERT INTO ... VALUES (...)`，本函数负责补冲突子句。
    """
    s = sql.rstrip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if "on conflict" not in s.lower():
        s += " ON CONFLICT DO NOTHING"
    store.execute(s, params)


def seed_defaults(store: Store, project_id: str):
    """默认规则 / 工具几何 / 材料 / 工艺参数（SLC-011 默认三层配置）。"""
    for r in DEFAULT_RULES:
        _insert_or_ignore(store, "INSERT INTO mrc_rule(code,name,threshold_json,weight,"
                      "rule_version,enabled) VALUES (?,?,?,?,?,?)",
                      (r["code"], r["name"], json.dumps(r["threshold"], ensure_ascii=False),
                       r["weight"], r["rule_version"], r["enabled"]))
    _insert_or_ignore(store, "INSERT INTO tool_geometry(id,name,nozzle_id_mm,tip_od_mm,"
                  "safe_gap_mm,head_envelope_mm,min_corner_r_mm,min_mesa_w_mm,max_aspect) "
                  "VALUES ('TG-STD','标准针头 0.41/0.64',0.41,0.64,0.5,20.0,0.8,1.0,1.0)")
    _insert_or_ignore(store, "INSERT INTO material_profile(id,name,role,recipe_json,batch,"
                  "arrived_at,price_cny_ml,enabled) VALUES (?,?,?,?,?,?,?,1)",
                  ("MT-A-STD", "可焊烧结银浆 A", "conductor",
                   json.dumps({"viscosity_mPa_s": 12000, "cure": "170℃/15min"}),
                   "AG-2026-10", "2026-10-01", 50.0))
    _insert_or_ignore(store, "INSERT INTO material_profile(id,name,role,recipe_json,batch,"
                  "arrived_at,price_cny_ml,enabled) VALUES (?,?,?,?,?,?,?,1)",
                  ("MT-B-STD", "热固化介电环氧 B", "dielectric",
                   json.dumps({"viscosity_mPaS": 8000, "filler": "气相二氧化硅"}),
                   "EP-2026-10", "2026-10-01", 8.0))
    _insert_or_ignore(store, "INSERT INTO process_params(id,material_a_id,material_b_id,"
                  "layer_height_mm,bottom_layer_mm,bottom_passes,line_width_comp,"
                  "z_comp_mm,speed_mm_s,flow_factor,"
                  "oven_temp_c,pregel_temp_c,set_temp_c,cure_temp_c,cure_min,ramp_c_min,"
                  "final_cure_min,cool_to_c,silver_two_fills,cure_mode) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("PP-DEFAULT", "MT-A-STD", "MT-B-STD", 0.10, 0.08, 3, 0.0, 0.0,
                   20.0, 1.0, 170.0, 80.0, 100.0, 170.0, 7.0, 3.0, 15.0, 60.0, 0,
                   "layer"))


class Workspace(CommandsMixin):
    def __init__(self, store: Store, project_id: str):
        self.store = store
        self.project_id = project_id
        if not store.one("SELECT id FROM project WHERE id=?", (project_id,)):
            raise CmdError(f"工程 {project_id} 不存在")
        self.undo_stack: list[tuple] = []   # (undo_cmd, undo_args, redo_cmd, redo_args, label)
        self.redo_stack: list[tuple] = []
        self.op_log: list[dict] = []
        self.session_id = new_id()
        # 视图缓存：STEP 解析与状态视图重建开销随模型规模线性增长
        # （1800 面模型约 1.2 s），画布每次重绘都调用，必须缓存。
        # 一切写路径（命令/撤销/重做）都经过 _log 或 invalidate() 失效。
        self._step_model_cache = None
        self._mrc_state_cache = None
        self._mrc_state_version: tuple | None = None
        head = self.project["head_commit_id"]
        store.execute("INSERT INTO session(id,project_id,base_commit_id,mode,status,opened_at)"
                      " VALUES (?,?,?,?, 'open', ?)",
                      (self.session_id, project_id, head, "edit", now_iso()))

    def invalidate(self) -> None:
        """清空只读视图缓存（撤销/重做等未走命令总线的写路径必须调用）。"""
        self._step_model_cache = None
        self._mrc_state_cache = None
        self._mrc_state_version = None

    @property
    def project(self) -> dict:
        """project 行实时读取：commit 后 head_commit_id 变化必须立即可见。"""
        row = self.store.one("SELECT * FROM project WHERE id=?", (self.project_id,))
        if not row:
            raise CmdError(f"工程 {self.project_id} 不存在")
        return row

    # ================================================== 视图（只读）
    def rules(self) -> list[dict]:
        rows = self.store.query("SELECT * FROM mrc_rule ORDER BY code")
        for r in rows:
            try:
                thr = json.loads(r["threshold_json"])
                r["threshold"] = thr if isinstance(thr, dict) else {}
            except (json.JSONDecodeError, TypeError):
                # 阈值 JSON 被外部写坏时按空阈值处理，由引擎回退默认值（NFR-002）
                r["threshold"] = {}
        return rows

    def tool(self) -> dict:
        row = self.store.one("SELECT * FROM tool_geometry LIMIT 1")
        return {**DEFAULT_TOOL, **(row or {})}

    def structure(self) -> dict | None:
        return self.store.one("SELECT * FROM structure WHERE project_id=? LIMIT 1",
                              (self.project_id,))

    def integrity_check(self) -> list[dict]:
        """校验工程引用的所有内容寻址文件是否被外部修改（DAT-005）。

        只读、无副作用；返回 [{kind, ref, ok, detail}]。打开工程时调用一次，
        有异常就在界面上提示用户（"STEP 文件已被外部修改"），不静默继续。
        """
        out: list[dict] = []
        s = self.structure()
        if s and s.get("step_hash"):
            ok, detail = self.store.verify_blob(s["step_hash"])
            out.append({"kind": "STEP 结构文件", "ref": s.get("source_step", ""),
                        "ok": ok, "detail": detail})
        c = self.store.one("SELECT * FROM circuit WHERE project_id=? LIMIT 1",
                           (self.project_id,))
        if c and c.get("netlist_hash"):
            ok, detail = self.store.verify_blob(c["netlist_hash"])
            out.append({"kind": "板文件", "ref": c.get("netlist_source") or "",
                        "ok": ok, "detail": detail})
        for row in self.store.query(
                "SELECT id, created_at, gcode_blob FROM slice_set "
                "WHERE project_id=? AND gcode_blob IS NOT NULL AND stale=0 "
                f"ORDER BY {self.store.recent_order()} LIMIT 3", (self.project_id,)):
            ok, detail = self.store.verify_blob(row["gcode_blob"])
            out.append({"kind": "切片 G-code", "ref": row["created_at"],
                        "ok": ok, "detail": detail})
        return out

    def step_model(self):
        s = self.structure()
        if not s:
            return None
        if self._step_model_cache is not None:
            return self._step_model_cache          # 命中缓存（解析一次复用）
        # DAT-005：外部改动过 STEP 文件时明确报错，而不是拿被改过的几何继续算
        ok, detail = self.store.verify_blob(s["step_hash"])
        if not ok:
            from ..db.store import IntegrityError
            raise IntegrityError(
                f"结构文件完整性校验失败（DAT-005）：{detail}。"
                "该文件可能在软件外部被修改或删除；请重新导入 STEP，"
                "或在“文件 → 校验工程完整性”中确认后继续。")
        text = self.store.get_blob(s["step_hash"]).decode("utf-8", errors="replace")
        # STR-008：容差从 structure.tess_tol 读取（可调），不再写死 0.02
        tol = float(s.get("tess_tol") or 0.02)
        m = read_step(text, tess_tol=tol)
        self._step_model_cache = m
        return m


    def board_outline(self):
        s = self.structure()
        if s and s["outline_json"]:
            return json.loads(s["outline_json"]), "手工/识别板框"
        m = self.step_model() if s else None
        if m:
            return analysis.find_board_outline(m)
        # 无结构时回退到板文件的 Edge.Cuts 板框（CIR-001：板文件含板框）
        row = self.store.one(
            "SELECT outline_json FROM board_outline WHERE project_id=?",
            (self.project_id,)) if self._has_board_outline_table() else None
        if row and row["outline_json"]:
            return json.loads(row["outline_json"]), "板框取自板文件 Edge.Cuts"
        return None, "无结构，未定义板框"

    def board_holes(self) -> list[list[tuple[float, float]]]:
        """板框内孔（STR-004/SLC-004）。结构优先，其次板文件。"""
        s = self.structure()
        if s:
            if s.get("holes_json"):
                try:
                    holes = json.loads(s["holes_json"])
                    if holes:
                        return holes
                except (json.JSONDecodeError, TypeError):
                    pass
            m = self.step_model()
            if m:
                return analysis.board_holes(m)
        if self._has_board_outline_table():
            row = self.store.one(
                "SELECT holes_json FROM board_outline WHERE project_id=?",
                (self.project_id,))
            if row and row.get("holes_json"):
                try:
                    return json.loads(row["holes_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return []

    def _has_board_outline_table(self) -> bool:
        return self.store.has_table("board_outline")

    def comps(self) -> list[dict]: return self.store.query(
        "SELECT c.* FROM comp c JOIN circuit ci ON c.circuit_id=ci.id "
        "WHERE ci.project_id=?", (self.project_id,))
    def pads(self) -> list[dict]: return self.store.query(
        "SELECT p.* FROM pad p JOIN comp c ON p.comp_id=c.id "
        "JOIN circuit ci ON c.circuit_id=ci.id WHERE ci.project_id=?", (self.project_id,))

    def pad_state(self) -> list[dict]:
        """焊盘几何视图：DB 行 → 连通性/MRC/切片共用的字典（CIR/MRC/SLC）。"""
        rows = self.store.query(
            "SELECT p.id, c.ref, p.pad_no, n.name AS net, p.x_mm, p.y_mm, "
            "p.w_mm, p.h_mm, p.shape "
            "FROM pad p JOIN comp c ON p.comp_id=c.id "
            "JOIN circuit ci ON c.circuit_id=ci.id "
            "LEFT JOIN net n ON p.net_id=n.id WHERE ci.project_id=?", (self.project_id,))
        return [{"id": r["id"], "ref": r["ref"], "pad_no": r["pad_no"],
                 "net": r["net"], "x": r["x_mm"], "y": r["y_mm"],
                 "w": r["w_mm"], "h": r["h_mm"], "shape": r["shape"], "rot": 0.0}
                for r in rows]

    def comp_state(self) -> list[dict]:
        rows = self.comps()
        return [{"id": r["id"], "ref": r["ref"], "x": r["x_mm"], "y": r["y_mm"],
                 "height": r["height_mm"], "surface": r["surface"],
                 "size_dx": 3.0, "size_dy": 3.0, "locked": r["locked"]}
                for r in rows]
    def nets(self) -> list[dict]: return self.store.query(
        "SELECT n.* FROM net n JOIN circuit ci ON n.circuit_id=ci.id "
        "WHERE ci.project_id=?", (self.project_id,))
    def routes(self) -> list[dict]: return self.store.query(
        "SELECT r.* FROM route r JOIN circuit ci ON r.circuit_id=ci.id "
        "WHERE ci.project_id=? ORDER BY r.id", (self.project_id,))
    def jumpers(self) -> list[dict]: return self.store.query(
        "SELECT j.* FROM jumper j JOIN net n ON j.net_id=n.id "
        "JOIN circuit ci ON n.circuit_id=ci.id WHERE ci.project_id=?", (self.project_id,))

    def keepouts(self) -> list[dict]:
        """禁布区列表（CIR-006）：[{"id","name","pts","note"}]。"""
        out = []
        for r in self.store.query(
                "SELECT id, name, pts_json, note FROM keepout WHERE project_id=? "
                "ORDER BY name", (self.project_id,)):
            try:
                pts = json.loads(r["pts_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            out.append({"id": r["id"], "name": r["name"], "pts": pts,
                        "note": r["note"]})
        return out

    def features(self) -> list[dict]: return self.store.query(
        "SELECT f.* FROM feature f JOIN structure s ON f.structure_id=s.id "
        "WHERE s.project_id=?", (self.project_id,))
    def params(self) -> dict: return self.store.one("SELECT * FROM process_params LIMIT 1")
    def materials(self, enabled_only=False):
        sql = "SELECT * FROM material_profile"
        if enabled_only:
            sql += " WHERE enabled=1"
        return self.store.query(sql + " ORDER BY role,name")

    def _db_version(self) -> tuple:
        """数据库变更指纹（委托给存储实现，兼容 SQLite / PostgreSQL）。

        早期这里直接读 `PRAGMA data_version` 与 `conn.total_changes`，
        在 PostgreSQL 下两者都不存在（psycopg 的 Connection 没有
        total_changes 属性，PRAGMA 是语法错误）——云上任何走缓存的请求都会崩。
        现在由 `Storage.change_token()` 各自实现。
        """
        return self.store.change_token()

    def mrc_state(self) -> dict:
        """MRC / 切片共用的纯数据视图（缓存：写路径失效 + 外部写入自动检测）。"""
        if self._mrc_state_cache is not None:
            v = self._db_version()
            if v == self._mrc_state_version:
                return self._mrc_state_cache
            self.invalidate()          # 数据库被外部改动过，缓存作废
        self._mrc_state_version = self._db_version()
        m = self.step_model()
        faces = []
        obstacles = []
        outline = None
        if m:
            r3 = next((r for r in self.rules() if r["code"] == "R3"), None)
            thr = r3["threshold"]["max_slope_deg"] if r3 else 45.0
            # 坡度只有一处算法：复用 analyze_faces 的 slope_deg（避免与引擎各算一遍而发散）
            faces = [{"fid": f.fid, "slope_deg": round(f.slope_deg, 1),
                      "area": round(f.area, 2)}
                     for f in analysis.analyze_faces(m, thr)]
            outline, _ = self.board_outline()
            if outline:
                # 凸台/台阶识别（MRC-004/005 的遮挡物来源）：
                # 不能用"高于整个模型最高点"判定（那样永远为空，因为最高点就在凸台上）。
                # 正确做法：找面积最大的水平朝上面作为**基准台面**，其余高于台面的
                # 朝上水平面即为凸台/台阶，作为可达性与遮挡的障碍物。
                horiz = [f for f in m.faces
                         if f.surf == "plane" and abs(f.normal[2]) > 0.98
                         and len(f.loop) >= 3]
                if horiz:
                    base = max(horiz, key=lambda f: f.area)
                    base_z = max(p[2] for p in base.loop)
                    for f in horiz:
                        if f is base or f.normal[2] <= 0:
                            continue
                        fz = max(p[2] for p in f.loop)
                        if fz <= base_z + 0.05 or f.area < 1.0:
                            continue
                        xs = [p[0] for p in f.loop]
                        ys = [p[1] for p in f.loop]
                        # 防御：平铺到整个板框的顶面不是凸台
                        if (max(xs) - min(xs)) > 0.9 * (max(p[0] for p in outline)
                                                        - min(p[0] for p in outline)) \
                                and (max(ys) - min(ys)) > 0.9 * (
                                    max(p[1] for p in outline)
                                    - min(p[1] for p in outline)):
                            continue
                        obstacles.append({"x0": min(xs), "y0": min(ys),
                                          "x1": max(xs), "y1": max(ys),
                                          "height": fz,
                                          "name": f"结构凸台 {f.fid}"})
        net_names = {r["id"]: r["name"] for r in self.nets()}
        state = {
            "outline": outline or [(0, 0), (100, 0), (100, 100), (0, 100)],
            "holes": self.board_holes(),
            "faces": faces, "obstacles": obstacles,
            "comps": self.comp_state(), "pads": self.pad_state(),
            # 网络名一次查全：原实现每条走线/跳线各查一次 net（N+1），
            # SQL 次数随走线数线性增长，冷启动视图重建明显变慢。
            # 按**内容**稳定排序，不用 uuid 主键顺序。
            #
            # `routes()` 是 `ORDER BY r.id`，而 id 是每次导入重新生成的 uuid：
            # 同一份设计重建一次，行的先后就变了。这对"结果"没有影响，
            # 但会顺流污染下游——MRC 问题清单的先后、以及 Agent 的
            # `by_net`/`seen_routes` 去重选择都会跟着变，最终导致
            # **候选修复方案的条数在 6 与 7 之间抖动**（实测 selftest 连续
            # 两次跑出不同数字）。按（网络名, 线宽, 层号, 几何）排序后，
            # 同一设计在任何工程里都得到同一行序。
            "routes": sorted(
                ({"id": r["id"],
                  "net": (net_names.get(r["net_id"]) or str(r["net_id"])),
                  "pts": json.loads(r["pts_json"]), "width": r["width_mm"],
                  "surface": r["surface"], "layer_index": r["layer_index"]}
                 for r in self.routes()),
                key=_route_sort_key),
            "jumpers": [{"id": j["id"], "net": (net_names.get(j["net_id"]) or str(j["net_id"])),
                         "from_pad": j["from_pad_id"], "to_pad": j["to_pad_id"]}
                        for j in self.jumpers()],
            "printed_layers": self.slice_pass_geometry(),
            "bed": [self.project["bed_x_mm"], self.project["bed_y_mm"]],
        }
        self._mrc_state_cache = state
        return state

    def slice_pass_geometry(self) -> list[dict]:
        """最近一份有效切片的道次几何（MRC-005「已打印层」障碍来源）。

        切片道次的 ``z_mm`` 是材料**顶面**，``h_mm`` 是厚度。
        没有切片时返回空列表——此时谈不上"已打印层"。
        """
        row = self.store.one(
            "SELECT id FROM slice_set WHERE project_id=? AND stale=0 "
            f"ORDER BY {self.store.recent_order()} LIMIT 1", (self.project_id,))
        if not row:
            return []
        sid = row["id"]
        # layer.z_mm 存的是该道次材料的**顶面**（切片器按累加厚度算），
        # kind 给出物理层归属；path 挂在 layer 下按 seq 排列。
        # 注意：h_mm 未入库（顶面 + pass_no 已足够判"哪层在先、顶面多高"）。
        out: dict[int, dict] = {}
        for lay in self.store.query(
                "SELECT l.id, l.pass_no, l.z_mm, l.h_mm, l.kind, "
                "       m.role AS mat_role "
                "FROM layer l LEFT JOIN material_profile m ON l.material_id=m.id "
                "WHERE l.slice_set_id=? ORDER BY l.pass_no", (sid,)):
            paths = []
            for p in self.store.query(
                    "SELECT pts_json, width_mm FROM path WHERE layer_id=? ORDER BY seq",
                    (lay["id"],)):
                try:
                    pts = json.loads(p["pts_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                paths.append({"pts": pts, "width_mm": p["width_mm"]})
            slot = out.setdefault(lay["pass_no"], {
                "pass_no": lay["pass_no"],
                "kind": lay.get("kind") or "",
                "mat": (lay.get("mat_role") or "").upper(),
                "z_mm": lay["z_mm"], "h_mm": lay.get("h_mm") or 0.0,
                "paths": []})
            slot["paths"].extend(paths)
        return list(out.values())

    def _net_name(self, net_id) -> str:
        row = self.store.one("SELECT name FROM net WHERE id=?", (net_id,))
        return row["name"] if row else str(net_id)

    def run_mrc(self, rules=None) -> dict:
        return mrc_check(self.mrc_state(), rules or self.rules(), self.tool())

    def design_digest(self) -> str:
        """设计内容摘要：结构+电路+参数（重放一致性的锚点）。

        只摘要**设计内容**，不摘要主键与行序：

        - 主键（uuid）在"新建对象"时每次生成，同一份设计重跑一次必然不同。
          若把 id 算进摘要，Agent 新建走线的方案就永远无法重放一致
          （实测：重放后走线数量、网络、线宽全对，唯独 id 不同 → 判定不一致）。
        - ``routes()`` 以 ``ORDER BY id`` 返回，行序同样随 uuid 变化。
          因此这里按内容排序后再摘要。

        同一份设计内容（几何、网络、参数）无论怎样重建，都应得到同一摘要——
        这正是 G-code 头 ``design_sha256`` 可复现的前提（SLC-008）。
        """
        net_names = {r["id"]: r["name"] for r in self.nets()}
        comp_refs = {r["id"]: r["ref"] for r in self.comps()}

        def _stable_rows(rows, circuit_key: str | None = None,
                         comp_key: bool = False):
            """把 uuid 主键/外键换成稳定的语义标识，再按内容排序。

            换掉的字段（都会随重建而变，且不承载设计语义）：
              id / circuit_id / net_id / comp_id / from_pad_id / to_pad_id /
              pad_id / structure_id / project_id / slice_set_id
            换上的稳定标识：
              net_id → net（网名）、comp_id → comp（器件位号）
            剥掉外键会丢信息：两个不同器件的焊盘若坐标相同、剥掉 comp_id 后
            在摘要里就分不出来（排序键相同，顺序可能互换）。用位号替代既
            稳定又保留语义。
            """
            uuid_keys = {"id", "circuit_id", "net_id", "comp_id", "from_pad_id",
                         "to_pad_id", "pad_id", "structure_id", "project_id",
                         "slice_set_id"}
            if circuit_key:
                uuid_keys.add(circuit_key)
            out = []
            for r in rows:
                d = dict(r)
                nid = d.pop("net_id", None)
                if nid is not None:
                    d["net"] = net_names.get(nid, str(nid))
                if comp_key:
                    cid = d.pop("comp_id", None)
                    if cid is not None:
                        d["comp"] = comp_refs.get(cid, str(cid))
                d = {k: v for k, v in d.items() if k not in uuid_keys}
                out.append(d)
            return sorted(out, key=lambda x: canon_json(x))

        routes = _stable_rows(self.routes())
        pads = _stable_rows(self.pads(), comp_key=True)
        comps = _stable_rows(self.comps(), circuit_key="circuit_id")
        jumpers = _stable_rows(self.jumpers())
        features = _stable_rows(self.features())
        return digest({"structure": (self.structure() or {}).get("step_hash"),
                       "outline": self.board_outline()[0],
                       "routes": routes, "pads": pads,
                       "comps": comps, "jumpers": jumpers,
                       "features": features})

    # ================================================== 命令总线
    # ---- 撤销/重做回调构造器（消除各处重复的闭包定义） ----
    # 允许通过 _setter/_setter_multi/_inserter 写入的表与列（NFR-005 防御）。
    # 这些辅助函数用 f-string 拼表名/列名，虽然当前调用点都是硬编码常量，
    # 但一旦将来有人把外部输入传进来就会变成 SQL 注入。用白名单卡死这个面。
    _WRITABLE = {
        "comp": {"x_mm", "y_mm", "rot_deg", "locked", "height_mm", "surface",
                 "footprint", "ref"},
        "pad": {"x_mm", "y_mm", "w_mm", "h_mm", "net_id", "pad_no", "shape"},
        "route": {"width_mm", "pts_json", "surface", "strategy", "layer_index"},
        "net": {"klass", "name", "pad_count"},
        "structure": {"outline_json", "holes_json", "tess_tol", "bbox_json",
                      "diag_json", "step_hash", "unit"},
        "circuit": {"netlist_hash", "netlist_source", "format"},
        "feature": {"enabled", "status", "dims_json", "transform_json", "note"},
        "process_params": {"layer_height_mm", "bottom_layer_mm", "bottom_passes",
                           "line_width_comp", "z_comp_mm", "speed_mm_s",
                           "extrusion_factor", "pregel_temp_c", "set_temp_c",
                           "cure_temp_c", "pregel_s", "set_s", "cure_s",
                           "ramp_c_per_min", "cool_temp_c", "cure_mode",
                           "material_a_id", "material_b_id", "silver_two_fills"},
        "slice_set": {"stale", "gcode_hash", "gcode_blob"},
        "board_outline": {"outline_json", "holes_json", "source"},
        "keepout": {"name", "pts_json", "note"},
        "mrc_rule": {"threshold_json", "enabled", "rule_version"},
        "material_profile": {"name", "role", "recipe_json", "batch", "arrived_at",
                             "price_cny_ml", "enabled"},
        "device": {"name", "protocol", "fw_version", "calib_json", "online",
                   "bed_x_mm", "bed_y_mm"},
        "print_job": {"state", "layer_cursor", "safe_point_json", "finished_at"},
        "defect": {"status", "note"},
    }

    @classmethod
    def _check_write(cls, table: str, columns):
        """校验表名与列名在允许集合内；不在则抛 CmdError（不静默）。"""
        if table not in cls._WRITABLE:
            raise CmdError(f"内部错误：不允许写表 {table!r}（NFR-005 白名单）")
        bad = [c for c in columns if c not in cls._WRITABLE[table]]
        if bad:
            raise CmdError(f"内部错误：表 {table} 不允许写列 {bad[:3]}（NFR-005 白名单）")

    def _setter(self, table: str, column: str, value, pk: str, pk_val):
        """生成"把 table.column 设为 value（按 pk 定位）"的回调。"""
        self._check_write(table, [column])

        def _apply():
            self.store.execute(f"UPDATE {table} SET {column}=? WHERE {pk}=?",
                               (value, pk_val))
        return _apply

    def _setter_multi(self, table: str, values: dict, pk: str, pk_val):
        """多列同时赋值的回调。"""
        cols = list(values)
        self._check_write(table, cols)
        vals = [values[c] for c in cols]

        def _apply():
            sets = ", ".join(f"{c}=?" for c in cols)
            self.store.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?",
                               (*vals, pk_val))
        return _apply

    def _inserter(self, table: str, values: dict):
        """按字典插入一行的回调。"""
        cols = list(values)
        self._check_write(table, cols)
        vals = [values[c] for c in cols]

        def _apply():
            ph = ", ".join("?" for _ in cols)
            self.store.execute(
                f"INSERT INTO {table}({','.join(cols)}) VALUES({ph})", tuple(vals))
        return _apply

    def _log(self, cmd, args, undo=None, redo=None, label=""):
        self.op_log.append({"cmd": cmd, "args": args, "t": now_iso()})
        # NFR-006：所有写命令都汇聚到这里，是"关键路径日志"最可靠的埋点
        _LOG.info("命令 %s project=%s args=%s", cmd, self.project_id[:8],
                  {k: (v if not isinstance(v, (list, dict)) else type(v).__name__)
                   for k, v in (args or {}).items()})
        self.invalidate()          # 任何写命令后视图缓存失效
        if undo:
            # redo 未显式提供时记为不可重做（None），不能把命令名字符串当可调用对象
            if redo:
                rcmd, rargs = redo[0], redo[1]
            else:
                rcmd, rargs = None, ()
            self.undo_stack.append((undo[0], undo[1], rcmd, rargs, label or cmd))
            if len(self.undo_stack) > UNDO_LIMIT:
                self.undo_stack.pop(0)
            self.redo_stack.clear()

    def undo(self) -> str:
        if not self.undo_stack:
            return "无可撤销操作"
        if self.undo_stack[-1][0] is None:
            return f"“{self.undo_stack[-1][4]}”不可撤销（如规则版本变更请用新命令改回）"
        ucmd, uargs, _rc, _ra, label = self.undo_stack.pop()
        ucmd(*uargs)
        # PRJ-005：撤销本身也要记入操作日志（它是对工程的一次真实修改）
        self.op_log.append({"cmd": "undo", "args": {"label": label}, "t": now_iso()})
        self.invalidate()            # 撤销未走命令总线，需手动失效缓存
        self.redo_stack.append((ucmd, uargs, _rc, _ra, label))
        return f"已撤销：{label}"

    def redo(self) -> str:
        if not self.redo_stack:
            return "无可重做操作"
        _uc, _ua, rcmd, rargs, label = self.redo_stack.pop()
        if rcmd is None:
            self.undo_stack.append((_uc, _ua, None, (), label))
            return f"“{label}”不可重做（请在界面中重新执行该操作）"
        rcmd(*rargs)
        self.op_log.append({"cmd": "redo", "args": {"label": label}, "t": now_iso()})
        self.invalidate()            # 重做未走命令总线，需手动失效缓存
        self.undo_stack.append((_uc, _ua, rcmd, rargs, label))
        return f"已重做：{label}"

    # ---------------- 结构域命令

    @staticmethod
    def _validate_outline(pts: list, model_bbox: tuple | None = None) -> list[list[float]]:
        """板框合法性校验（STR-007）：闭合多边形、无 NaN、面积非零、不严重偏离模型。"""
        if not isinstance(pts, (list, tuple)):
            raise CmdError("板框必须是坐标点列表（STR-007）")
        clean: list[list[float]] = []
        for i, p in enumerate(pts):
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                raise CmdError(f"板框第 {i + 1} 个点格式不对，需要 (x, y)（STR-007）")
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError) as e:
                raise CmdError(f"板框第 {i + 1} 个点坐标不是数字（STR-007）") from e
            if x != x or y != y or abs(x) == float("inf") or abs(y) == float("inf"):
                raise CmdError(f"板框第 {i + 1} 个点坐标是 NaN/无穷大（STR-007）")
            if abs(x) > 1e5 or abs(y) > 1e5:
                raise CmdError(f"板框第 {i + 1} 个点超出合理量程（|坐标| ≤ 1e5 mm，STR-007）")
            clean.append([round(x, 4), round(y, 4)])
        if len(clean) >= 2 and clean[0] == clean[-1]:
            clean.pop()                       # 允许闭合点重复，去掉尾点
        if len(clean) < 3:
            raise CmdError(f"板框至少需要 3 个不重合的点，收到 {len(clean)} 个（STR-007）")
        uniq = {tuple(p) for p in clean}
        if len(uniq) < 3:
            raise CmdError("板框退化：有效顶点少于 3 个（STR-007）")
        # 面积必须非零（退化多边形不能作为板框）
        area = 0.0
        for i in range(len(clean)):
            x1, y1 = clean[i]
            x2, y2 = clean[(i + 1) % len(clean)]
            area += x1 * y2 - x2 * y1
        if abs(area) / 2 < 1.0:
            raise CmdError(f"板框面积过小（{abs(area) / 2:.3f} mm²），请检查顶点顺序"
                           "（STR-007）")
        # 与模型包围盒的一致性：完全不重叠多半是坐标单位/原点填错 —— 记为警告，
        # 不硬性拒绝（板框有权超出模型范围，例如预留工艺边框）
        warning = ""
        if model_bbox:
            x0, y0, _z0, x1, y1, _z1 = model_bbox
            ox0 = min(p[0] for p in clean)
            oy0 = min(p[1] for p in clean)
            ox1 = max(p[0] for p in clean)
            oy1 = max(p[1] for p in clean)
            if ox1 < x0 - 0.5 or ox0 > x1 + 0.5 or oy1 < y0 - 0.5 or oy0 > y1 + 0.5:
                warning = (f"板框 ({ox0:.1f},{oy0:.1f})-({ox1:.1f},{oy1:.1f}) 与模型包围盒 "
                           f"({x0:.1f},{y0:.1f})-({x1:.1f},{y1:.1f}) 不重叠，"
                           "请确认坐标原点/单位是否正确（STR-007）")
        return clean, warning


    # ---------------- 走线域命令














    # ---------------- 特征/参数/规则命令





    # 工艺参数的合法范围（SLC-003/SLC-012：参数越界必须拒绝并说明）
    PARAM_RANGES = {
        "layer_height_mm": (0.02, 0.5, "层高"),
        "bottom_layer_mm": (0.02, 0.5, "下保护单层厚"),
        "bottom_passes": (1, 10, "下保护道次数"),
        "line_width_comp": (-1.0, 1.0, "线宽补偿"),
        "z_comp_mm": (-1.0, 1.0, "Z 补偿"),
        "speed_mm_s": (0.5, 500.0, "打印速度"),
        "flow_factor": (0.1, 5.0, "出料系数"),
        "oven_temp_c": (20.0, 300.0, "烘箱温度"),
        "pregel_temp_c": (20.0, 250.0, "预凝胶温度"),
        "set_temp_c": (20.0, 250.0, "定型温度"),
        "cure_temp_c": (20.0, 300.0, "固化温度"),
        "cure_min": (0.1, 600.0, "层固化时间"),
        "ramp_c_min": (0.1, 30.0, "升温速率"),
        "final_cure_min": (0.1, 600.0, "终固化时间"),
        "cool_to_c": (0.0, 100.0, "冷却温度"),
    }
    PARAM_INTS = {"silver_two_fills", "bottom_passes"}
    PARAM_ENUMS = {"cure_mode": ("pass", "layer")}
    PARAM_REFS = {"material_a_id", "material_b_id"}

    def _validate_params(self, kv: dict) -> dict:
        """校验并规整参数（类型 + 范围 + 枚举 + 外键存在性）。"""
        p = self.params()
        out = {}
        for k, v in kv.items():
            if k in self.PARAM_RANGES:
                lo, hi, label = self.PARAM_RANGES[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise CmdError(f"{label}（{k}）必须是数字，收到 {v!r}（SLC-012）")
                if v != v or v in (float("inf"), float("-inf")):
                    raise CmdError(f"{label}（{k}）不能是 NaN 或无穷大（SLC-012）")
                if not (lo <= float(v) <= hi):
                    raise CmdError(f"{label} {v} 越界（允许 {lo}–{hi}，SLC-012）")
                out[k] = float(v)
            elif k in self.PARAM_INTS:
                try:
                    iv = int(v)
                except (TypeError, ValueError) as e:
                    raise CmdError(f"{k} 必须是整数 0/1（SLC-012）") from e
                if iv not in (0, 1):
                    raise CmdError(f"{k} 只能是 0 或 1（SLC-012）")
                out[k] = iv
            elif k in self.PARAM_ENUMS:
                if v not in self.PARAM_ENUMS[k]:
                    raise CmdError(
                        f"{k} 只能是 {'/'.join(self.PARAM_ENUMS[k])}，收到 {v!r}（SLC-012）")
                out[k] = v
            elif k in self.PARAM_REFS:
                if not isinstance(v, str) or not self.store.one(
                        "SELECT id FROM material_profile WHERE id=?", (v,)):
                    raise CmdError(f"材料 {v!r} 不存在（SLC-013）")
                out[k] = v
            elif k in p or k == "id":
                out[k] = v          # 未知列留给调用方统一报"无有效参数"
            else:
                out[k] = v
        # 跨字段约束：预凝胶 ≤ 定型 ≤ 固化
        merged = {**p, **out}
        if merged["pregel_temp_c"] > merged["set_temp_c"] or \
                merged["set_temp_c"] > merged["cure_temp_c"]:
            raise CmdError("温度曲线不合法：预凝胶 ≤ 定型 ≤ 层固化（SLC-012）")
        return out


    # 规则阈值字段的合法范围（MRC-011：阈值可配置，但必须落在物理合理区间）
    RULE_THRESHOLD_RANGES = {
        "R1": {"min_width": (0.05, 10.0, "线宽下限")},
        "R2": {"min_clear": (0.05, 10.0, "间距下限")},
        "R3": {"max_slope_deg": (0.0, 89.0, "坡度上限")},
        "R4": {"min_gap": (0.0, 10.0, "最小间隙")},
        "R5": {},
        "R6": {"max_dt": (0.0, 200.0, "允许温差"),
               "gradient_c_per_mm": (0.0, 10.0, "温度梯度")},
        "R7": {"min_channel": (0.05, 10.0, "流道下限"),
               "dam_w": (0.05, 10.0, "介电坝宽")},
    }

    def _validate_rule_threshold(self, code: str, threshold: dict) -> dict:
        if not isinstance(threshold, dict):
            raise CmdError(f"规则 {code} 的阈值必须是键值表，收到 {type(threshold).__name__}")
        allowed = self.RULE_THRESHOLD_RANGES.get(code, {})
        out = {}
        for k, v in threshold.items():
            if k not in allowed:
                raise CmdError(f"规则 {code} 没有阈值字段 {k}（可用："
                               f"{', '.join(allowed) or '无'}，MRC-011）")
            lo, hi, label = allowed[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise CmdError(f"{code}.{k}（{label}）必须是数字，收到 {v!r}（MRC-011）")
            if v != v or v in (float("inf"), float("-inf")):
                raise CmdError(f"{code}.{k}（{label}）不能是 NaN 或无穷大（MRC-011）")
            if not (lo <= float(v) <= hi):
                raise CmdError(f"{code}.{k}（{label}）{v} 越界（允许 {lo}–{hi}，MRC-011）")
            out[k] = float(v)
        return out


    # ---------------- 材料库（SLC-013，P1）


    # ================================================== 版本账本
    def commit(self, reason: str, author="user") -> dict:
        """提交版本：评分前后、命令、diff；旧切片过期（DAT-002/SLC-010）。"""
        last = self.store.one("SELECT * FROM version WHERE project_id=? "
                              "ORDER BY seq DESC LIMIT 1", (self.project_id,))
        seq = (last["seq"] + 1) if last else 0
        mrc = self.run_mrc() if (self.routes() or self.structure()) else None
        score = mrc["score"] if mrc else None
        diff = self._diff_summary(last)
        cid = new_id()
        with self.store.batch():
            self.store.execute(
                "INSERT INTO version(id,project_id,parent_id,seq,cmd_json,diff_json,reason,"
                "score_before,score_after,author,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid, self.project_id, last["id"] if last else None, seq,
                 json.dumps(self.op_log, ensure_ascii=False),
                 json.dumps(diff, ensure_ascii=False), reason,
                 last["score_after"] if last else None, score, author, now_iso()))
            self.store.execute("UPDATE project SET head_commit_id=? WHERE id=?",
                               (cid, self.project_id))
            self.store.execute("UPDATE slice_set SET stale=1 WHERE project_id=? AND "
                               "source_commit_id<>?", (self.project_id, cid))
            # DAT-001：问题也要持久化。mrc_issue 表早就建好，但从未写过——
            # 重开工程后"这个版本当时有哪些问题"就查不到了。
            # 迁移来的旧库可能没有 mrc_rule 种子（mrc_issue.rule_code 有外键），
            # 这里先补齐再插入，否则 commit 会因外键失败。
            if mrc and mrc["issues"]:
                for r in DEFAULT_RULES:
                    _insert_or_ignore(self.store, "INSERT INTO mrc_rule(code,name,threshold_json,weight,"
                        "rule_version,enabled) VALUES (?,?,?,?,?,?)",
                        (r["code"], r["name"],
                         json.dumps(r["threshold"], ensure_ascii=False),
                         r["weight"], r["rule_version"], r["enabled"]))
                for it in mrc["issues"]:
                    self.store.execute(
                        "INSERT INTO mrc_issue(id,rule_code,target_type,target_id,"
                        "severity,penalty,reason,suggestion,commit_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (new_id(), it["rule_code"], it["target_type"],
                         str(it["target_id"]), int(it["severity"]),
                         int(round(it["penalty"])), it["reason"],
                         it.get("suggestion", ""), cid))
        self.op_log = []
        _LOG.info("提交版本 seq=%d score=%s 问题=%d reason=%s", seq, score,
                  len(mrc["issues"]) if mrc else 0, reason)
        return {"id": cid, "seq": seq, "score_after": score, "diff": diff,
                "issue_count": len(mrc["issues"]) if mrc else 0}

    def issues_of_commit(self, commit_id: str) -> list[dict]:
        """某版本当时的问题列表（DAT-001：问题随版本留存，可追溯）。"""
        return self.store.query(
            "SELECT * FROM mrc_issue WHERE commit_id=? ORDER BY severity DESC, rule_code",
            (commit_id,))

    def _diff_summary(self, last) -> dict:
        """版本间 diff 摘要（PRJ-004：能看出改了哪些对象、分数变化多少）。

        返回值**包含** ``design``（设计摘要）：这是版本账本的关键信息——
        有了它才能回答"这一版和上一版的设计内容是否相同"，进而实现
        "设计没变就不必新建版本"（云端切片依赖这个判断，否则同一设计
        每次切片都会因 source_commit 变化而得到不同 G-code 哈希）。
        早期版本只拿它算 changed、返回时剔除，等于信息写丢了。
        """
        cur = {"routes": len(self.routes()), "comps": len(self.comps()),
               "pads": len(self.pads()), "jumpers": len(self.jumpers()),
               "features": len(self.features()),
               "design": self.design_digest()}
        if last:
            prev = json.loads(last["diff_json"])
        else:
            prev = {}
        changed = [k for k in cur if k != "design" and cur[k] != prev.get(k)]
        if prev.get("design") != cur["design"]:
            changed.append("design")
        return {**cur, "changed": changed, "score_delta": None}

    def history(self) -> list[dict]:
        return self.store.query("SELECT id,seq,author,reason,score_before,score_after,"
                                "created_at,diff_json FROM version WHERE project_id=? "
                                "ORDER BY seq", (self.project_id,))

    def close(self) -> None:
        self.store.execute("UPDATE session SET status='closed', closed_at=? WHERE id=?",
                           (now_iso(), self.session_id))


# ---------------------------------------------------------------- 示例工程
def build_sample_project(root, name="T19-A 示例板") -> Workspace:
    """创建可直接演示的示例工程（NFR-003：新用户几分钟内看到完整链路）。

    内容：导入平板 STEP + T19-A 板文件（含 8 条违规走线），提交基线并切片一次。
    样例文件随安装包分发；缺失时给出明确提示而不是静默失败。
    """
    from ..db.store import resource_path

    data_dir = resource_path("samples/data")
    step = data_dir / "plate_100x60.step"
    board = data_dir / "board_T19A.kicad_pcb"
    missing = [p.name for p in (step, board) if not p.exists()]
    if missing:
        raise CmdError(f"示例数据缺失（{', '.join(missing)}），"
                       f"请确认安装包完整或手动导入自己的工作文件")

    ws = create_project(root, name)
    ws.cmd_import_step(step.read_text("utf-8"), step.name)
    ws.cmd_import_board(board.read_text("utf-8"))
    ws.commit("导入示例板（初始设计）", author="user")
    return ws


# ---------------------------------------------------------------- 工程级
def create_project(root, name: str, unit="mm", bed=(200.0, 200.0)) -> Workspace:
    """新建工程（PRJ-001）：空工程 + 初始版本 + 默认规则/材料/参数。

    存储后端由 ``root`` 决定（本地 SQLite 或云端 PostgreSQL），
    本函数只依赖 `Storage` 协议，不感知具体实现。
    """
    from ..db.store import CURRENT_SCHEMA_VERSION, Store
    pid = new_id()
    if getattr(root, "_store_factory", None) is not None:
        st = root.open_project(pid)          # 注入的后端（如 PG）
    else:
        st = Store(root.data_dir / "projects" / f"{pid}.db")
    st.execute("INSERT INTO project(id,name,schema_version,unit,bed_x_mm,bed_y_mm,"
               "created_at) VALUES (?,?,?,?,?,?,?)",
               (pid, name, CURRENT_SCHEMA_VERSION, unit, bed[0], bed[1], now_iso()))
    seed_defaults(st, pid)
    ws = Workspace(st, pid)
    ws.commit("新建工程", author="system")
    # PG 模式：登记到全局目录表，否则 list_projects 扫不到（工程分散在各自 schema）
    _register_in_catalog(root, pid, name, ws.project.get("head_commit_id"))
    return ws


def _register_in_catalog(root, pid: str, name: str, head: str | None) -> None:
    """把新工程登记到 PG 的 `ame_catalog` 目录表（SQLite 模式无操作）。"""
    if getattr(root, "_store_factory", None) is None:
        return
    try:
        cat = root.open_project("__catalog__")
    except Exception:
        return
    try:
        # 不用 cat.commit()：Storage 协议没有 commit 方法，`execute` 在
        # batch 外已自动提交。写成 commit() 会被下面 except 吞掉成静默失败。
        cat.execute("INSERT INTO ame_catalog(project_id,name,created_at,head_commit_id) "
                    "VALUES (?,?,?,?)",
                    (pid, name, now_iso(), head))
    except Exception:
        pass          # 目录表写失败不应阻断建工程（列表会缺一条，但工程可用）
    finally:
        try:
            cat.close()
        except Exception:
            pass
