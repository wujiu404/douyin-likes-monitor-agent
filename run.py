"""一键启动。

    python run.py

等价于 `uvicorn app.main:app`，只是把 host / port / reload 从 `.env` 读出来。
"""
from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    print(f"→ http://{settings.app_host}:{settings.app_port}   (Ctrl+C 停止)")
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.debug,
        log_level="info",
        # ⚠ Windows 专用坑（2026-09-15 实测钉死）：
        # uvicorn 在 reload/workers 模式下强制用 SelectorEventLoop
        # （config.use_subprocess=True → loops.asyncio 返回 Selector），
        # 而 playwright 的 asyncio.create_subprocess_exec 在 SelectorEventLoop
        # 上抛 NotImplementedError → browser provider 秒挂、静默降级到 mock。
        # loop="none" 让 uvicorn 不干预循环创建 → Windows 默认策略 ProactorEventLoop，
        # reload 照常可用。删掉这个参数真实采集就会失效。
        loop="none",
    )


if __name__ == "__main__":
    main()
