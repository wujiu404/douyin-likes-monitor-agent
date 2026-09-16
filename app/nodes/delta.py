"""节点 4：compute_deltas —— 算相邻两次扫描的点赞增量。

副作用：无（只读上一轮快照）。

`增量 = 本次点赞 − 上一条快照的点赞`。没有上一条快照（首见视频）时增量记为 0，
并把 `is_first` 标出来——判定节点据此**不把首条快照算成告警**，
否则新出现的视频会立刻报一个巨大的「增量」，那是噪声不是信号。

增量允许为负（真实场景里会有点赞取消）。负增量不告警，但照样落表。
"""
from __future__ import annotations

import logging

from app.graph.state import MonitorState
from app.storage.base import StorageProvider

log = logging.getLogger(__name__)


def make_compute_deltas(storage: StorageProvider):
    async def compute_deltas(state: MonitorState) -> dict:
        run_id = state["run_id"]
        deltas: list[dict] = []

        for v in state.get("videos", []):
            video_id = v["video_id"]
            prev = await storage.previous_snapshot(video_id, run_id)
            curr_likes = int(v.get("likes", 0))

            if prev is None:
                deltas.append(
                    {
                        "video_id": video_id,
                        "account": v.get("account", ""),
                        "title": v.get("title", ""),
                        "prev_likes": curr_likes,
                        "curr_likes": curr_likes,
                        "delta": 0,
                        "is_first": True,
                    }
                )
                continue

            prev_likes = int(prev.get("likes", 0))
            deltas.append(
                {
                    "video_id": video_id,
                    "account": v.get("account", ""),
                    "title": v.get("title", ""),
                    "prev_likes": prev_likes,
                    "curr_likes": curr_likes,
                    "delta": curr_likes - prev_likes,
                    "is_first": False,
                }
            )

        first_seen = sum(1 for d in deltas if d["is_first"])
        log.info("算出 %d 条增量（其中 %d 条是首见视频）", len(deltas), first_seen)
        return {"deltas": deltas}

    return compute_deltas
