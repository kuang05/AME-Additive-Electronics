"""检查源码是否能在 Python 3.9（910B 节点的默认解释器）上导入运行。

两个层次：
1. 语法层：用 `ast.parse(feature_version=(3, 9))`，匹配语句 / match-case 等
   只有 3.10+ 才有的语法会直接报错。
2. 运行期层：注解位置以外的 `X | Y`（PEP 604）在 3.9 会 TypeError；
   `from __future__ import annotations` 只对注解生效，对
   `isinstance(x, int | str)` / 默认值 / 模块级常量无效。

用法：python check_py39.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _has_future_annotations(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(a.name == "annotations" for a in node.names):
                return True
    return False


class _AnnotationCollector(ast.NodeVisitor):
    """收集所有"注解位置"的节点 id，用于把注解里的 X|Y 排除掉。"""

    def __init__(self) -> None:
        self.ids: set[int] = set()

    def _mark(self, node) -> None:
        if node is None:
            return
        for sub in ast.walk(node):
            self.ids.add(id(sub))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._mark(node.returns)
        for a in (list(node.args.args) + list(node.args.kwonlyargs)
                  + list(node.args.posonlyargs)):
            self._mark(a.annotation)
        self._mark(node.args.vararg.annotation if node.args.vararg else None)
        self._mark(node.args.kwarg.annotation if node.args.kwarg else None)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._mark(node.annotation)
        self.generic_visit(node)


def _looks_like_type_union(node: ast.BinOp) -> bool:
    """区分"类型联合"和"位运算或"。

    这条 lint 只在拿不准时保守报出，所以用一组很窄的判据：
    操作数是类名（首字母大写且不止一个字符）、`None`、或泛型下标
    （`list[str] | None`）。`re.I | re.M` 这种单字母常量掩码不会被误报。
    """
    def typeish(n) -> bool:
        if isinstance(n, ast.Constant):
            return n.value is None
        if isinstance(n, ast.Subscript):
            return True
        if isinstance(n, ast.Name):
            return n.id[0].isupper() and len(n.id) > 1
        if isinstance(n, ast.Attribute):
            return n.attr[0].isupper() and len(n.attr) > 1
        return False

    return typeish(node.left) or typeish(node.right)


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path), feature_version=(3, 9))
    except SyntaxError as e:
        return [f"{path}: 语法不被 3.9 接受：第 {e.lineno} 行 {e.msg}"]
    future = _has_future_annotations(tree)
    ann = _AnnotationCollector()
    ann.visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if future and id(node) in ann.ids:
                continue
            if _looks_like_type_union(node):
                problems.append(
                    f"{path}: 第 {node.lineno} 行 运行期用了 PEP 604 的 `X | Y`"
                    "（3.9 会 TypeError）")
    return problems


def main() -> int:
    print(f"Python {sys.version.split()[0]} 上做 3.9 兼容静态检查")
    files = sorted(ROOT.rglob("*.py"))
    files = [f for f in files
             if "__pycache__" not in f.parts and "build" not in f.parts
             and "dist" not in f.parts and "_snapshots" not in f.parts]
    all_problems: list[str] = []
    for f in files:
        all_problems += check_file(f)
    print(f"扫描 {len(files)} 个文件")
    for p in all_problems[:40]:
        print("  " + p)
    print("结果：" + ("通过" if not all_problems else f"{len(all_problems)} 处问题"))
    return 0 if not all_problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
