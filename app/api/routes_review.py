"""评论人工确认的独立回口。

⚠ 这是**独立于主图**的入口。主图起评论子图后立刻走到 END（fire-and-forget），
评论确认的结果直接写《评论命中表》，**不回流主图**。

代价：主图 run 的轨迹里不含评论最终结果，要靠 `thread_id` 关联——
所以 `thread_id` 必须和 `run_id` 一起落表。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.deps import Deps, get_deps

log = logging.getLogger(__name__)
router = APIRouter(tags=["评论确认"])


class ReviewPayload(BaseModel):
    decisions: dict[str, str] = Field(
        default_factory=dict,
        description="{comment_id: 'approved' | 'ignored'}",
        examples=[{"a1b2_v01_c03": "approved", "a1b2_v01_c05": "ignored"}],
    )


@router.get("/reviews", summary="评论命中列表")
async def list_reviews(
    status: str | None = "pending", limit: int = 100, deps: Deps = Depends(get_deps)
) -> dict:
    items = await deps.storage.list_comment_hits(status=status or None, limit=limit)
    return {"items": items}


@router.get("/reviews/{thread_id}", summary="某个视频的评论确认详情")
async def get_review(thread_id: str, deps: Deps = Depends(get_deps)) -> dict:
    items = await deps.storage.list_comment_hits(status=None, limit=500)
    mine = [i for i in items if i["thread_id"] == thread_id]
    if not mine:
        raise HTTPException(status_code=404, detail=f"没有 thread_id={thread_id} 的记录")

    suspended = False
    try:
        snapshot = await deps.comment.aget_state({"configurable": {"thread_id": thread_id}})
        suspended = bool(snapshot and snapshot.next)
    except Exception:  # noqa: BLE001 - 拿不到执行态不影响读数据
        log.debug("读取子图执行态失败：%s", thread_id)

    return {"thread_id": thread_id, "suspended": suspended, "items": mine}


@router.post("/reviews/{thread_id}", summary="提交人工确认（会恢复挂起的子图）")
async def submit_review(
    thread_id: str, body: ReviewPayload, deps: Deps = Depends(get_deps)
) -> dict:
    config = {"configurable": {"thread_id": thread_id}}

    snapshot = None
    try:
        snapshot = await deps.comment.aget_state(config)
    except Exception:  # noqa: BLE001
        log.debug("读取子图执行态失败：%s", thread_id)

    existing = await deps.storage.list_comment_hits(status=None, limit=500)
    decided_ids = set(body.decisions.keys())
    known = any(
        i["thread_id"] == thread_id or i["comment_id"] in decided_ids for i in existing
    )
    if snapshot is None and not known:
        raise HTTPException(status_code=404, detail=f"找不到 thread_id={thread_id} 的评论记录")

    suspended = bool(snapshot and snapshot.next)
    if suspended:
        # resume 的值会成为 human_review 里 interrupt() 的返回值，
        # 子图从这里接着往下跑 apply_decisions。
        await deps.comment.ainvoke(Command(resume={"decisions": body.decisions}), config)
        log.info("评论子图已恢复并跑完：thread_id=%s，确认 %d 条", thread_id, len(body.decisions))
    else:
        # 子图已经结束（或本轮没有待确认项），直接把决定写库
        await deps.storage.decide_comment_hits(body.decisions)
        log.info("子图已结束，仅更新确认状态：thread_id=%s", thread_id)

    items = await deps.storage.list_comment_hits(status=None, limit=500)
    return {
        "ok": True,
        "thread_id": thread_id,
        "resumed": suspended,
        # 命中记录按 comment_id 去重（跨轮次复用同一条），所以这里既按 thread_id 也按
        # comment_id 认领——否则从「第二轮的线程」点进来会看到空列表。
        "items": [
            i for i in items
            if i["thread_id"] == thread_id or i["comment_id"] in decided_ids
        ],
    }
