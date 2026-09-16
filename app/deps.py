"""依赖容器与装配。

把所有外部依赖（存储、数据源、告警渠道、两张图）装进一个对象，
路由通过 `Depends` 取它。好处是测试时可以整体替换成 fake——
全图跑通不需要网络、不需要飞书账号、不需要真实数据源。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request

from app.config import Settings, get_settings
from app.graph.comment_graph import build_comment_graph, make_comment_runner
from app.graph.monitor_graph import build_monitor_graph
from app.notifiers import build_notifier
from app.providers.registry import ProviderRegistry
from app.storage import build_storage

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@dataclass
class Deps:
    settings: Settings
    storage: Any
    registry: ProviderRegistry
    notifier: Any
    checkpointer: Any = None
    monitor: Any = None
    comment: Any = None
    comment_runner: Any = None


async def build_deps(settings: Settings, checkpointer: Any) -> Deps:
    storage = build_storage(settings)
    await storage.init()

    deps = Deps(
        settings=settings,
        storage=storage,
        registry=ProviderRegistry(settings),
        notifier=build_notifier(settings),
        checkpointer=checkpointer,
    )

    # 让 mock provider 从「已入库轮次数」接着数，进程重启后模拟点赞数不会回落
    deps.registry.set_round_baseline(len(await storage.list_rounds(limit=1000)))

    # 装配顺序有讲究：评论图 → 评论发射器 → 主图（主图节点要用到发射器）
    deps.comment = build_comment_graph(deps)
    deps.comment_runner = make_comment_runner(deps.comment)
    deps.monitor = build_monitor_graph(deps)

    log.info(
        "装配完成：存储=%s，告警=%s，数据源链=%s，评论子图=%s",
        getattr(storage, "backend", settings.storage_backend),
        deps.notifier.name,
        " → ".join(deps.registry.chain),
        "开" if settings.enable_comment_graph else "关",
    )
    return deps


def get_deps(request: Request) -> Deps:
    """FastAPI 依赖：从 app.state 取装配好的容器（在 lifespan 里放进去）。"""
    return request.app.state.deps


__all__ = ["Deps", "build_deps", "get_deps", "get_settings", "WEB_DIR"]
