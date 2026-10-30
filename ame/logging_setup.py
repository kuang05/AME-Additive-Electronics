"""运行日志（NFR-006：关键路径有日志，便于排查与审计）。

设计取舍
--------
- **标准库 logging**，不用自研：Windows 打包后仍可用，无额外依赖。
- **文件轮转**（1 MB × 3）：日志不能无限增长拖垮数据目录（NFR-002 同一原则）。
- **默认写到 <数据目录>/logs/ame.log**：与工程数据同处，便于用户打包带走。
- **默认 INFO**：记录打开工程、导入、切片、打印、错误；DEBUG 才记录每条命令。
- **绝不记密钥**：只记工程 id、文件哈希、耗时、结果，不记路径以外的个人内容。

用法
----
    from ame.logging_setup import get_logger, configure
    configure(data_dir)                # 启动时调用一次
    log = get_logger(__name__)
    log.info("导入 STEP: %s faces=%d", name, n)
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "ame"
_configured_for: Path | None = None


def _level(name: str) -> int:
    return {
        "debug": logging.DEBUG, "info": logging.INFO,
        "warning": logging.WARNING, "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }.get(str(name).lower(), logging.INFO)


def configure(data_dir: str | Path | None = None, level: str = "info") -> Path | None:
    """配置根 logger。返回日志文件路径；无法写文件时退化为仅控制台。"""
    global _configured_for
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(_level(level))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    # 控制台：打包后是窗口程序，没有 stderr，加不上也不报错
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    except Exception:
        pass

    path = None
    if data_dir is not None:
        try:
            log_dir = Path(data_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / "ame.log"
            fh = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3,
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            _configured_for = Path(data_dir)
        except OSError:
            path = None          # 只读介质等情况下退回控制台
    return path


def get_logger(name: str = "") -> logging.Logger:
    """取 ame 命名空间下的 logger（使用前需 configure 一次）。"""
    if not name or name == "__main__":
        return logging.getLogger(_LOGGER_NAME)
    if name.startswith(_LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def log_path(data_dir: str | Path | None) -> Path | None:
    """当前日志文件路径（供"帮助 → 打开日志目录"使用）。"""
    if data_dir is None:
        return None
    p = Path(data_dir) / "logs" / "ame.log"
    return p if p.exists() else None


__all__ = ["configure", "get_logger", "log_path"]
