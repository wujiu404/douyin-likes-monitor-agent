"""扫描服务：跑一次监控主图，并把汇总写进《扫描轮次表》。

这是**触发层与编排层之间的唯一入口**——定时任务和 HTTP 手动触发都调它，
保证两条路径走的是完全一样的东西（不会出现「手动能跑、定时跑不通」这类问题）。

并发由一把进程内锁把住：上一轮没跑完就不开下一轮。
这和 APScheduler 的 `max_instances=1` + `coalesce=True` 是同一道保险的两层。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.core.errors import ScanSkipped
from app.core.idempotency import new_run_id
from app.core.logging import run_context
from app.nodes.comment_trigger import drain_background

log = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()


class ScanBusy(RuntimeError):
    """已有扫描在进行中。"""


def is_scanning() -> bool:
    return _scan_lock.locked()


async def run_scan(deps, trigger: str = "manual") -> dict:
    if _scan_lock.locked():
        raise ScanBusy("已有扫描正在进行中，请稍后再试")

    async with _scan_lock:
        run_id = new_run_id()
        started = time.perf_counter()
        duration = 0

        with run_context(run_id):
            log.info("=== 扫描开始（%s）===", trigger)
            await deps.storage.create_round(
                {
                    "run_id": run_id,
                    "trigger_type": trigger,
                    "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )

            final: dict = {}
            error: str | None = None
            try:
                final = await deps.monitor.ainvoke(
                    {"run_id": run_id, "trigger": trigger},
                    {"configurable": {"thread_id": run_id}},
                ) or {}
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                log.exception("扫描失败")
            finally:
                duration = int((time.perf_counter() - started) * 1000)
                threads = final.get("comment_threads", []) or []
                skip = final.get("skip_reason") or ""

                # 主图此刻**已经收敛**（fire-and-forget 保证了这一点，见 comment_trigger.py）。
                # 这里再等一小会儿，只是为了让刚发射的评论子图跑到 interrupt 挂起，
                # 好让本次响应里的「待确认评论数」是准的——前端因此不必轮询。
                # ⚠ 等待发生在**图之外**，主图 run 早已 END，所以不影响
                #   max_instances=1 / 单轮 run 轨迹收敛 / 轮次台账这三件事。
                # 超时有上限（默认 2s），所以子系统卡死时最多拖慢这么久。
                timeout = float(getattr(deps.settings, "comment_drain_timeout", 0) or 0)
                drained = await drain_background(timeout=timeout) if timeout > 0 else 0
                if threads:
                    pending = len(await deps.storage.list_comment_hits(status="pending", limit=1000))
                else:
                    pending = 0

                note = final.get("note") or ""
                if skip:
                    note = f"跳过：{skip}"
                if error:
                    note = f"失败：{error}"

                await deps.storage.finish_round(
                    run_id,
                    account_count=len(final.get("accounts", []) or []),
                    video_count=len(final.get("videos", []) or []),
                    source=final.get("source", "") or "",
                    alert_count=len(final.get("alerts", []) or []),
                    error_count=int(final.get("error_count", 0) or 0) + (1 if error else 0),
                    thread_ids=",".join(threads),
                    note=note,
                )
                log.info("=== 扫描结束（%d ms，收尾评论子图 %d 个）===", duration, drained)

        if error:
            raise RuntimeError(error)
        if skip:
            # 不是错误——但调用方（HTTP / 调度）需要知道本轮什么都没做
            raise ScanSkipped(skip)

        return {
            "run_id": run_id,
            "trigger": trigger,
            "duration_ms": duration,
            "account_count": len(final.get("accounts", []) or []),
            "video_count": len(final.get("videos", []) or []),
            "delta_count": len(final.get("deltas", []) or []),
            "alert_count": len(final.get("alerts", []) or []),
            "source": final.get("source", ""),
            "provider_trace": final.get("provider_trace", []),
            "comment_threads": threads,
            "pending_comments": pending,
            "alert_summary": final.get("alert_summary", {}),
            # 采集失败 / 账号级结论都在这两条里，调用方（看板、自检脚本）不用再自己拼。
            # 没有它们的时候，「扫描返回 200 但 video_count=0」看不出是采集坏了
            # 还是账号真的没新作品——2026-09-16 就是这个问题让人来问「为什么什么都没增加」。
            "error_count": int(final.get("error_count", 0) or 0) + (1 if error else 0),
            "note": note,
        }
