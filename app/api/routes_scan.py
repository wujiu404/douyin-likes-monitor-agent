"""触发层的 HTTP 入口。

⚠ 触发层里其实有两种东西，职责不同，**不该画成同一个模块**：
- `APScheduler`：进程内定时器 → `app/scheduler.py`
- `POST /api/scan`：HTTP 手动入口 → 这里

两者都调同一个 `run_scan`，所以行为一致。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.errors import ScanSkipped
from app.deps import Deps, get_deps
from app.providers.base import ProviderNotConfigured
from app.providers.browser import LOGIN_EXPIRED_HINT
from app.scan_service import ScanBusy, is_scanning, run_scan

log = logging.getLogger(__name__)
router = APIRouter(tags=["扫描"])


@router.post("/scan", summary="手动触发一次扫描")
async def trigger_scan(trigger: str = "manual", deps: Deps = Depends(get_deps)) -> dict:
    try:
        return await run_scan(deps, trigger=trigger)
    except ScanSkipped as exc:
        # 409 而不是 500：这是「当前状态不允许」，调用方改一下状态就能跑
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScanBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/login_status", summary="抖音登录态（browser 数据源）")
async def login_status(deps: Deps = Depends(get_deps)) -> dict:
    """让**服务自己**回答「抖音登录态还在不在」。

    为什么要有这个接口（2026-09-16 实测出来的）：登录态体检以前只能靠
    `tools/check_douyin_login.py`，而那个工具要**自己再拉一个 Edge** 去读 cookie。
    但服务跑起来之后，profile 被它的**热上下文**占着（`_ensure_context` 有意缓存，
    只在进程退出时关），第二个实例必然起不来 —— 实测报
    `BrowserType.launch_persistent_context: Target page, context or browser has been closed`。
    偏偏「演示前体检」正是服务开着的时候，于是这个体检在最需要它的场景下用不了。

    解法是别抢 profile：服务本来就握着待用的浏览器，让它用自己的上下文读一次 cookie
    （`BrowserProvider.check_login`，判据只有 `sessionid`）。

    三种回答，前端/脚本按 `logged_in` 分支即可：
    - `true`  登录态正常
    - `false` 登录态过期（`detail` 就是给用户看的话术 + 修复命令）
    - `null`  **查不了**（browser 没接 / 扫描占着 / 启动失败），看 `detail` 里的原因，
              `configured=false` 表示这一档根本没接——那是预期内降级，不是故障
    """
    provider = deps.registry.get("browser")
    check = getattr(provider, "check_login", None)
    if provider is None or not callable(check):
        return {
            "configured": False,
            "logged_in": None,
            "detail": "browser 数据源不可用（降级链里没有它）",
        }

    # 扫描正在进行时会去抢 provider 的锁，而一轮真实采集要 100 秒上下 ——
    # 与其让这个只读体检把请求挂在那儿，不如立刻如实说「现在查不了」。
    # 想知道这一刻的状态，看轮次台账的备注更快也更准。
    if is_scanning():
        return {
            "configured": True,
            "logged_in": None,
            "detail": "正在扫描，登录态请扫描结束后再查（或直接看 /api/runs 的 note）",
        }

    try:
        ok = await check()
    except ProviderNotConfigured as exc:
        return {"configured": False, "logged_in": None, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 体检失败也要给个能照做的结论
        log.warning("登录态体检失败：%s", exc)
        return {
            "configured": True,
            "logged_in": None,
            "detail": f"检查失败：{exc}（profile 是否被别的进程占用？）",
        }

    if ok:
        return {"configured": True, "logged_in": True, "detail": "登录态正常（profile 里有 sessionid）"}
    return {"configured": True, "logged_in": False, "detail": LOGIN_EXPIRED_HINT}


@router.get("/scan/status", summary="扫描与调度状态")
async def scan_status(request: Request) -> dict:
    scheduler = getattr(request.app.state, "scheduler", None)
    sched = scheduler.snapshot() if scheduler is not None else {"enabled": False, "running": False}
    return {"scanning": is_scanning(), "scheduler": sched}


@router.get("/runs", summary="扫描轮次台账")
async def list_runs(limit: int = 50, deps: Deps = Depends(get_deps)) -> dict:
    return {"items": await deps.storage.list_rounds(limit)}
