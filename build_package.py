"""一键打包：构建 → 校验 → 压缩发布包。

用法：
    python build_package.py            # 完整构建
    python build_package.py --skip-build   # 只压缩已有 dist/AME

产物：
    dist/AME/                    单目录版（可直接运行 AME.exe）
    dist/AME-<版本>-win64.zip    发布压缩包（含说明）
"""
from __future__ import annotations

import shutil
import subprocess
import time
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
APP_DIR = DIST / "AME"
EXE = APP_DIR / "AME.exe"

README = """增材电子一体化软件 AME — 使用说明
========================================

一、运行
--------
双击 AME.exe 启动。
如需指定数据目录（工程库与模型文件存放位置）：
    在命令行执行  AME.exe D:\\ame-data
不指定时默认放在  %USERPROFILE%\\.ame

首次启动会显示空界面，按以下顺序操作：
  1. 工程 → 新建工程
  2. 左栏「① 结构域」→ 导入 STEP
  3. 左栏「② 走线域」→ 导入板文件
  4. 左栏「③ 可打性」→ 运行检查并评分
  5. 左栏「④ 切片」→ 切片 → 导出 G-code
  6. 左栏「⑤ 打印」→ 连接虚拟打印机 → 开始打印

二、系统要求
------------
  Windows 10/11 64 位
  内存 8 GB 以上（推荐 16 GB）
  集显可运行；分辨率 1920×1080 及以上
  核心功能完全离线，不需要网络

三、界面速查
------------
  三维视图：左键拖拽旋转 / 右键拖拽平移 / 滚轮缩放 / Ctrl+F 适配窗口
  问题列表：点「对象」列可定位，点「解释」看原因与建议
  底栏：显示当前道次、物理层、材料与 Z 高度
  快捷键：Ctrl+Z 撤销 / Ctrl+Y 重做 / Ctrl+S 提交版本
          Delete 删除选中走线 / Tab 切换三维二维 / Esc 取消工具

四、数据与备份
--------------
  数据目录，存放工程库：
    <数据目录>/projects/<工程ID>.db
  模型与 G-code 按内容哈希存放：
    <数据目录>/blobs/ab/cd/<sha256>
  工程 → 导出工程包  可生成单个 .amepkg 文件，换机器后导入继续编辑。

五、已知限制
------------
  · 布尔运算只做预览与特征登记，不执行几何布尔
  · 布线在二维俯视视图进行，三维视图只做显示与校验
  · STEP 只精确解析平面与柱面；圆锥/球/环面/样条面的面积按包围盒估算，
    导入时会给出 G-SURFACE-UNSUPPORTED 诊断
  · 首版仅虚拟打印，真机接口已预留

六、问题排查
------------
  启动无反应：确认系统为 64 位 Windows 10/11
  导入 STEP 失败：查看状态栏诊断码与中文说明（如 G-OPEN-SHELL 表示壳体不闭合）
  界面卡顿：大模型会自动降精度显示；切片在后台执行，底栏可点「取消」
"""


def build() -> None:
    print("=" * 60)
    print("构建 AME …")
    print("=" * 60)
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "ame.spec",
                        "--noconfirm", "--clean"], cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"构建失败，退出码 {r.returncode}")


def verify() -> None:
    """产物校验：EXE 存在、关键依赖齐备、体积合理。"""
    print("\n" + "=" * 60)
    print("校验产物 …")
    print("=" * 60)
    if not EXE.exists():
        raise SystemExit(f"缺少可执行文件：{EXE}")
    internal = APP_DIR / "_internal"
    required = ["shapely", "numpy", "sqlite3.dll", "_tkinter.pyd",
                "ame/db/schema.sql", "ame/db/schema_pg.sql"]
    missing = [r for r in required if not (internal / r).exists()]
    if missing:
        raise SystemExit(f"打包缺少关键文件：{missing}")
    size_mb = sum(p.stat().st_size for p in APP_DIR.rglob("*") if p.is_file()) / 1024 / 1024
    print(f"  AME.exe      {EXE.stat().st_size / 1024 / 1024:.1f} MB")
    print(f"  整体体积      {size_mb:.0f} MB")
    print(f"  文件数        {sum(1 for p in APP_DIR.rglob('*') if p.is_file())}")
    # PIL 不在此列：三维渲染的 PIL 光栅化是 STR-003「20 万面 30 fps」的
    # 关键路径（209 fps vs 多边形回退 14 fps），ame.spec 也明确保留它。
    banned = ["llvmlite", "matplotlib", "scipy", "pandas"]
    leaked = [b for b in banned if (internal / b).exists()]
    if leaked:
        print(f"  ⚠ 仍包含未使用的大库：{leaked}（可在 ame.spec 的 excludes 中排除）")
    print("  ✓ 关键依赖齐备")


def package() -> Path:
    print("\n" + "=" * 60)
    print("生成发布包 …")
    print("=" * 60)
    version = "1.0.0"
    try:
        sys.path.insert(0, str(ROOT))
        import ame
        version = ame.__version__
    except Exception:
        pass
    zip_path = DIST / f"AME-{version}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()
    # 把使用说明放进发布包根目录
    readme_path = APP_DIR / "使用说明.txt"
    readme_path.write_text(README, encoding="utf-8-sig")
    # PyInstaller 刚退出时可能还有句柄未释放（曾见 compressobj 报
    # "Can't close the ZIP file while there is an open writing handle"）。
    # 失败一次就等 1 s 重试，最多 3 次，并把半成品删掉。
    files = sorted(p for p in APP_DIR.rglob("*") if p.is_file())
    last: Exception | None = None
    for attempt in range(3):
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as z:
                for p in files:
                    z.write(p, Path("AME") / p.relative_to(APP_DIR))
            break
        except Exception as exc:                     # noqa: BLE001
            last = exc
            zip_path.unlink(missing_ok=True)
            print(f"  压缩失败（第 {attempt + 1} 次）：{exc}；1 s 后重试")
            time.sleep(1.0)
    else:
        raise SystemExit(f"压缩失败：{last}")
    print(f"  {zip_path.name}  {zip_path.stat().st_size / 1024 / 1024:.0f} MB")
    print(f"  路径：{zip_path}")
    return zip_path


def main() -> int:
    if "--skip-build" not in sys.argv:
        build()
    verify()
    zip_path = package()
    print("\n" + "=" * 60)
    print("完成")
    print("=" * 60)
    print(f"  绿色版目录：{APP_DIR}")
    print(f"  直接运行：  {EXE}")
    print(f"  发布压缩包：{zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
