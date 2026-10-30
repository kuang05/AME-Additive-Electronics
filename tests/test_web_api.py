"""云上后端（FastAPI）验收：端到端闭环 + 错误处理 + 与桌面版一致性。

为什么用 TestClient 而不是 MCP/真浏览器
---------------------------------------
后端契约就是 HTTP。TestClient 直接调用 ASGI，能覆盖路由、序列化、
异常转换的全部逻辑；浏览器层（画布渲染）另在手工演示里验证。
两者分工明确：这里保证"接口给出的数据是对的"，前端只负责画。

最关键的一条：**同一份设计，云端接口算出的分数必须等于桌面版**。
这说明"云上"没有偷偷另写一套算法。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from conftest import SAMPLES  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立数据目录，避免工程列表互相污染。"""
    monkeypatch.setenv("AME_DATA_DIR", str(tmp_path / "cloud"))
    monkeypatch.delenv("AME_PG_DSN", raising=False)
    import ame.web.app as webapp
    # 重建 ROOT 与会话缓存，指向临时目录
    webapp.ROOT = webapp._build_root()
    webapp._SESSIONS.clear()
    with TestClient(webapp.app) as c:
        yield c
    webapp._SESSIONS.clear()


def _new_project(client, name="云端测试") -> str:
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _upload(client, pid: str, kind: str, path) -> dict:
    with open(path, "rb") as f:
        r = client.post(f"/api/projects/{pid}/import/{kind}",
                        files={"file": (path.name, f, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


# ================================================================ 健康与工程
def test_health_reports_backend(client):
    """健康检查必须报告存储后端——部署排障第一眼要看这个。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["storage"] == "sqlite"


def test_new_and_list_project(client):
    pid = _new_project(client, "我的板")
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json())


def test_sample_project_ready_to_use(client):
    """一键示例：创建后即有结构、电路、分数（NFR-003）。"""
    r = client.post("/api/projects/sample")
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    st = client.get(f"/api/projects/{pid}/state").json()
    assert st["outline"], "示例工程应有板框"
    assert st["routes"], "示例工程应有走线"
    assert st["score"] is not None


# ================================================================ 四域闭环
def test_full_chain_import_score_slice_export(client):
    """完整链路：导入 STEP → 板文件 → 评分 → 切片 → 下载 G-code。

    这是赛题一 Demo 的主线，任何一环断了这个测试就红。
    """
    pid = _new_project(client)
    step = _upload(client, pid, "step", SAMPLES / "plate_100x60.step")
    assert step["face_count"] == 6
    assert step["solid_count"] == 1
    assert step["diagnostics"] == [], "标准平板不应有诊断"

    board = _upload(client, pid, "board", SAMPLES / "board_T19A.kicad_pcb")
    assert board["comps"] == 8
    assert board["pads"] == 20

    mrc = client.post(f"/api/projects/{pid}/mrc").json()
    assert mrc["score"] == 60, f"T19-A 基准分应为 60，实际 {mrc['score']}"
    assert len(mrc["issues"]) == 12
    # 每条问题都要有可读原因（UI-008）
    assert all(i["reason"] for i in mrc["issues"])

    sl = client.post(f"/api/projects/{pid}/slice").json()
    assert sl["passes"] == 7
    assert sl["layer_count"] == 3
    assert sl["total_thickness_mm"] == pytest.approx(0.44, abs=0.01)
    assert sl["gcode_hash"]

    g = client.get(f"/api/projects/{pid}/gcode")
    assert g.status_code == 200
    assert "G21 ; mm" in g.text
    assert ";design_sha256" in g.text


def test_cloud_score_matches_desktop(tmp_path, client):
    """云端与桌面版对同一设计给出**相同分数**（说明没有两套算法）。

    这是本轮改造最想守住的性质：上云只是换壳，算法内核唯一。
    """
    from ame.db.store import DataRoot
    from ame.ledger.workspace import create_project

    # 桌面路径：直接用 Workspace 算分
    desk_root = DataRoot(tmp_path / "desk")
    ws = create_project(desk_root, "桌面对照")
    ws.cmd_import_step((SAMPLES / "plate_100x60.step").read_text("utf-8"),
                       "plate_100x60.step")
    ws.cmd_import_board((SAMPLES / "board_T19A.kicad_pcb").read_text("utf-8"))
    desktop_score = ws.run_mrc()["score"]

    # 云端路径：走 HTTP
    pid = _new_project(client, "云端对照")
    _upload(client, pid, "step", SAMPLES / "plate_100x60.step")
    _upload(client, pid, "board", SAMPLES / "board_T19A.kicad_pcb")
    cloud_score = client.post(f"/api/projects/{pid}/mrc").json()["score"]

    assert cloud_score == desktop_score == 60


def test_slice_is_deterministic_over_http(client):
    """同一工程两次切片，G-code 哈希一致（SLC-008 在云端同样成立）。"""
    pid = _new_project(client)
    _upload(client, pid, "step", SAMPLES / "plate_100x60.step")
    _upload(client, pid, "board", SAMPLES / "board_T19A.kicad_pcb")
    h1 = client.post(f"/api/projects/{pid}/slice").json()["gcode_hash"]
    h2 = client.post(f"/api/projects/{pid}/slice").json()["gcode_hash"]
    assert h1 == h2


def test_route_via_http_uses_command_bus(client):
    """Web 布线走的是同一个命令总线：合法路径入库、非法路径被拒。"""
    pid = _new_project(client)
    _upload(client, pid, "step", SAMPLES / "plate_100x60.step")
    _upload(client, pid, "board", SAMPLES / "board_T19A.kicad_pcb")
    before = len(client.get(f"/api/projects/{pid}/state").json()["routes"])

    r = client.post(f"/api/projects/{pid}/route",
                    json={"net": "N2", "pts": [[20, 40, 0], [40, 40, 0]],
                          "width": 0.6})
    assert r.status_code == 200, r.text
    after = len(client.get(f"/api/projects/{pid}/state").json()["routes"])
    assert after == before + 1

    # 低于工艺下限的线宽必须被拒（CIR-010），且返回中文原因
    bad = client.post(f"/api/projects/{pid}/route",
                      json={"net": "N2", "pts": [[20, 45, 0], [40, 45, 0]],
                            "width": 0.1})
    assert bad.status_code == 400
    assert any("\u4e00" <= c <= "\u9fff" for c in bad.json()["detail"])


# ================================================================ Agent / AI
def test_agent_endpoint_improves_score(client):
    """Agent 接口：分数提升、返回对比卡片与重放信息。"""
    pid = client.post("/api/projects/sample").json()["id"]
    res = client.post(f"/api/projects/{pid}/agent",
                      json={"auth": "auto", "max_rounds": 8}).json()
    assert res["score_final"] > res["score_start"]
    assert res["cards"], "应有对比卡片"
    assert res["verdict"] == "停止"


def test_ai_rank_endpoint_lists_candidates(client):
    """AI 收益排序接口：给出排序来源与候选列表。"""
    pid = client.post("/api/projects/sample").json()["id"]
    r = client.get(f"/api/projects/{pid}/ai/rank")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] in ("local", "heuristic", "cloud")
    assert d["ordered"], "样例板应有可执行候选"
    ranks = [p["rank"] for p in d["ordered"]]
    assert ranks == sorted(ranks), "rank 必须递增"


# ================================================================ 错误处理
def test_unknown_project_returns_404(client):
    r = client.get("/api/projects/not-exist/state")
    assert r.status_code == 404
    assert "打不开" in r.json()["detail"]


def test_slice_without_board_returns_readable_error(client):
    """无板框切片：400 + 中文原因，不是裸 500（UI-005）。"""
    pid = _new_project(client, "空工程")
    r = client.post(f"/api/projects/{pid}/slice")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert any("\u4e00" <= c <= "\u9fff" for c in detail)
    assert "板框" in detail or "切片" in detail


def test_gcode_before_slice_is_400(client):
    pid = _new_project(client)
    r = client.get(f"/api/projects/{pid}/gcode")
    assert r.status_code == 400
    assert "切片" in r.json()["detail"]


def test_meta_declares_honest_scope(client):
    """能力清单必须如实说明用了什么、没用什么（避免答辩被打穿）。"""
    d = client.get("/api/meta").json()
    notes = " ".join(d["honest_notes"])
    assert "MindSpore" in notes and "未使用" in notes


def test_index_page_served(client):
    """首页必须返回**真实前端页面**（含画布元素），不只是 200。

    前身断言 `status in (200, 503)`——审计实测**删掉 index.html 该测试仍然通过**：
    应用在资源缺失时会返回 503 + 占位文案，两种状态都被这个断言接受，
    等于"无论前端在不在都算对"。前端资源是赛题要求的"可视化 Demo 页面"，
    缺失就是交付物缺失，不能静默接受。

    现在断言：200 + 真实页面特征（`<canvas`、修复后的行为）。
    """
    r = client.get("/")
    assert r.status_code == 200, (
        f"首页应返回 200（前端资源缺失时会 503），实际 {r.status_code}")
    assert "<canvas" in r.text, "首页应含 <canvas> 元素（可视化 Demo 的核心）"
    # 二次确认 index.html 真实存在（而不是刚好被别的路由兜底）
    assert (Path(__file__).resolve().parent.parent
            / "ame" / "web" / "static" / "index.html").exists()


# ================================================================ 交互补齐（26 命令对齐）
def test_change_width_endpoint(client):
    """改线宽：落库成功；低于工艺下限被拒并给中文原因（CIR-010）。"""
    pid = client.post("/api/projects/sample").json()["id"]
    routes = client.get(f"/api/projects/{pid}/state").json()["routes"]
    rid = routes[0]["id"]

    ok = client.post(f"/api/projects/{pid}/routes/{rid}/width", json={"width": 0.8})
    assert ok.status_code == 200, ok.text
    after = client.get(f"/api/projects/{pid}/state").json()["routes"]
    assert next(r for r in after if r["id"] == rid)["width"] == pytest.approx(0.8)

    bad = client.post(f"/api/projects/{pid}/routes/{rid}/width", json={"width": 0.1})
    assert bad.status_code == 400
    assert any("\u4e00" <= c <= "\u9fff" for c in bad.json()["detail"])


def test_delete_route_and_undo(client):
    """删除走线后能用撤销恢复（UI 与 API 共享同一条命令总线）。"""
    pid = client.post("/api/projects/sample").json()["id"]
    routes = client.get(f"/api/projects/{pid}/state").json()["routes"]
    n0 = len(routes)
    rid = routes[0]["id"]

    assert client.delete(f"/api/projects/{pid}/routes/{rid}").status_code == 200
    assert len(client.get(f"/api/projects/{pid}/state").json()["routes"]) == n0 - 1

    u = client.post(f"/api/projects/{pid}/undo")
    assert u.status_code == 200
    assert len(client.get(f"/api/projects/{pid}/state").json()["routes"]) == n0


def test_redo_after_undo(client):
    """撤销后重做，状态回到撤销前。"""
    pid = client.post("/api/projects/sample").json()["id"]
    routes = client.get(f"/api/projects/{pid}/state").json()["routes"]
    n0 = len(routes)
    rid = routes[0]["id"]
    client.delete(f"/api/projects/{pid}/routes/{rid}")
    client.post(f"/api/projects/{pid}/undo")
    r = client.post(f"/api/projects/{pid}/redo")
    assert r.status_code == 200
    assert len(client.get(f"/api/projects/{pid}/state").json()["routes"]) == n0 - 1


def test_move_comp_endpoint(client):
    """移动器件：坐标更新且焊盘随动（CIR-002）。"""
    pid = client.post("/api/projects/sample").json()["id"]
    st = client.get(f"/api/projects/{pid}/state").json()
    comp = st["comps"][0]
    pads_before = {(p["ref"], p["pad_no"]): (p["x"], p["y"])
                   for p in st["pads"] if p["ref"] == comp["ref"]}

    r = client.post(f"/api/projects/{pid}/comps/{comp['id']}/move",
                    json={"x": comp["x"] + 5, "y": comp["y"] + 3})
    assert r.status_code == 200, r.text

    st2 = client.get(f"/api/projects/{pid}/state").json()
    c2 = next(c for c in st2["comps"] if c["id"] == comp["id"])
    assert c2["x"] == pytest.approx(comp["x"] + 5)

    pads_after = {(p["ref"], p["pad_no"]): (p["x"], p["y"])
                  for p in st2["pads"] if p["ref"] == comp["ref"]}
    assert pads_after != pads_before, "焊盘必须随器件平移"


def test_rules_list_and_threshold_change(client):
    """规则阈值可读可改，改完评分随之变化（MRC-011）。"""
    pid = client.post("/api/projects/sample").json()["id"]
    rules = client.get(f"/api/projects/{pid}/rules").json()
    assert len(rules) == 7
    assert {r["code"] for r in rules} == {"R1", "R2", "R3", "R4", "R5", "R6", "R7"}

    before = client.post(f"/api/projects/{pid}/mrc").json()["score"]
    assert client.post(f"/api/projects/{pid}/rules/R1",
                       json={"threshold": {"min_width": 0.1}}).status_code == 200
    after = client.post(f"/api/projects/{pid}/mrc").json()
    assert after["score"] >= before
    assert not [i for i in after["issues"] if i["rule_code"] == "R1"]


def test_rules_reject_invalid_threshold(client):
    """非法阈值必须被拒并说明原因（SLC-012 同类防线）。"""
    pid = client.post("/api/projects/sample").json()["id"]
    r = client.post(f"/api/projects/{pid}/rules/R1",
                    json={"threshold": {"min_width": -5}})
    assert r.status_code == 400
    assert any("\u4e00" <= c <= "\u9fff" for c in r.json()["detail"])


def test_versions_endpoint(client):
    """版本历史：示例工程提交过基线，至少一条。"""
    pid = client.post("/api/projects/sample").json()["id"]
    v = client.get(f"/api/projects/{pid}/versions").json()
    assert v, "示例工程应有版本记录"
    assert "seq" in v[0] and "reason" in v[0]


def test_heatmap_endpoint(client):
    """热力图：有违规时给出点位与权重。"""
    pid = client.post("/api/projects/sample").json()["id"]
    h = client.get(f"/api/projects/{pid}/heatmap").json()
    assert h["points"], "样例板有违规，应有热力点"
    assert h["max_weight"] > 0
    assert len(h["bbox"]) == 4


def test_auto_outline_endpoint(client):
    """按模型自动生成板框（STR-007 捷径）。"""
    pid = _new_project(client, "自动板框")
    _upload(client, pid, "step", SAMPLES / "plate_100x60.step")
    r = client.post(f"/api/projects/{pid}/outline/auto")
    assert r.status_code == 200, r.text
    st = client.get(f"/api/projects/{pid}/state").json()
    assert st["outline"], "应生成板框"


def test_command_coverage_is_documented():
    """Web 暴露的命令必须来自**真实调用**，不能靠名字出现在注释里。

    前身实现用 `c not in web` —— 源码全文的字符串包含判断。审计实测：
    把真实调用全改掉、只在文件顶部留一行注释
    `# placeholder: cmd_add_route cmd_delete_route ...` → 该测试仍然 1 passed。
    相当于"文档里写了就等于实现了"。

    现在改为 AST 收集 `ws.cmd_xxx(` 的真实 Call 节点，与 commands.py 的定义做集合比较。
    同时保留"命令总数变化要同步检查"的护栏。
    """
    import ast
    from pathlib import Path as _P

    src = _P("ame/ledger/commands.py").read_text("utf-8")
    all_cmds = {n.name for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name.startswith("cmd_")}

    app_src = _P("ame/web/app.py").read_text("utf-8")
    tree = ast.parse(app_src)

    def _called_name(node) -> str | None:
        """`x.cmd_foo(...)` → "cmd_foo"；只取属性调用的方法名。"""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr.startswith("cmd_")):
            return node.func.attr
        return None

    # 直接调用 + getattr(ws, "cmd_xxx") 两种形态都要认
    called = {c for c in (_called_name(n) for n in ast.walk(tree)) if c}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "getattr" and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant)
                and isinstance(n.args[1].value, str)
                and n.args[1].value.startswith("cmd_")):
            called.add(n.args[1].value)

    required = {"cmd_add_route", "cmd_delete_route", "cmd_change_width",
                "cmd_move_comp", "cmd_import_step", "cmd_import_board",
                "cmd_set_outline", "cmd_set_rule"}
    missing = required - called
    assert not missing, (
        f"Web 未**调用**这些核心命令：{missing}"
        f"（当前实际调用 {len(called)} 个：{sorted(called)}）")
    # 引用的命令必须真的存在
    unknown = called - all_cmds
    assert not unknown, f"Web 调用了不存在的命令：{unknown}"
    assert len(all_cmds) == 26, "命令总数变化时请同步检查 Web 覆盖"


# ================================================================ 前端契约（静态检查）
def test_frontend_has_routing_and_selection_wiring():
    """前端必须真的实现了它宣称的交互。

    这条测试针对一个具体的历史问题：上一版页面上写着"画布上拖拽可布线"，
    但 `index.html` 里**没有任何布线提交代码**——纯虚假宣称。
    评委随手点一下就露，比缺功能更糟。

    这里做静态检查：布线必须调用 `/route` 接口，选择必须能打开属性面板，
    撤销/重做必须接到对应端点。
    """
    html = (Path(__file__).resolve().parent.parent
            / "ame" / "web" / "static" / "index.html").read_text("utf-8")
    # 布线：必须真的 POST 到 /route
    assert "/route`" in html, "前端必须有布线提交调用"
    assert "dblclick" in html, "布线需要双击结束"
    # 选择与属性
    assert "selectAt" in html and "showProp" in html, "需要点选与属性面板"
    assert "/width`" in html or "routes/${" in html, "属性面板需要能改线宽"
    # 撤销/重做
    assert "/undo" in html and "/redo" in html
    # 不得再出现无实现的宣称
    assert "拖拽可布线" not in html, "不能保留未实现的交互宣称"


def test_frontend_debug_hook_is_read_only():
    """前端调试钩子只暴露读接口（写入必须走 REST，不能有旁路）。"""
    html = (Path(__file__).resolve().parent.parent
            / "ame" / "web" / "static" / "index.html").read_text("utf-8")
    assert "window.__ame" in html
    # 钩子里不得出现写操作
    hook = html.split("window.__ame = {")[1].split("};")[0]
    for bad in ("fetch(", "POST", "DELETE", "mutate", "set("):
        assert bad not in hook, f"调试钩子不应含写操作：{bad}"


# ================================================================ 安全与资源（审计修复）
class TestSessionSecurityAndLeaks:
    """路径穿越与失效工程泄漏（审计发现的真实缺陷）。"""

    def test_path_traversal_project_id_rejected(self, client):
        """URL 里的 project_id 不得拼出数据目录外的文件（NFR-005）。

        前身测试有两处盲区（审计实测）：
          ① 断言 `status in (400, 404)` —— 而 `_ws()` 会把任何异常转成 404，
             所以无论有没有校验都成立；
          ② glob 的是 `tmp_path.parent`，但穿越落点实际在 `tmp_path`
             （`AME_DATA_DIR=tmp_path/cloud` + `projects/../../victim.db`）。
             删掉校验后 `victim.db` 确实落到 `tmp_path`，测试却看的是父目录。
        现在改为：断言拒绝原因含 NFR-005，并在**两个候选目录**里都查落点。
        """
        import ame.web.app as webapp

        base = Path(webapp.ROOT.data_dir)
        candidates = [base, base.parent, base.parent.parent]
        before = {c: set(c.glob("*.db")) if c.exists() else set()
                  for c in candidates}

        evil = "..%5C..%5Cvictim"
        r = client.get(f"/api/projects/{evil}/state")
        assert r.status_code in (400, 404), f"应拒绝，实际 {r.status_code}"

        # ① 必须是"id 非法"这条理由，不能只是"文件不存在"
        detail = r.json().get("detail", "")
        assert "NFR-005" in detail or "非法" in detail or "只允许" in detail, (
            f"拒绝原因应指向 id 校验（NFR-005），实际：{detail}")

        # ② 落点检查：所有候选目录都不得新增 victim*.db
        for c in candidates:
            leaked = set(c.glob("victim*.db")) if c.exists() else set()
            new = leaked - before[c]
            assert not new, f"穿越在 {c} 下创建了文件：{new}"

    def test_traversal_does_not_open_preexisting_outside_db(self, client, tmp_path):
        """数据目录外已存在同名库时，也不得被打开/写入（正向对照）。"""
        import ame.web.app as webapp

        base = Path(webapp.ROOT.data_dir)
        outside = base.parent / "victim.db"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"")          # 空文件：若被打开会因无表而暴露

        r = client.get("/api/projects/..%5C..%5Cvictim/state")
        assert r.status_code in (400, 404)
        # 空文件不应被建表（大小应保持 0）
        assert outside.stat().st_size == 0, "外部库被打开并被建表了"

    def test_invalid_id_never_creates_db(self, client, tmp_path, monkeypatch):
        """任意不存在的工程 id 不得留下 .db/-wal/-shm 三件套。"""
        import ame.web.app as webapp

        projects_dir = webapp.ROOT.data_dir / "projects"
        before = len(list(projects_dir.glob("*"))) if projects_dir.exists() else 0
        for i in range(3):
            client.get(f"/api/projects/ghost-{i}/state")
        after = len(list(projects_dir.glob("*"))) if projects_dir.exists() else 0
        assert after == before, (
            f"不存在的工程被创建了文件（{before} → {after}）："
            "打开失败时必须关掉已建立的连接并清理")

    def test_unknown_project_returns_404_not_500(self, client):
        r = client.get("/api/projects/no-such-project/state")
        assert r.status_code == 404
        body = r.json()
        assert "打不开" in body.get("detail", "") or "不存在" in body.get("detail", "")


class TestUnhandledErrorHidesInternals:
    """错误响应不得泄漏内部信息（审计 P1-4）。

    前身测试写的是 `if r.status_code == 500: assert ...`，而它请求的工程不存在
    → 恒为 404 → **4 条断言一次都没执行**。审计实测把它要防的泄漏形态注回去，
    测试仍然 pass。更糟的是内部路径本来就在泄漏，只是从 **400** 出去：
    `_err()` 对非 CmdError 异常返回 `f"{type(e).__name__}: {str(e)}"`，
    实测 `GET /slice` 返回 `RuntimeError: 内部路径 C:/secret/x.db`。

    现在两条都堵上：`_err()` 只回中文兜底 + request_id，测试对**任何** 4xx/5xx
    都断言响应体不含原始异常串。
    """

    @staticmethod
    def _has_leak(body: str) -> bool:
        leaks = ["C:/secret", "C:" + chr(92) + "secret", "RuntimeError",
                 "Traceback", "closed database", "/opt/x.db", "SECRET"]
        return any(s in body for s in leaks)

    def test_error_body_never_leaks_internal_detail(self, client, tmp_path,
                                                    monkeypatch):
        """任何 4xx/5xx 的响应体都不得含原始异常串（含真实存在的工程）。"""
        import ame.web.app as webapp

        # 造一个真实工程，让请求能走到业务逻辑深处（而不是 404 短路）
        r = client.post("/api/projects", json={"name": "泄漏测试"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]

        # 让切片内部抛一个带"敏感路径"的异常
        def boom(*a, **kw):
            raise RuntimeError("内部路径 C:/secret/x.db 不应泄露")

        monkeypatch.setattr(webapp, "do_slice", boom)
        resp = client.post(f"/api/projects/{pid}/slice")
        assert resp.status_code >= 400, "应报错"
        body = resp.text
        assert not self._has_leak(body), (
            f"响应体泄漏了内部信息：{body[:300]}")
        # 必须给出可上报的编号
        assert "错误编号" in body or "request_id" in body, (
            f"错误响应应给可上报编号：{body[:200]}")

    def test_error_body_keeps_readable_chinese(self, client):
        """业务错误（CmdError）仍要回中文可读原因，不能被兜底吞成通用文案。"""
        r = client.get("/api/projects/no-such-project/state")
        assert r.status_code == 404
        detail = r.json().get("detail", "")
        assert "打不开" in detail or "不存在" in detail, (
            f"业务错误应保留可读中文：{detail}")
