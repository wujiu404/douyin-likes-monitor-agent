"""飞书开放平台的公共底座：域名、租户令牌缓存。

有两个地方要用飞书接口，但用途完全不同：

- `app/storage/feishu.py` —— **多维表格**（业务数据）
- `app/notifiers/feishu_im.py` —— **应用消息**（把告警发到你的飞书）

它们**不共用客户端**（一张表挂了不该连带消息也发不出），但取令牌这件事必须一致——
所以令牌逻辑收在这里，各用各的 `TenantToken` 实例。

令牌缓存用 `time.monotonic()` 计时：墙钟被 NTP 校准或用户改系统时间时，
`monotonic` 不会突然倒退导致缓存被误判为「已过期」或「永不过期」。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.core.retry import retry_with_backoff

if TYPE_CHECKING:
    import httpx

OPEN_BASE = "https://open.feishu.cn/open-apis"

# 令牌提前量：官方有效期 7200s，留 5 分钟余量，避免「请求刚发出就过期」
REFRESH_MARGIN = 300.0


class TenantToken:
    """`tenant_access_token` 的缓存与刷新（一个应用一份）。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str = ""
        self._deadline: float = 0.0  # monotonic 秒

    async def get(self, client: "httpx.AsyncClient", *, label: str = "飞书") -> str:
        now = time.monotonic()
        if self._token and now < self._deadline:
            return self._token
        if not (self.app_id and self.app_secret):
            raise RuntimeError(f"{label}未配置 app_id / app_secret，无法鉴权")

        token, expire = await retry_with_backoff(
            lambda: _fetch(self.app_id, self.app_secret, client), label=f"{label}取 token"
        )
        self._token = token
        self._deadline = time.monotonic() + max(expire - REFRESH_MARGIN, 60.0)
        return self._token


async def _fetch(app_id: str, app_secret: str, client: "httpx.AsyncClient") -> tuple[str, int]:
    resp = await client.post(
        f"{OPEN_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"飞书鉴权失败：{body.get('msg')}（检查 FEISHU_APP_ID / FEISHU_APP_SECRET）"
        )
    return body["tenant_access_token"], int(body.get("expire", 7200))


__all__ = ["OPEN_BASE", "TenantToken"]
