"""增材电子一体化软件（AME）核心包。

四域：结构 / 走线 / 切片 / 打印 + 命令账本 + 智能体。
设计原则（需求 §1.2/§4.2）：
- 命令总线是唯一写入口，人与 Agent 同源（AGT-009）；
- 大文件按内容寻址（哈希）存储；
- 一切与 G-code 相关的输出必须确定可重放（AGT-008 / SLC-008）。
"""
__version__ = "1.0.0"

import hashlib
import json
import uuid


def new_id() -> str:
    return str(uuid.uuid4())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canon_json(obj) -> str:
    """确定性 JSON：键排序 + 紧凑分隔，用于内容哈希与 diff。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj) -> str:
    """任意可 JSON 化对象的内容摘要。"""
    return sha256_text(canon_json(obj))


TOOL_VERSIONS = {"ame": __version__}
