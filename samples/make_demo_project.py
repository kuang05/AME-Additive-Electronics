"""示例工程生成器：一键建立可演示的四域工程（§10.4 交付物 3）。

用法：
    python -m samples.make_demo_project [数据目录]

生成内容（数据目录缺省为 ~/.ame）：
- 工程「T19-A 演示板」：导入结构与板文件，运行 MRC（初始分约 60）
- 提交版本基线
- 打印一份切片与 G-code 哈希（不落打印作业，避免演示前就有缺陷记录）

演示步骤见 用户操作说明.md。
"""
from __future__ import annotations

import sys
from pathlib import Path

from ame.db.store import DataRoot
from ame.ledger.services import do_slice
from ame.ledger.workspace import create_project

DATA = Path(__file__).parent / "data"


def build(data_dir=None, name="T19-A 演示板") -> dict:
    root = DataRoot(data_dir)
    ws = create_project(root, name)
    step = ws.cmd_import_step((DATA / "plate_100x60.step").read_text("utf-8"),
                              "plate_100x60.step")
    board = ws.cmd_import_board((DATA / "board_T19A.kicad_pcb").read_text("utf-8"))
    mrc = ws.run_mrc()
    ws.commit("导入 T19-A 演示板（初始设计）", author="user")
    sl = do_slice(ws)
    return {"project_id": ws.project_id, "data_dir": str(root.data_dir),
            "step_faces": step["face_count"], "comps": board["comps"],
            "pads": board["pads"], "nets": board["nets"], "routes": board["routes"],
            "score": mrc["score"], "issues": len(mrc["issues"]),
            "passes": sl["passes"], "gcode_hash": sl["gcode_hash"]}


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else None
    info = build(out)
    print("示例工程已创建：")
    for k, v in info.items():
        print(f"  {k:12} {v}")
    print("\n启动界面：python -m ame " + info["data_dir"])


if __name__ == "__main__":
    main()
