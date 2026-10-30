# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（AME 增材电子一体化软件）。

构建：
    pyinstaller ame.spec --noconfirm

产物：
    dist/AME/AME.exe            单目录版（启动快，推荐分发）
    dist/AME-<version>.zip      由 build_package.py 压缩

要点：
- 隐藏导入 shapely / numpy 的动态子模块（PyInstaller 静态分析抓不到）；
- schema.sql 与样例 STEP/板文件作为数据文件随包；
- 排除体积大且未使用的库（matplotlib / PyQt / IPython 等），控制体积；
- 控制台窗口关闭（GUI 程序），但保留 --debug 时可开。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH)  # noqa: F821  (PyInstaller 注入)

# ---- 数据文件：数据库 schema、样例工程、AI 权重 ----
datas = [
    (str(ROOT / "ame" / "db" / "schema.sql"), "ame/db"),
    # PG 建表脚本：云上形态（换 PgStore）时需要，缺了会 FileNotFoundError
    (str(ROOT / "ame" / "db" / "schema_pg.sql"), "ame/db"),
]
# AI 收益排序的随包权重：缺失时运行期自动降级到规则先验（AGT-010），
# 但比赛版要求"本地模型可用"，因此必须打进包内。
ai_weights = ROOT / "ame" / "ai" / "weights.json"
if ai_weights.exists():
    datas.append((str(ai_weights), "ame/ai"))
samples = ROOT / "samples"
if samples.exists():
    for sub in ("data",):
        p = samples / sub
        if p.exists():
            datas.append((str(p), f"samples/{sub}"))
    # 样例生成器与演示脚本（用户可重新生成样例）
    for name in ("gen_samples.py", "make_demo_project.py", "__init__.py"):
        f = samples / name
        if f.exists():
            datas.append((str(f), "samples"))

# ---- 隐藏导入：shapely 的 GEOS 绑定与 numpy 子模块 ----
hiddenimports = []
hiddenimports += collect_submodules("shapely")
hiddenimports += collect_submodules("numpy")
hiddenimports += [
    "ame.ui.app", "ame.ui.render", "ame.ui.theme",
    "ame.ui._shell", "ame.ui._canvas",
    # 各域 mixin 由 app.py 显式导入，但 PyInstaller 对多重继承的类发现较弱，
    # 逐一列出防止产物里缺某个域（缺一个就是整块功能区消失）。
    "ame.ui.domains", "ame.ui.domains.structure", "ame.ui.domains.circuit",
    "ame.ui.domains.mrc", "ame.ui.domains.slicing",
    "ame.ui.domains.printing", "ame.ui.domains.agent",
    "ame.ui.domains.project",
    "ame.ai", "ame.ai.features", "ame.ai.local", "ame.ai.cloud",
    "ame.ai.service", "ame.ai.train",
    "ame.geometry.vec3", "ame.geometry.types", "ame.geometry.syntax",
    "ame.geometry.assembly",
    "ame.ledger.errors", "ame.ledger.commands",
    "ame.db.migrations", "ame.db.storage", "ame.db.pg_store", "ame.db.cache",
    "ame.slicing.raster", "ame.circuit.routing",
    "ame.__main__",
    "sqlite3", "tkinter", "tkinter.ttk", "tkinter.filedialog",
    "tkinter.messagebox", "tkinter.simpledialog",
]

# ---- 二进制：shapely 自带的 GEOS DLL ----
binaries = collect_dynamic_libs("shapely")

# ---- 排除：明显用不到的大件 ----
excludes = [
    # 未使用的重型分析/图形库
    "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "jupyter", "notebook", "pandas", "scipy",
    # 打包工具链本身
    "pytest", "sphinx", "setuptools", "pip", "wheel",
    "test", "unittest", "pydoc", "doctest",
    # 实测未使用的可选依赖（体积占比最大：llvmlite 70 MB 等）
    # 注意：PIL/Pillow **不能**排除——三维渲染用 PIL 光栅化后贴图，
    # 比逐多边形 create_polygon 快一个数量级（实测 209 fps vs 14 fps），
    # 是 STR-003「20 万面 30 fps」达标的关键路径。缺失时会回退到多边形绘制。
    "llvmlite", "numba",
    "cryptography", "lxml", "markupsafe", "jinja2",
    "win32", "win32api", "win32con", "pywin32",
    "pygments", "colorama", "certifi", "urllib3", "requests",
    "pkg_resources", "importlib_metadata", "zipp",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "ame" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AME",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # UPX 易触发杀软误报，且对 numpy 大 dll 收益低
    console=False,          # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# 诊断版（带控制台）：AME-selftest.exe --selftest 可在打包产物上跑完整链路，
# 解决 GUI 程序无输出、无法自动验证的问题。
exe_cli = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AME-selftest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AME",
)
