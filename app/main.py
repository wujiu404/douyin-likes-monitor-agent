"""FastAPI 入口：装配依赖 → 挂调度器 → 托管前端。

启动顺序（lifespan）：
1. 建目录、读 Settings、配日志
2. 开 checkpointer（AsyncSqliteSaver）—— 评论子图的 interrupt 挂起靠它
3. `build_deps` 装配存储 / 数据源 / 告警 / 两张图
4. 调度器按《配置表》注册 job 并启动
5. 关闭时先停调度、再关存储
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.api import routes_config, routes_data, routes_review, routes_scan
from app.config import get_settings
from app.core.logging import setup_logging
from app.deps import WEB_DIR, build_deps
from app.scheduler import ScanScheduler

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(logging.DEBUG if settings.debug else logging.INFO)

    settings.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    log.info("启动中……（后端 Python: %s）", __import__("sys").version.split()[0])

    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_file)) as checkpointer:
        deps = await build_deps(settings, checkpointer)
        app.state.deps = deps
        app.state.scheduler = None

        if settings.scheduler_enabled:
            scheduler = ScanScheduler(deps)
            description = await scheduler.reload()
            scheduler.start()
            app.state.scheduler = scheduler
            log.info("调度已启动：%s", description)
        else:
            log.info("调度未启用（SCHEDULER_ENABLED=false）")

        log.info("就绪 → http://%s:%s", settings.app_host, settings.app_port)
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown()
            browser = deps.registry.get("browser")
            if browser is not None and hasattr(browser, "aclose"):
                await browser.aclose()  # 关掉共享的 Chrome 持久化上下文（若启动过）
            notifier = deps.notifier
            close_notifier = getattr(notifier, "aclose", None)  # 复合渠道由它自己逐个关
            if close_notifier is not None:
                await close_notifier()
            await deps.storage.close()
            log.info("已停止")


app = FastAPI(
    title="抖音点赞监控 Agent",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "定时扫描抖音公开作品数据 → 落表 → 算增量 → 超阈值告警，"
        "叠加评论关键词拟回复（人工确认后才落表，任何情况下不自动发评论）。\n\n"
        "- 编排：LangGraph（评论子图用 interrupt 挂起等人工确认）\n"
        "- 存储：业务数据走 StorageProvider（默认 SQLite），Graph 执行状态走 checkpointer\n"
        "- 数据源：provider 降级链 thirdparty → browser → mock\n"
        "- 合规：只读公开数据，不自动点赞、不自动评论、不自动关注"
    ),
)

app.include_router(routes_scan.router, prefix="/api")
app.include_router(routes_review.router, prefix="/api")
app.include_router(routes_data.router, prefix="/api")
app.include_router(routes_config.router, prefix="/api")


@app.get("/api/health", tags=["系统"], summary="健康检查")
async def health() -> dict:
    return {"ok": True}


if WEB_DIR.exists():
    # 前端是零构建静态页，直接由后端托管（不用 npm、不用打包）
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
