"""带退避与抖动的重试。外部请求（provider、飞书）统一走这层。"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Iterable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: bool = True,
    exceptions: Iterable[type[BaseException]] = (Exception,),
    label: str = "task",
) -> T:
    """执行 fn，失败按指数退避重试；最终仍失败则抛出最后一次异常。"""
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn()
        except tuple(exceptions) as exc:  # type: ignore[misc]
            last = exc
            if attempt >= max_attempts:
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            if jitter:
                delay *= 0.5 + random.random()
            log.warning("%s 第 %d/%d 次失败：%s，%.1fs 后重试", label, attempt, max_attempts, exc, delay)
            await asyncio.sleep(delay)
    assert last is not None
    raise last
