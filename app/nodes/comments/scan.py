"""评论子图节点 1：scan_comments —— 拉取评论。

副作用：无（只读外部数据）。
"""
from __future__ import annotations

import logging

from app.graph.state import CommentState
from app.providers.registry import ProviderRegistry

log = logging.getLogger(__name__)


def make_scan_comments(registry: ProviderRegistry, limit: int = 20):
    async def scan_comments(state: CommentState) -> dict:
        video = state.get("video") or {}
        video_id = video.get("video_id", "")
        comments, source = await registry.fetch_comments(video_id, limit=limit)
        log.info("拉取评论 %d 条（来源=%s）", len(comments), source or "-")
        return {"comments": comments}

    return scan_comments
