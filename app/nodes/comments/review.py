"""评论子图的确认、提醒与落表。

顺序是刻意的：

```
draft_reply → persist_drafts → notify_reviews → human_review(interrupt) → apply_decisions → END
              ↑                 ↑                 ↑                        ↑
         先把记录写进去     再推提醒        在这里挂起等人工          确认后只更新状态
```

**为什么 `persist_drafts` 必须在 `human_review` 之前**：
`interrupt()` 会把 run 停在这一步，后面的节点都不执行。如果落表放在确认之后，
那么挂起期间《评论命中表》里根本没有这条记录，前端的「待确认」列表会是空的——
人工无从确认，整个环节死锁。所以先落表（状态 `pending`），挂起，确认后再只更新状态。

**为什么 `notify_reviews` 排在落表之后、挂起之前**：
选做的要求是「识别评论 → 生成拟回复 → **发送提醒**」。提醒要带上拟回复，
而拟回复此刻已经落表；排在挂起之后就永远不会执行（interrupt 一停，后面全不跑）。

副作用：
- `persist_drafts` —— 写《评论命中表》。幂等键 `comment:{comment_id}`（跨轮次只落一次）。
- `notify_reviews` —— 推提醒。幂等键用 `alert_log` 的 `(comment_review, comment_id, channel)` 唯一键。
- `apply_decisions` —— 更新《评论命中表》状态。

    合规红线：这三个节点都**只碰自己的数据库/告警渠道**，不调用抖音的评论发布接口，
    任何情况下都不。拟回复只落表 + 推提醒，发不发由人决定。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from langgraph.types import interrupt

from app.core.idempotency import comment_key
from app.notifiers.base import Notifier
from app.storage.base import StorageProvider

log = logging.getLogger(__name__)

# 评论提醒的幂等命名空间。
# 用**固定串而不是 run_id**：同一条评论只在第一次被扑到时提醒一次，
# 之后每轮再扫到都不重复打扰（run_id 每轮都变，用它当键就会轮轮重推）。
REVIEW_NAMESPACE = "comment_review"


def make_persist_drafts(storage: StorageProvider):
    """在挂起**之前**落表，让「待确认」这件事在数据里看得见。"""

    async def persist_drafts(state: dict) -> dict:
        thread_id = state.get("thread_id", "")
        video = state.get("video") or {}
        song = state.get("song") or {}
        hits = state.get("hits") or []
        drafts = state.get("drafts") or []

        draft_by_id = {d.get("comment_id", ""): d for d in drafts}
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        rows: list[dict] = []
        for h in hits:
            cid = h.get("comment_id", "")
            rows.append(
                {
                    "idem_key": comment_key(cid),
                    "thread_id": thread_id,
                    "run_id": state.get("run_id", ""),
                    "video_id": video.get("video_id", ""),
                    "account": video.get("account", ""),
                    "comment_id": cid,
                    "content": h.get("content", ""),
                    "comment_time": h.get("comment_time", ""),
                    "keywords": ",".join(h.get("keywords", []) or []),
                    "song_title": song.get("title", ""),
                    "song_artist": song.get("artist", ""),
                    "draft": draft_by_id.get(cid, {}).get("draft", ""),
                    "created_at": created_at,
                }
            )

        written = await storage.upsert_comment_hits(rows)
        log.info(
            "评论命中落表：本轮命中 %d 条，其中 %d 条是新记录（status=pending）",
            len(rows), written,
        )
        return {"persisted": written}

    return persist_drafts


def make_notify_reviews(storage: StorageProvider, notifier: Notifier):
    """把「有待确认的拟回复」这件事推给人工。

    没有这一步，整条评论链路就只有「落表」没有「通知」——用户得自己盯着看板刷新，
    等于把「主动提醒」退化成了「被动查询」。选做要求的最后一步正是它。
    """

    async def notify_reviews(state: dict) -> dict:
        thread_id = state.get("thread_id", "")
        video = state.get("video") or {}
        song = state.get("song") or {}
        hits = state.get("hits") or []
        drafts = state.get("drafts") or []

        if not hits:
            return {"notified": 0}

        draft_by_id = {d.get("comment_id", ""): d for d in drafts}
        fresh: list[dict] = []
        for h in hits:
            cid = h.get("comment_id", "")
            item = {
                "comment_id": cid,
                "content": h.get("content", ""),
                "keywords": h.get("keywords", []) or [],
                "draft": draft_by_id.get(cid, {}).get("draft", ""),
                "video_id": video.get("video_id", ""),
                "account": video.get("account", ""),
                "video_title": video.get("title", ""),
                "song_title": song.get("title", ""),
                "song_artist": song.get("artist", ""),
                "thread_id": thread_id,
            }
            # 抢一次唯一键：抢到才是「这条评论第一次被提醒」
            if await storage.try_log_alert(
                REVIEW_NAMESPACE, cid, notifier.name,
                json.dumps(item, ensure_ascii=False), "review",
            ):
                fresh.append(item)

        deduped = len(hits) - len(fresh)
        if not fresh:
            log.info("本轮 %d 条评论提醒已推送过，幂等拦截", deduped)
            return {"notified": 0}

        sent = await notifier.send_reviews(thread_id, fresh)
        log.info(
            "推送 %d 条评论提醒（渠道=%s，成功=%s，幂等拦截 %d）",
            len(fresh), notifier.name, sent, deduped,
        )
        return {"notified": len(fresh) if sent else 0}

    return notify_reviews


def make_human_review():
    """用 `interrupt()` 挂起等人工确认。

    这是 LangGraph 在本项目里**唯一真正不可替代**的地方：原生支持
    「暂停 → 等外部输入 → 恢复」，自己手写要造一套状态机加持久化。

    挂起后由 `POST /api/reviews/{thread_id}` 传入确认结果恢复执行。
    ⚠ `thread_id` 必须和 `run_id` 一起落表，否则进程重启后找不到等待中的确认
    （技术方案第十一章风险提醒第 3 条）。
    """

    async def human_review(state: dict) -> dict:
        drafts = state.get("drafts") or []
        if not drafts:
            return {"decisions": {}}

        # 本轮命中的评论如果**全都**是前几轮已经记录过的（命中记录按 comment_id 去重，
        # 跨轮次复用同一条），那就没有新东西要人拍板——别为老评论再挂起一次，
        # 否则每轮扫描都会堆出一个新的「待确认」线程，看板上全是重复项。
        if not state.get("persisted"):
            log.info("本轮没有新的评论命中，跳过人工确认（%d 条已在待确认列表里）", len(drafts))
            return {"decisions": {}}

        decision = interrupt(
            {
                "type": "comment_reply_review",
                "thread_id": state.get("thread_id", ""),
                "video_id": (state.get("video") or {}).get("video_id", ""),
                "prompt": "请确认哪些拟回复可以采纳（approved / ignored）",
                "drafts": drafts,
            }
        )
        decisions = (decision or {}).get("decisions", {}) or {}
        log.info("收到人工确认 %d 条", len(decisions))
        return {"decisions": decisions}

    return human_review


def make_apply_decisions(storage: StorageProvider):
    """确认之后：把决定写回《评论命中表》。幂等——重放只是把同样的状态再写一遍。"""

    async def apply_decisions(state: dict) -> dict:
        decisions = state.get("decisions") or {}
        changed = await storage.decide_comment_hits(decisions)
        log.info("回写人工决定 %d 条（thread_id=%s）", changed, state.get("thread_id", ""))
        return {"decided": changed}

    return apply_decisions
