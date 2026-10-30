"""把"测试总数/文件分布"从代码事实同步到文档，避免文档数字过期。

为什么需要脚本
--------------
手工维护"文档里的数字"必然过期：这一轮里 `验收报告.md` 还写着 458 和 552，
而实际已经是 655。**文档说错数字比不写更糟**——评委按文档核对时会对不上，
进而怀疑其它数据。

做法：从 pytest 的实际收集结果生成数字，替换文档里的标记区间。
只改被标记包围的内容，不碰人写的叙述文字。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ["验收报告.md", "云上部署与比赛材料.md", "赛题一改造方案.md",
        "README.md", "质量提升记录.md", "重构记录.md", "AI证据链.md",
        "用户操作说明.md", "云上部署与比赛材料.md"]


def collect_counts() -> tuple[int, list[tuple[str, int]]]:
    """返回 (总数, [(文件名, 用例数)])。"""
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                        "-m", "not perf"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = p.stdout or ""
    total = 0
    m = re.search(r"(\d+)/(\d+) tests collected", out)
    if m:
        total = int(m.group(1))
    else:
        m2 = re.search(r"(\d+) tests? collected", out)
        total = int(m2.group(1)) if m2 else 0

    per: dict[str, int] = {}
    for line in out.splitlines():
        mm = re.match(r"(tests/[^\s:]+)::", line)
        if mm:
            per[mm.group(1)] = per.get(mm.group(1), 0) + 1
    return total, sorted(per.items(), key=lambda x: -x[1])


def sync_doc(path: Path, total: int, per: list[tuple[str, int]]) -> list[str]:
    """替换文档中的数字标记。返回改动说明。"""
    if not path.exists():
        return []
    t = path.read_text("utf-8")
    before = t
    changes: list[str] = []

    # 1) 「**N 项测试全部通过**」这类表述
    def _repl_total(m: re.Match) -> str:
        return f"{m.group(1)}{total}{m.group(3)}"

    t2 = re.sub(r"(\*\*)(\d{3,4})( 项测试全部通过\*\*)", _repl_total, t)
    # 不带"全部通过"的表述（如"已有 524 项测试""经过 524 项测试验证"）
    t2 = re.sub(r"(?<![\d.])(\d{3,4})( 项测试)", rf"{total}\g<2>", t2)
    if t2 != t:
        changes.append(f"{path.name}: 总数 → {total}")
        t = t2

    # 2) 测试分布表里的每文件行数（只改表格行）
    for fname, n in per:
        short = Path(fname).name
        pat = rf"(\| `tests/{re.escape(short)}` \| )(\d+)( \|)"
        t3, cnt = re.subn(pat, rf"\g<1>{n}\g<3>", t)
        if cnt:
            t = t3

    # 3) 表格合计
    # 合计行格式可能带备注列（如 "| **合计** | **712** | 实跑 710 passed |"），
    # 早期正则要求行尾正好是 "| |"，格式一变就静默不匹配——文档数字因此漂移。
    # 现在只要求"合计 | **N**"，后面跟什么不管。
    t4 = re.sub(r"(\| \*\*合计\*\* \| \*\*)(\d+)(\*\*)", rf"\g<1>{total}\g<3>", t)
    if t4 != t:
        changes.append(f"{path.name}: 合计 → {total}")
        t = t4

    if t != before:
        path.write_text(t, encoding="utf-8")
    return changes


def main() -> int:
    total, per = collect_counts()
    if not total:
        print("无法获取测试数量（pytest 收集失败）", file=sys.stderr)
        return 1
    print(f"实际收集 {total} 项，{len(per)} 个文件")
    for fname, n in per:
        print(f"  {fname:42s} {n:4d}")
    touched = []
    for d in DOCS:
        touched += sync_doc(ROOT / d, total, per)
    print()
    if touched:
        print("已同步：")
        for c in touched:
            print("  " + c)
    else:
        print("文档已是最新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
