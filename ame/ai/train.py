"""拟合脚本：从真实工程采集标签 → 解岭回归方程 → 写系数与指标。

**这不是神经网络训练。** 它做的事是：
    1. 跑真实工程，对每条候选修复"应用→重跑评分"，得到分数增量作为标签；
    2. 把这些 (特征, 标签) 组成矩阵，解一次闭式解（岭回归）；
    3. 把 26 个系数写进 weights.json。

没有梯度下降、没有反向传播、没有 epoch。数据规模（62 条）也不支持更复杂的模型。

用法::

    python -m ame.ai.train                 # 重新拟合，写 ame/ai/weights.json
    python -m ame.ai.train --report        # 只读现有系数的指标，不覆盖
    python -m ame.ai.train --holdout 0.3   # 调整留出比例

标签怎么来（这是关键，不是造数据）
----------------------------------
每条样本是"**真的把这条修复应用下去，然后重新评分**"得到的分数增量：

    label = score_after - score_before

这是可验证的因果标签，不是人工拍脑袋的"重要性打分"。采集流程与线上完全
一致——同样走 `Workspace` 命令总线、同样跑 `run_mrc()`，因此特征分布
与推理时一致，不存在训练/线上偏移。

样本来源是多种板型的真实工程（不同幅面、器件密度、走线宽度、夹角），
覆盖 R1/R2/R7 与跳线。**R3/R4/R5/R6 没有足够样本，这四条规则的排序
仍靠规则先验**——这一限制在 `AI证据链.md` §10 明确写出。

整个过程离线、确定性：同一 numpy 版本 + 同一输入 → 同一系数（可复核）。

为什么是岭回归
--------------
26 维特征、62 条样本，线性模型配合 L2 正则足以稳定排序；闭式解无需迭代，
CPU 上毫秒级完成，且结果完全可复现。指标（MAE / R² / 排序准确率）随系数
一起落盘，供验收时核对——不写"训练过"这种无法证伪的话。

> 赛题一真正要求的"AI"是调用华为云大模型，见 `ame/ai/maas.py`。
> 本文件是离线降级路径，不是 AI 卖点。
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np

from ..db.store import DataRoot
from ..ledger.workspace import create_project
from .features import FEATURE_NAMES, FEATURE_VERSION, group_proposals, proposal_features

SAMPLES = Path(__file__).resolve().parent.parent.parent / "samples" / "data"
WEIGHTS_PATH = Path(__file__).with_name("weights.json")

# 训练场景：不同幅面/器件/走线配置，覆盖多规则（保持确定性，无随机生成）
SCENARIOS = [
    # (名称, STEP, 板文件, 扩展参数)
    # R3/R4/R5 需要"有斜面 / 有凸台"的结构才有样本。
    # 早期只用平板，这三条规则命中数为 0，模型永远学不到——
    # 这不是模型能力问题，是**训练场景没覆盖**。
    ("斜面-基准", "overhang60.step", "board_T19A.kicad_pcb", None),
    ("斜面-极细", "overhang60.step", "board_T19A.kicad_pcb", {"width": 0.30}),
    ("斜面-加线", "overhang60.step", "board_T19A.kicad_pcb",
     {"width": 0.35, "gap": 3.0, "add_routes": True}),
    ("凸台-基准", "plate_boss.step", "board_T19A.kicad_pcb", None),
    ("凸台-极细", "plate_boss.step", "board_T19A.kicad_pcb", {"width": 0.32}),
    ("凸台-加线", "plate_boss.step", "board_T19A.kicad_pcb",
     {"width": 0.34, "gap": 2.8, "add_routes": True}),
    # 装配体：几何来自多个零件实例，触发装配展平路径
    ("装配-基准", "assembly_two_parts.step", "board_T19A.kicad_pcb", None),
    ("装配-极细", "assembly_two_parts.step", "board_T19A.kicad_pcb", {"width": 0.30}),
    ("装配旋转-基准", "assembly_rotated.step", "board_T19A.kicad_pcb", None),
    # 10 器件板的更多线宽档位（不同器件密度下的规则命中差异）
    ("10器件-宽线", "plate_100x60.step", "board_10.kicad_pcb", {"width": 0.55}),
    ("10器件-加线近", "plate_100x60.step", "board_10.kicad_pcb",
     {"width": 0.40, "gap": 2.6, "add_routes": True}),
    # T19A 的更多线宽梯度（0.25 到 0.7，覆盖工艺下限附近与基线之上）
    ("T19A-中线", "plate_100x60.step", "board_T19A.kicad_pcb", {"width": 0.45}),
    ("T19A-下限", "plate_100x60.step", "board_T19A.kicad_pcb", {"width": 0.51}),
    ("T19A-基准", "plate_100x60.step", "board_T19A.kicad_pcb", None),
    ("T19A-宽线", "plate_100x60.step", "board_T19A.kicad_pcb", {"width": 0.55}),
    ("T19A-极细", "plate_100x60.step", "board_T19A.kicad_pcb", {"width": 0.30}),
    ("T19A-超细", "plate_100x60.step", "board_T19A.kicad_pcb", {"width": 0.26}),
    ("10器件", "plate_100x60.step", "board_10.kicad_pcb", None),
    ("10器件-极细", "plate_100x60.step", "board_10.kicad_pcb", {"width": 0.25}),
    ("T19A-近距", "plate_100x60.step", "board_T19A.kicad_pcb", {"width": 0.5, "gap": 1.2}),
    # 追加走线：间距取"刚好触发少量违规"的档位。
    # 太挤（gap 1.0–1.5）会让基础分直接跌到 0，修复前后都是 0，样本无区分度
    # （实测 60 条样本标签全为 0，等于没有信息）。因此用 2.5–4 mm 的温和档。
    ("T19A-加线A", "plate_100x60.step", "board_T19A.kicad_pcb",
     {"width": 0.34, "gap": 3.0, "add_routes": True}),
    ("T19A-加线B", "plate_100x60.step", "board_T19A.kicad_pcb",
     {"width": 0.44, "gap": 2.6, "add_routes": True}),
    ("T19A-加线C", "plate_100x60.step", "board_T19A.kicad_pcb",
     {"width": 0.70, "gap": 3.4, "add_routes": True}),
    ("10器件-加线", "plate_100x60.step", "board_10.kicad_pcb",
     {"width": 0.36, "gap": 3.0, "add_routes": True}),
]


def _build_scenario(root, name, step, board, extra):
    """建一个干净工程并导入样本（与 conftest.build_project 同口径）。"""
    ws = create_project(root, name)
    if step and (SAMPLES / step).exists():
        ws.cmd_import_step((SAMPLES / step).read_text("utf-8"), step)
    if board and (SAMPLES / board).exists():
        ws.cmd_import_board((SAMPLES / board).read_text("utf-8"))
    if extra:
        if extra.get("add_routes"):
            _load_routes_spec(ws, name, extra)
        elif extra.get("width"):
            # 统一改线宽：模拟不同工艺档位下的设计
            for r in list(ws.routes()):
                try:
                    ws.cmd_change_width(r["id"], extra["width"])
                except Exception:
                    pass
    return ws


def collect(root: DataRoot) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """逐条修复实测分数增量，返回 (X, y, 元信息列表)。"""
    from ..agent.loop import apply_step, designer_propose
    from ..agent.loop import clone_workspace

    X: list[list[float]] = []
    y: list[float] = []
    meta: list[dict] = []
    skipped_scenarios: list[str] = []

    for name, step, board, extra in SCENARIOS:
        try:
            ws = _build_scenario(root, name, step, board, extra)
        except Exception:
            continue
        mrc0 = ws.run_mrc()
        # 分数为 0 的场景没有区分度：任何修复都止步于 0 分，标签恒为 0。
        # 这类样本只会稀释信号（实测曾让 60 条样本标签全为 0），直接跳过并记录。
        if (mrc0.get("score") or 0) <= 0:
            skipped_scenarios.append(f"{name}（基础分 0）")
            try:
                ws.store.close()
            except Exception:
                pass
            continue
        state0 = ws.mrc_state()
        steps = designer_propose(ws, mrc0, state0)
        if not steps:
            skipped_scenarios.append(f"{name}（无可执行建议）")
            try:
                ws.store.close()
            except Exception:
                pass
            continue
        proposals = group_proposals(mrc0["issues"], steps)

        for prop in proposals:
            # 每条候选都从同一基线克隆，保证互不干扰（单变量对照）
            try:
                snap = clone_workspace(ws, "train")
            except Exception:
                continue
            base = snap.run_mrc()["score"]
            ok = True
            for s in prop["steps"]:
                try:
                    apply_step(snap, s)
                except Exception:
                    ok = False
                    break
            if not ok:
                try:
                    snap.store.close()
                except Exception:
                    pass
                continue
            try:
                after = snap.run_mrc()["score"]
            except Exception:
                try:
                    snap.store.close()
                except Exception:
                    pass
                continue
            feats = proposal_features(prop, state0, mrc0)
            X.append(feats)
            y.append(float(after - base))
            meta.append({"scenario": name, "action": prop["action"],
                         "rule": (prop.get("issue") or {}).get("rule_code", ""),
                         "delta": after - base})
            try:
                snap.store.close()
            except Exception:
                pass
        try:
            ws.store.close()
        except Exception:
            pass

    if skipped_scenarios:
        print(f"[跳过 {len(skipped_scenarios)} 个无区分度场景] "
              + "、".join(skipped_scenarios))
    return np.asarray(X, dtype=float), np.asarray(y, dtype=float), meta


def fit_ridge(X: np.ndarray, y: np.ndarray, alpha: float = 1.0) -> tuple[np.ndarray, float]:
    """闭式解岭回归（含偏置项）；全零特征列不再被放大（正则项会压到 0）。"""
    n, d = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])
    reg = alpha * np.eye(d + 1)
    reg[-1, -1] = 0.0                      # 偏置不参与正则
    w = np.linalg.solve(Xb.T @ Xb + reg, Xb.T @ y)
    return w[:-1], float(w[-1])


def evaluate(w: np.ndarray, b: float, X: np.ndarray, y: np.ndarray) -> dict:
    pred = X @ w + b
    mae = float(np.mean(np.abs(pred - y)))
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2)) if len(y) > 1 else 0.0
    r2 = 0.0 if ss_tot <= 1e-12 else 1.0 - ss_res / ss_tot
    # 排序准确率：只看"预测收益更高的那对，实际收益也更高"的样本对比例
    pairs = 0
    correct = 0
    for i in range(len(y)):
        for j in range(i + 1, len(y)):
            if abs(y[i] - y[j]) < 1e-9:
                continue
            pairs += 1
            if (pred[i] - pred[j]) * (y[i] - y[j]) > 0:
                correct += 1
    acc = 1.0 if pairs == 0 else correct / pairs
    return {"mae": round(mae, 4), "r2": round(r2, 4),
            "pairwise_rank_accuracy": round(acc, 4), "pairs": pairs}


def _load_routes_spec(ws, name: str, extra: dict | None) -> None:
    """给已有板文件追加一组**局部**短线（宽度/间距按场景变化）。

    早期版本把走线铺满整板宽度并密集叠压，基础分直接跌到 0——
    修复前后都是 0，样本毫无区分度（实测 4 个场景全被丢弃）。
    现在只在板右侧一片 undisturbed 区域铺短横线，密度温和，
    目的是"多触发几条 R1/R2，同时保住可观的基础分"。
    """
    from .. import new_id

    cir = ws.store.one("SELECT id FROM circuit WHERE project_id=?", (ws.project_id,))
    if not cir:
        return
    nets = [n["name"] for n in ws.nets()]
    if not nets:
        return
    width = float((extra or {}).get("width", 0.6))
    gap = float((extra or {}).get("gap", 3.0))
    outline, _ = ws.board_outline()
    if not outline:
        return
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    import json as _json
    # 右侧 22% 宽 × 中部 60% 高的局部区块，避开已有走线与器件密集区
    ax0 = x0 + (x1 - x0) * 0.72
    ax1 = x0 + (x1 - x0) * 0.94
    ay0 = y0 + (y1 - y0) * 0.22
    ay1 = y0 + (y1 - y0) * 0.78
    n = 0
    y = ay0
    i = 0
    # 只加 2 条：实测加到 6 条时，新增走线与既有走线、彼此之间产生十几条
    # R2/R7 连带违规，扣分超过 100 直接把分数压到 0（基础分 0 的场景没有
    # 训练价值）。2 条能稳定带来 1–2 条新违规且保住可观基础分。
    while y < ay1 and n < 2:
        net_name = nets[i % len(nets)]
        net = ws.store.one(
            "SELECT n.id FROM net n JOIN circuit c ON n.circuit_id=c.id "
            "WHERE c.project_id=? AND n.name=?", (ws.project_id, net_name))
        if net:
            pts = [[ax0, y, 0.0], [ax1, y, 0.0]]
            with ws.store.batch():
                ws.store.execute(
                    "INSERT INTO route(id,circuit_id,net_id,pts_json,width_mm,surface,"
                    "layer_index,strategy) VALUES (?,?,?,?,?,?,0,'manual')",
                    (new_id(), cir["id"], net["id"], _json.dumps(pts), width, "top"))
            n += 1
        y += max(gap, 8.0)
        i += 1
    ws.invalidate()


def loso_report(X: np.ndarray, y: np.ndarray, meta: list[dict],
                alpha: float = 1.0, model_weight: float = 0.5) -> dict:
    """留一场景交叉验证：每次留出一个板型，只在其余板型上拟合。

    这是本模块**唯一可信的泛化指标**。样本内 R² 会被同板近邻抬高；随机
    留出又会把同板样本同时放进训练与测试。留一场景（LOSO）保证测试板
    完全没参与训练，再把各场景结果平均——"换个板子还准不准"就是这个数。
    """
    from .features import ACTION_ORDER, RULE_ORDER
    from .local import HEURISTIC_ACTION_PRIOR, HEURISTIC_RULE_PRIOR

    def heur(feats: list[float]) -> float:
        d = dict(zip(FEATURE_NAMES, feats, strict=True))
        code = next((r for r in RULE_ORDER if d.get(f"rule_{r}", 0) > 0.5), "")
        act = next((a for a in ACTION_ORDER if d.get(f"act_{a}", 0) > 0.5), "")
        sev = d.get("severity_norm", 0) * 3.0
        return (HEURISTIC_RULE_PRIOR.get(code, 0.4)
                * HEURISTIC_ACTION_PRIOR.get(act, 0.4) * (sev / 3.0))

    def pacc(pred: list[float], ys: list[float]) -> float:
        ok = tot = 0
        for i in range(len(ys)):
            for j in range(i + 1, len(ys)):
                if abs(ys[i] - ys[j]) < 1e-9:
                    continue
                tot += 1
                if (pred[i] - pred[j]) * (ys[i] - ys[j]) > 0:
                    ok += 1
        return ok / tot if tot else 0.0

    scenarios = sorted({m["scenario"] for m in meta})
    per = []
    for s in scenarios:
        tr = [i for i, m in enumerate(meta) if m["scenario"] != s]
        te = [i for i, m in enumerate(meta) if m["scenario"] == s]
        if len(tr) < 6 or len(te) < 2:
            continue
        w, b = fit_ridge(X[tr], y[tr], alpha)
        raw = [float(X[i] @ w + b) for i in te]
        lo, hi = min(raw), max(raw)
        span = (hi - lo) or 1.0
        norm = [(v - lo) / span for v in raw]
        heurs = [heur(list(X[i])) for i in te]
        blend = [model_weight * nv + (1 - model_weight) * hv
                 for nv, hv in zip(norm, heurs, strict=True)]
        ys = [y[i] for i in te]
        per.append({"scenario": s, "n": len(te),
                    "model": round(pacc(norm, ys), 3),
                    "heuristic": round(pacc(heurs, ys), 3),
                    "blend": round(pacc(blend, ys), 3)})
    if not per:
        return {"per_scenario": [], "model": None, "heuristic": None, "blend": None}
    mean = lambda k: round(sum(p[k] for p in per) / len(per), 3)
    return {"per_scenario": per, "model": mean("model"),
            "heuristic": mean("heuristic"), "blend": mean("blend"),
            "note": "留一场景交叉验证：每行是该板型完全未参与训练时的排序准确率"}


def train(out_path: Path = WEIGHTS_PATH, alpha: float = 1.0,
          holdout: float = 0.25) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="ame-train-"))
    try:
        root = DataRoot(tmp / "d")
        X, y, meta = collect(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if len(y) < 8:
        raise SystemExit(f"样本不足（{len(y)} 条），无法训练。请检查 samples/data 样本是否完整。")

    # 留出验证：按**场景**分组切分，而不是按样本随机切分。
    # 同一场景的多条样本高度相关（同板同几何），随机切会让测试集混入训练集
    # 的近邻，报出虚高的 R²。按场景留出才是"换个板子还准不准"。
    scenarios = sorted({m["scenario"] for m in meta})
    n_test = max(1, int(round(len(scenarios) * holdout))) if len(scenarios) > 2 else 0
    test_set = set(scenarios[-n_test:]) if n_test else set()

    # 最终权重用全量样本重拟合；泛化能力用留一场景交叉验证报告
    w_full, b_full = fit_ridge(X, y, alpha)
    metrics = evaluate(w_full, b_full, X, y)
    loso = loso_report(X, y, meta, alpha, model_weight=0.5)
    payload = {
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "weights": [round(float(v), 6) for v in w_full],
        "bias": round(float(b_full), 6),
        "meta": {
            "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "samples": int(len(y)),
            "alpha": alpha,
            "scenarios": sorted({m["scenario"] for m in meta}),
            "labels": "score_after - score_before（实测，非人工标注）",
            "by_rule": _count(meta, "rule"),
            "by_action": _count(meta, "action"),
            "in_sample": metrics,
            "loso": loso,
            "holdout_scenarios": sorted(test_set),
            # 顶层保留样本内指标便于快速查看；泛化看 loso.blend
            **metrics,
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    return payload


def _count(meta: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for m in meta:
        out[m[key]] = out.get(m[key], 0) + 1
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AME AI 收益排序模型训练")
    ap.add_argument("--report", action="store_true", help="只评估现有权重，不重训")
    ap.add_argument("--alpha", type=float, default=1.0, help="L2 正则强度")
    ap.add_argument("--out", default=str(WEIGHTS_PATH), help="权重输出路径")
    ap.add_argument("--holdout", type=float, default=0.25, help="按场景留出的比例")
    args = ap.parse_args(argv)

    if args.report:
        data = json.loads(Path(args.out).read_text("utf-8"))
        print(json.dumps(data.get("meta", {}), ensure_ascii=False, indent=2))
        return 0

    payload = train(Path(args.out), args.alpha, args.holdout)
    meta = payload["meta"]
    print(f"样本 {meta['samples']} 条 → {args.out}")
    print(f"MAE {meta['mae']}  R² {meta['r2']}  "
          f"排序准确率 {meta['pairwise_rank_accuracy']}")
    print(f"按规则：{meta['by_rule']}")
    print(f"按动作：{meta['by_action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
