"""业务数据查询接口（给前端看板用）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import Deps, get_deps

router = APIRouter(tags=["数据"])


# ---------------------------------------------------------------- 概览
@router.get("/stats", summary="看板顶部的汇总数字")
async def stats(deps: Deps = Depends(get_deps)) -> dict:
    return await deps.storage.stats()


# ---------------------------------------------------------------- 表 1 账号
class AccountPayload(BaseModel):
    name: str
    sec_uid: str
    homepage: str = ""
    enabled: bool = True
    note: str = ""


@router.get("/accounts", summary="监控账号列表")
async def list_accounts(only_enabled: bool = False, deps: Deps = Depends(get_deps)) -> dict:
    return {"items": await deps.storage.list_accounts(only_enabled=only_enabled)}


@router.post("/accounts", summary="新增或更新监控账号")
async def upsert_account(body: AccountPayload, deps: Deps = Depends(get_deps)) -> dict:
    await deps.storage.upsert_account(body.model_dump())
    return {"ok": True}


@router.delete("/accounts/{sec_uid}", summary="删除监控账号")
async def delete_account(sec_uid: str, deps: Deps = Depends(get_deps)) -> dict:
    await deps.storage.delete_account(sec_uid)
    return {"ok": True}


# ---------------------------------------------------------------- 表 2 快照
@router.get("/videos", summary="视频快照")
async def list_videos(
    limit: int = 100, video_id: str | None = None, deps: Deps = Depends(get_deps)
) -> dict:
    return {"items": await deps.storage.list_snapshots(limit=limit, video_id=video_id)}


# ---------------------------------------------------------------- 表 3 增量与告警
@router.get("/deltas", summary="增量记录")
async def list_deltas(
    limit: int = 100, only_alert: bool = False, deps: Deps = Depends(get_deps)
) -> dict:
    return {"items": await deps.storage.list_deltas(limit=limit, only_alert=only_alert)}


@router.get("/alerts", summary="推送日志（点赞告警 + 评论提醒）")
async def list_alerts(
    limit: int = 50,
    kind: str | None = None,
    deps: Deps = Depends(get_deps),
) -> dict:
    """`kind`：不传=全部，`alert`=点赞告警，`review`=评论提醒。"""
    return {"items": await deps.storage.list_alerts(limit=limit, kind=kind)}
