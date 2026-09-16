"""评论子图装配。

链路：
```
scan_comments → match_keywords → identify_song → draft_reply
              → persist_drafts → notify_reviews → human_review(interrupt) → apply_decisions
```

`notify_reviews`（选做的「发送提醒」）排在 `persist_drafts` 之后、`human_review` 之前：
提醒里要带拟回复，所以必须在落表之后；而 `interrupt()` 一挂起后面的节点就不跑了，
所以必须在挂起之前。顺序错一头，提醒就永远发不出去。

⚠ `persist_drafts` 排在 `human_review` **之前**：`interrupt()` 一挂起，后面的节点就不跑了。
如果落表放在确认之后，挂起期间《评论命中表》里没有记录，前端「待确认」列表是空的，
人工无从确认——整个环节会死锁。所以：先落表（status=pending），挂起，确认后只更新状态。

用**自己的 thread_id**（形如 `{run_id}:{video_id}`），和主图共用 checkpointer。
之所以能和主图解耦，是因为主图对它是 fire-and-forget——主图不等它返回。

挂起态的 run 是正常状态，不是异常。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from app.graph.state import CommentState
from app.nodes.comments.draft import make_draft_reply
from app.nodes.comments.keywords import make_match_keywords
from app.nodes.comments.review import (
    make_apply_decisions,
    make_human_review,
    make_notify_reviews,
    make_persist_drafts,
)
from app.nodes.comments.scan import make_scan_comments
from app.nodes.comments.song import make_identify_song

log = logging.getLogger(__name__)


def build_comment_graph(deps) -> object:
    graph = StateGraph(CommentState)

    graph.add_node("scan_comments", make_scan_comments(deps.registry))
    graph.add_node("match_keywords", make_match_keywords())
    graph.add_node("identify_song", make_identify_song())
    graph.add_node("draft_reply", make_draft_reply(deps.settings))
    graph.add_node("persist_drafts", make_persist_drafts(deps.storage))
    graph.add_node("notify_reviews", make_notify_reviews(deps.storage, deps.notifier))
    graph.add_node("human_review", make_human_review())
    graph.add_node("apply_decisions", make_apply_decisions(deps.storage))

    graph.add_edge(START, "scan_comments")
    graph.add_edge("scan_comments", "match_keywords")
    graph.add_edge("match_keywords", "identify_song")
    graph.add_edge("identify_song", "draft_reply")
    graph.add_edge("draft_reply", "persist_drafts")
    graph.add_edge("persist_drafts", "notify_reviews")
    graph.add_edge("notify_reviews", "human_review")
    graph.add_edge("human_review", "apply_decisions")
    graph.add_edge("apply_decisions", END)

    return graph.compile(checkpointer=deps.checkpointer)


def make_comment_runner(graph) -> Callable[[str, dict, str], Awaitable[None]]:
    """生成评论子图的「发射器」。

    它被 run_comment_graph 节点用 `asyncio.create_task` 起在后台，
    所以这里**不能抛异常**——后台任务的异常没人接，只会打日志。
    """

    async def run_comment_subgraph(thread_id: str, video: dict, run_id: str) -> None:
        config = {"configurable": {"thread_id": thread_id}}
        payload = {
            "thread_id": thread_id,
            "run_id": run_id,
            "video": video,
            "keywords": video.get("keywords", []),
        }
        try:
            result = await graph.ainvoke(payload, config)
            interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
            if interrupts:
                log.info(
                    "评论子图已挂起等人工确认：thread_id=%s，待确认 %d 条",
                    thread_id, len((interrupts[0].value or {}).get("drafts", [])),
                )
            else:
                log.info("评论子图跑完（无待确认项）：thread_id=%s", thread_id)
        except Exception:  # noqa: BLE001
            log.exception("评论子图异常：thread_id=%s", thread_id)

    return run_comment_subgraph
