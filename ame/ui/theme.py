"""界面配色与字体常量（app.py 与各 mixin 共用）。

从 `app.py` 顶部拆出：颜色值原先散落在主文件里，画布、面板、对话框都要引用。
集中一处后调色只需改这一个文件，也避免 mixin 之间循环导入。
"""
from __future__ import annotations

BG = "#1e2227"
PANEL = "#272c33"
ACCENT = "#4a9eff"
TEXT = "#e6e8ea"
MUTED = "#98a2ad"
WARN = "#f0a84b"
BAD = "#ef5f5f"
GOOD = "#5cc98a"

# 画布专用
CANVAS_BG = "#15181c"
GRID = "#232830"
ROUTE = "#d0a85c"
PAD = "#c8b273"

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_MONO = ("Consolas", 10)

__all__ = ["BG", "PANEL", "ACCENT", "TEXT", "MUTED", "WARN", "BAD", "GOOD",
           "CANVAS_BG", "GRID", "ROUTE", "PAD", "FONT_UI", "FONT_MONO"]
