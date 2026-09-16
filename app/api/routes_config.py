"""配置表读写（控制面）。

改一项配置，**下一轮调度立即生效**——写完之后调度器会重载 job。
不用改代码、不用重启，这就是「配置表是控制面」的含义。

演示切档走 `PUT /api/config/preset`（看板按钮与 `tools/demo_mode.py` 都调它），
档位定义在 `app/core/presets.py` —— **只有那一份**，避免两边各写一份再漂移。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.presets import normalize_preset_name, preset_of, resolve_preset
from app.deps import Deps, get_deps

log = logging.getLogger(__name__)
router = APIRouter(tags=["配置"])

# 切档要逐格写 6 个键，中间必然经过「半套档位」；**并发切档还会交叉覆盖**
# （2026-09-16：用户连点了 4 次「恢复正式档」，同一时刻自检也在切档）。
# 进程内一把锁把切档串行化 —— 本项目是单进程部署（见 README §9），够用。
_preset_lock = asyncio.Lock()

# 写后核对的补写次数。`get_config()` 在双写模式下**优先读飞书**，而 `set_config`
# 是「先本地、后飞书」逐格写 —— 飞书那一步慢半拍或被限流，读回来的就不是完整档位。
_PRESET_VERIFY_TRIES = 3


class ConfigPayload(BaseModel):
    key: str
    value: str | int | bool


class PresetPayload(BaseModel):
    name: str


@router.get("/config", summary="读取配置表（含已解析的值与当前档位）")
async def read_config(deps: Deps = Depends(get_deps)) -> dict:
    resolved = await deps.storage.get_config()
    return {
        "items": await deps.storage.list_config(),
        "resolved": resolved,
        # demo / formal / mixed —— 「混合」就是半个档位，前端会高亮提示
        "preset": preset_of(resolved),
    }


@router.put("/config", summary="改一项配置，下一轮调度生效")
async def write_config(
    body: ConfigPayload, request: Request, deps: Deps = Depends(get_deps)
) -> dict:
    await deps.storage.set_config(body.key, body.value)

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        desc = await scheduler.reload()
        log.info("配置已更新（%s=%s），调度重载：%s", body.key, body.value, desc)

    return {"ok": True, "resolved": await deps.storage.get_config()}


@router.put("/config/preset", summary="一键切档位（demo 演示档 / formal 正式档）")
async def write_preset(
    body: PresetPayload, request: Request, deps: Deps = Depends(get_deps)
) -> dict:
    """把一个档位涉及的所有格**一次改完**，再让调度重载一次。

    和逐格 PUT `/api/config` 的区别：切档要改 6 格，逐格写会触发 6 次调度重载
    外加 6 次飞书双写（约 30 秒）。

    ⚠ 档位必须整体应用 —— 只改 `threshold` 不改 `provider_chain` 会切出
    「节奏变快、阈值降低，但数据源还是真实源」的半个档位：增量上不去、
    告警不触发，看着像 mock 坏了（2026-09-16 的真实故障，见 presets.py）。

    ⚠ **写完必须读回来核对**：逐格写 + `get_config()` 优先读飞书，
    所以「写完了」不等于「读到的就是完整档位」——中间那几秒里读配置会看到
    `preset=mixed`，用户点完按钮刷新页面正好撞上，就会以为没切成功（他连着点了 4 次）。
    核对不一致就补写；仍不一致则如实返回 `verified=False`，让调用方提示重试，
    而不是假装成功。
    """
    try:
        name = normalize_preset_name(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    changes = resolve_preset(name)

    async with _preset_lock:
        before = await deps.storage.get_config()
        resolved = before
        for attempt in range(1, _PRESET_VERIFY_TRIES + 1):
            for key, value in changes.items():
                await deps.storage.set_config(key, value)
            resolved = await deps.storage.get_config()
            if preset_of(resolved) == name:
                break
            log.warning(
                "切档（%s）写后核对不一致（第 %s 次）：实际 preset=%s，补写",
                name, attempt, preset_of(resolved),
            )
            await asyncio.sleep(0.3 * attempt)   # 等对端（飞书）那几格落下去

        desc = None
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None:
            desc = await scheduler.reload()

    verified = preset_of(resolved) == name
    if verified:
        log.info("档位已切换（%s），调度重载：%s", name, desc)
    else:
        log.error(
            "档位（%s）写后核对仍未通过：preset=%s resolved=%s（本地/飞书可能不一致）",
            name, preset_of(resolved), resolved,
        )

    moved = {k: {"from": str(before.get(k)), "to": str(v)} for k, v in changes.items()
             if str(before.get(k)) != str(v)}
    return {
        "ok": True,
        "preset": name,
        "verified": verified,
        "applied": changes,
        "changed": moved,
        "resolved": resolved,
        "scheduler": desc,
    }
