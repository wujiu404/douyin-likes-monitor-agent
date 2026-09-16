"""节点 3：write_snapshots —— 写《视频快照表》。

副作用：**写业务存储**。幂等键：`run_id:video_id`（见 core/idempotency.py）。

为什么先查 `exists` 再写：checkpointer 恢复执行时会重放节点，同一对
`(run_id, video_id)` 可能被写两次。SQLite 侧的 `INSERT OR IGNORE` 是第二层兜底，
但显式查一次语义更清楚，而且换到飞书后端时不会退化——飞书的 batch_create 不是幂等的。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.idempotency import snapshot_key
from app.graph.state import MonitorState
from app.storage.base import StorageProvider

log = logging.getLogger(__name__)


def make_write_snapshots(storage: StorageProvider):
    async def write_snapshots(state: MonitorState) -> dict:
        run_id = state["run_id"]
        videos = state.get("videos", [])
        source = state.get("source", "")
        scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        rows: list[dict] = []
        skipped = 0
        for v in videos:
            key = snapshot_key(run_id, v["video_id"])
            if await storage.exists_snapshot(key):
                skipped += 1
                continue
            rows.append(
                {
                    "idem_key": key,
                    "run_id": run_id,
                    "scanned_at": scanned_at,
                    "account": v.get("account", ""),
                    "video_id": v["video_id"],
                    "title": v.get("title", ""),
                    "publish_time": v.get("publish_time", ""),
                    "likes": int(v.get("likes", 0)),
                    "comments": int(v.get("comments", 0)),
                    "shares": int(v.get("shares", 0)),
                    "source": v.get("source", source),
                }
            )

        if rows:
            await storage.batch_create_snapshots(rows)
        log.info("写快照 %d 条（重放跳过 %d 条）", len(rows), skipped)
        return {"snapshot_rows": rows}

    return write_snapshots
