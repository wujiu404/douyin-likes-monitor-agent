"""第三方数据服务 provider（P1 占位实现）。

市面上的可选服务（红狐、蝉妈妈等）都是付费的，接口形态各异，
所以这里只留出**接入点**，不硬编码任何一家的协议。

接入步骤：
1. 在 `.env` 里配好 `THIRDPARTY_BASE_URL` / `THIRDPARTY_API_KEY`
2. 在 `fetch_videos` 里发起请求，把响应映射成 `VIDEO_FIELDS` 约定的结构
3. 保持 `PROVIDER_CHAIN` 里 thirdparty 排在最前

**未配置时抛 ProviderError，降级链自动往下走** —— 这是刻意的：
没配就安静降级，而不是让整轮扫描失败。
"""
from __future__ import annotations

import httpx

from app.config import Settings
from app.providers.base import ProviderError, ProviderNotConfigured


class ThirdpartyProvider:
    name = "thirdparty"
    synthetic = False  # 真实数据源：成功返回空 = 真的没数据，不允许 mock 兜底

    def __init__(self, settings: Settings) -> None:
        self.base_url = getattr(settings, "thirdparty_base_url", "") or ""
        self.api_key = getattr(settings, "thirdparty_api_key", "") or ""

    async def fetch_videos(self, accounts: list[dict], days: int) -> list[dict]:
        if not self.base_url or not self.api_key:
            raise ProviderNotConfigured("thirdparty 未配置（缺 base_url / api_key）")

        # ---- 接入点：把下面这段换成具体服务商的协议 ----
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/videos",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "sec_uids": [a["sec_uid"] for a in accounts],
                    "days": days,
                },
            )
        if resp.status_code >= 400:
            raise ProviderError(f"thirdparty 返回 {resp.status_code}")

        payload = resp.json()
        return [
            {
                "video_id": item["video_id"],
                "account": item.get("account", ""),
                "title": item.get("title", ""),
                "publish_time": item.get("publish_time", ""),
                "likes": int(item.get("likes", 0)),
                "comments": int(item.get("comments", 0)),
                "shares": int(item.get("shares", 0)),
                "source": self.name,
            }
            for item in payload.get("data", [])
        ]

    async def fetch_comments(self, video_id: str, limit: int = 20) -> list[dict]:
        raise ProviderError("thirdparty 暂不支持评论采集")
