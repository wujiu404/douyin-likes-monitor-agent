"""测试夹具。

三条原则：
1. **不碰网络、不碰飞书、不碰真实抖音**——每个测试用临时目录里的 SQLite，
   checkpointer 用进程内 InMemorySaver。
2. **不依赖外部时钟**——mock provider 是按「轮次」推进的，测试里连点两轮
   就能拿到确定的增量。
3. **不留后台任务**——收尾统一 `drain_background()`，否则事件循环关闭时会刷
   "Task was destroyed but it is pending"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app.config import Settings  # noqa: E402
from app.deps import build_deps  # noqa: E402
from app.nodes.comment_trigger import cancel_background, drain_background  # noqa: E402


@pytest.fixture(autouse=True)
def quiet_logging():
    """把 app 的日志配置还原成安静状态。

    `app.main` 的 lifespan 会调 `setup_logging(DEBUG)`，那是给演示看的；
    它会往 stdout 挂一个 handler，之后每个用例都会刷一屏日志。
    这里在每个用例结束后拆掉，失败时才能看清真正的断言错误。
    """
    yield
    import logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    for name in ("app", "aiosqlite", "httpx", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_host="127.0.0.1",
        app_port=8000,
        debug=False,
        storage_backend="sqlite",
        sqlite_path=str(tmp_path / "app.db"),
        checkpoint_path=str(tmp_path / "checkpoints.db"),
        provider_chain="thirdparty,browser,mock",
        # 显式关掉 browser：否则会捡到真实 .env 里的 BROWSER_USER_DATA_DIR，
        # 测试里真的去启 Edge（profile 还可能被正在跑的服务锁着）
        browser_user_data_dir="",
        notifier_backend="local",
        scheduler_enabled=False,
        enable_comment_graph=True,
        comment_drain_timeout=2.0,
        llm_enabled=False,
    )


@pytest.fixture
async def deps(settings: Settings):
    checkpointer = InMemorySaver()
    d = await build_deps(settings, checkpointer)
    # 用例全部跑 mock：把阈值钉在 20，爆款档每轮 +22~25 正好稳定跨过阈值。
    # 出厂的《配置表》默认是 300（对应「真实账号点赞增量超 300 才告警」的口径），
    # 那是给真实数据用的，不该决定用例的确定性——所以用例自己钉一个值。
    await d.storage.set_config("threshold", 20)
    try:
        yield d
    finally:
        # 先取消（「永不返回」的假任务会拖满 drain 的超时），再让取消传播
        cancel_background()
        await drain_background(timeout=1)
        await d.storage.close()


@pytest.fixture
async def storage(settings: Settings):
    from app.storage.sqlite import SqliteStorage

    s = SqliteStorage(settings.db_file)
    await s.init()
    try:
        yield s
    finally:
        await s.close()
