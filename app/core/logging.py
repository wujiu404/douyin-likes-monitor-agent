"""结构化日志。

每条日志自动带上当前 `run_id`，所以「一个 run 的完整轨迹可以 grep 出来」——
这是可观测性契约的一部分，也要求 run 必须能收敛（见 nodes/comment_trigger.py 的 fire-and-forget 说明）。
"""
from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

_run_id: ContextVar[str] = ContextVar("run_id", default="-")


def current_run_id() -> str:
    return _run_id.get()


@contextmanager
def run_context(run_id: str):
    """绑定 run_id 到当前上下文，块内所有日志都会带上它。"""
    token = _run_id.set(run_id)
    try:
        yield run_id
    finally:
        _run_id.reset(token)


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()
        return True


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=CST).strftime("%H:%M:%S")
        rid = getattr(record, "run_id", "-")
        return f"{ts} [{(record.levelname or 'INFO')[:4]}] [{rid}] {record.name}: {record.getMessage()}"


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter())
    handler.addFilter(_RunIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # 第三方库少说话（aiosqlite 在 DEBUG 下会把每条 SQL 都打出来，最吵的一个）
    for noisy in ("apscheduler", "httpx", "httpcore", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
