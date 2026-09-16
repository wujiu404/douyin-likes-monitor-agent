"""并发门控。

方案里的硬性纪律：
- **账号之间可以并发**
- **单账号内必须串行**（避免触发风控）
- 用 `Semaphore` 控住全局并发上限

注意取锁顺序是「先账号锁、后全局信号量」——反过来会让等待同账号的协程也占着全局槽位，
吞吐会明显变差。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Sequence, TypeVar

T = TypeVar("T")


class ConcurrencyGate:
    def __init__(self, total: int = 3) -> None:
        self._total = asyncio.Semaphore(total)
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def account(self, name: str):
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            async with self._total:
                yield


async def run_per_account(
    accounts: Sequence[dict],
    handler: Callable[[dict], Awaitable[T]],
    *,
    total_concurrency: int = 3,
) -> list[T | BaseException]:
    """按账号并发执行，返回与 accounts 等长的结果列表（异常也保留在对应位置）。"""
    gate = ConcurrencyGate(total_concurrency)

    async def _one(acc: dict):
        async with gate.account(acc.get("name") or acc.get("sec_uid", "?")):
            return await handler(acc)

    return await asyncio.gather(*(_one(a) for a in accounts), return_exceptions=True)
