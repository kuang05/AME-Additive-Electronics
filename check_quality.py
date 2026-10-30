"""质量门禁：一条命令检查代码健康度（提交前 / CI 用）。

用法::

    python check_quality.py              # 全部门禁
    python check_quality.py --fast       # 跳过测试（只做静态检查）
    python check_quality.py --coverage   # 附带覆盖率报告

门禁项与阈值（都是可复现的客观指标，不是主观评价）::

    1. 静态正确性      ruff 的 E9/F/B 规则零违规
    2. 未定义名        F821 必须为零（这类问题只会在运行到那行时才炸）
    3. 模块规模        单文件 ≤ 1200 行（超过说明该拆了）
    4. 类型注解        公开函数返回注解覆盖 ≥ 60%
    5. 测试            全部通过
    6. 打包自检        源码形态 11 步全过（可选，--full）

为什么要有这一层
----------------
上一轮重构靠人工逐项检查，容易漏。把"什么算合格"写成可执行脚本后，
任何人（包括后续接手的人）都能一条命令确认代码没退化。
阈值取的是当前实测值的下限，故意留了一点余量，但不给大幅退回的空间。
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AME = ROOT / "ame"

# 阈值（当前实测值的下限；降低它等于接受退化）
MAX_MODULE_LINES = 1200
MIN_RETURN_ANN_PCT = 60.0
MIN_PARAM_ANN_PCT = 55.0


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.items.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def ok(self) -> bool:
        return all(x[1] for x in self.items)

    def summary(self) -> str:
        total = len(self.items)
        passed = sum(1 for x in self.items if x[1])
        return f"{passed}/{total} 项通过"


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check_ruff(rep: Report) -> None:
    """业务代码与测试代码都按住同一套规则。

    测试代码也纳入是有理由的：测试是"正确性的最后一道断言"，它自己出错时
    往往表现为静默失效（例如 zip 截断让断言只检查了前几个元素），
    比业务代码出错更难发现。
    """
    # 这里**不加** `--ignore-noqa`：F401（未使用导入）在本项目是**有意的**
    # ——`stepio` / `connectivity` / `slicer` 保留了旧导入路径作 re-export 兼容层，
    # 代码里用 `# noqa: F401` 标注。强制忽略 noqa 会把这 53 处兼容层全报出来，
    # 属于误报。
    #
    # 真正需要"不给豁免通道"的是 F821（未定义名）——那个由 check_undefined
    # 单独用 `--ignore-noqa` 把关（见下）。E9/B 类违规没有合理的 noqa 场景。
    offenders: list[str] = []
    for scope in (AME, ROOT / "tests"):
        code, out = _run([sys.executable, "-m", "ruff", "check", str(scope),
                          "--select", "E9,F,B",
                          "--output-format", "concise"])
        if code != 0:
            offenders += [ln for ln in out.splitlines()
                          if ln.strip() and ":" in ln and ".py:" in ln]
    rep.add("静态正确性（ruff E9/F/B，含 tests）", not offenders,
            "零违规" if not offenders
            else f"{len(offenders)} 处违规，例如：{offenders[0][:100]}")


def check_undefined(rep: Report) -> None:
    code, out = _run([sys.executable, "-m", "ruff", "check", str(AME),
                      "--select", "F821", "--ignore-noqa", "--output-format", "concise"])
    hits = [ln for ln in out.splitlines() if "F821" in ln]
    rep.add("未定义名（F821）", code == 0,
            "零违规" if code == 0
            else (f"{len(hits)} 处：{hits[0][:100]}" if hits
                  else f"ruff 退出码 {code}：{(out or '').strip()[:120]}"))


def module_sizes() -> list[tuple[int, str]]:
    out = []
    for f in AME.rglob("*.py"):
        n = len(f.read_text(encoding="utf-8").splitlines())
        out.append((n, str(f.relative_to(ROOT))))
    return sorted(out, reverse=True)


def check_sizes(rep: Report) -> None:
    sizes = module_sizes()
    over = [(n, p) for n, p in sizes if n > MAX_MODULE_LINES]
    biggest = f"{sizes[0][1]} {sizes[0][0]} 行" if sizes else "—"
    rep.add(f"模块规模（≤{MAX_MODULE_LINES} 行/文件）", not over,
            f"最大 {biggest}" if not over else f"超标：{over[:2]}")


def annotation_coverage() -> tuple[float, float, float, float]:
    """返回 (业务层返回%, 业务层参数%, 全量返回%, 全量参数%)。

    业务层 = 去掉 `ui/` 与纯回调样板的模块。理由：界面方法是 Tk 事件回调
    （返回 None 是契约的一部分），给它们加 `-> None` 不增加任何信息量，
    把这类噪音计入总量会让"注解覆盖率"变成一个没有指导意义的数字。
    真正需要注解的是**数据流的边界**：命令总线、存储、几何、切片、Agent。
    """
    biz = {"tot_fn": 0, "ann_fn": 0, "tot_arg": 0, "ann_arg": 0}
    all_ = {"tot_fn": 0, "ann_fn": 0, "tot_arg": 0, "ann_arg": 0}

    def _is_stub(node) -> bool:
        """空实现（只有 pass / ... / docstring / raise NotImplementedError）。

        为什么排除：审计实测追加 120 个
        `def _stubN(a:int,b:int,c:int)->None: pass` 就能把参数注解覆盖率
        从 74.5% 刷到 80.5%。这类函数不承载任何数据流，把它们计入分母
        等于"用注解填空函数来达标"，指标失去指导意义。
        """
        body = [n for n in node.body
                if not (isinstance(n, ast.Expr)
                        and isinstance(n.value, ast.Constant)
                        and isinstance(n.value.value, str))]   # 去 docstring
        if len(body) == 1:
            n = body[0]
            if isinstance(n, ast.Pass):
                return True
            if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and n.value.value is Ellipsis):
                return True
            if (isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                    and getattr(n.exc.func, "id", "") == "NotImplementedError"):
                return True
        return False

    def tally(node, box):
        if _is_stub(node):
            return                      # 空实现不计入分母（见 _is_stub）
        box["tot_fn"] += 1
        if node.returns is not None:
            box["ann_fn"] += 1
        for a in list(node.args.args) + list(node.args.kwonlyargs):
            if a.arg in ("self", "cls"):
                continue
            box["tot_arg"] += 1
            if a.annotation is not None:
                box["ann_arg"] += 1

    for f in AME.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        is_ui = "ui" in f.relative_to(AME).parts
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                tally(node, all_)
                if not is_ui:
                    tally(node, biz)

    def pct(box, k):
        return 100.0 * box["ann_" + k] / box["tot_" + k] if box["tot_" + k] else 0.0

    return pct(biz, "fn"), pct(biz, "arg"), pct(all_, "fn"), pct(all_, "arg")


def check_annotations(rep: Report) -> None:
    fn_pct, arg_pct, all_fn, all_arg = annotation_coverage()
    rep.add(f"业务层返回注解（≥{MIN_RETURN_ANN_PCT:.0f}%）",
            fn_pct >= MIN_RETURN_ANN_PCT, f"{fn_pct:.1f}%（全量 {all_fn:.1f}%）")
    rep.add(f"业务层参数注解（≥{MIN_PARAM_ANN_PCT:.0f}%）",
            arg_pct >= MIN_PARAM_ANN_PCT, f"{arg_pct:.1f}%（全量 {all_arg:.1f}%）")


def check_tests(rep: Report, coverage: bool) -> None:
    cmd = [sys.executable, "-m", "pytest", "-q", "-m", "not perf"]
    if coverage:
        cmd += ["--cov=ame", "--cov-report=term"]
    code, out = _run(cmd)
    tail = [ln for ln in out.splitlines() if "passed" in ln or "failed" in ln]
    detail = tail[-1] if tail else "(无测试输出)"
    rep.add("自动化测试", code == 0, detail)
    if coverage:
        cov = [ln for ln in out.splitlines() if ln.startswith("TOTAL")]
        if cov:
            rep.add("覆盖率报告", True, cov[-1])


def check_docs_synced(rep: Report) -> None:
    """文档里的测试数字必须与代码实际一致。

    这条检查针对一个真实发生过的问题：验收报告写着 458 和 552，实际已是 655。
    文档说错数字比不写更糟——评委按文档核对时会对不上，进而怀疑其它结论。
    """
    import importlib
    mod = importlib.import_module("sync_docs")
    total, per = mod.collect_counts()
    if not total:
        rep.add("文档测试数字", False, "pytest 收集失败")
        return
    stale: list[str] = []
    for d in mod.DOCS:
        path = ROOT / d
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        # 抽查：文档里出现的"N 项测试全部通过"必须等于实际
        for m in re.finditer(r"(\d{3,4}) 项测试全部通过", text):
            if int(m.group(1)) != total:
                stale.append(f"{d} 写 {m.group(1)}，实际 {total}")
        # 表格合计
        for m in re.finditer(r"\| \*\*合计\*\* \| \*\*(\d+)\*\*", text):
            if int(m.group(1)) != total:
                stale.append(f"{d} 合计 {m.group(1)}，实际 {total}")
    rep.add("文档测试数字一致", not stale,
            f"共 {total} 项" if not stale else "；".join(stale[:3]))


def check_oss_materials(rep: Report) -> None:
    """赛题要求的开源材料必须齐备。

    官方原文："建议含项目源码、依赖环境、部署指导、使用文档、
    开源许可证说明、Demo 截图或演示视频链接"。
    国赛提交材料明确列了 "readme 复现步骤"——缺 README 或 LICENSE
    会直接扣"完整性与可展示性"的分。
    """
    required = {
        "README.md": "项目说明与复现步骤",
        "LICENSE": "开源许可证说明",
        "requirements-cloud.txt": "云上依赖环境",
        "Dockerfile": "部署指导（容器化）",
        "用户操作说明.md": "使用文档",
    }
    missing = [f"{k}（{v}）" for k, v in required.items()
               if not (ROOT / k).exists()]
    detail = "五项齐备" if not missing else "缺：" + "、".join(missing)
    rep.add("开源材料齐备", not missing, detail)

    # README 必须含复现命令（组委会按材料复现）
    readme = ROOT / "README.md"
    if readme.exists():
        body = readme.read_text("utf-8")
        has_repro = "pytest" in body and "--selftest" in body
        rep.add("README 含复现步骤", has_repro,
                "含 pytest 与 selftest 命令" if has_repro else "缺少可执行复现命令")


def check_selftest(rep: Report) -> None:
    code, out = _run([sys.executable, "-m", "ame", "--selftest"])
    last = [ln for ln in out.splitlines() if ln.startswith("结果") or "失败" in ln]
    rep.add("源码自检（11 步）", code == 0, last[-1] if last else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AME 质量门禁")
    ap.add_argument("--fast", action="store_true", help="跳过测试与自检")
    ap.add_argument("--coverage", action="store_true", help="附带覆盖率")
    ap.add_argument("--full", action="store_true", help="附加源码自检")
    args = ap.parse_args(argv)

    print("AME 质量门禁")
    print("=" * 60)
    rep = Report()
    check_ruff(rep)
    check_undefined(rep)
    check_sizes(rep)
    check_annotations(rep)
    check_docs_synced(rep)
    check_oss_materials(rep)
    if not args.fast:
        check_tests(rep, args.coverage)
        if args.full:
            check_selftest(rep)

    print("=" * 60)
    print(f"结果：{rep.summary()} —— " + ("通过" if rep.ok else "未通过"))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
