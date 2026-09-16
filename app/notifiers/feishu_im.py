"""告警直发飞书（应用消息 / 私聊）。

和 `feishu_card.py` 的群 webhook 是**两条独立的路**：

| | 群自定义机器人 webhook | 应用消息（本文件） |
|---|---|---|
| 需要自建应用 | 否 | 是（本项目已有） |
| 收件人 | 那个群 | 你本人（私聊）/ 指定群 |
| 额度 | 不占开放平台 API 额度 | 占应用额度（约 1 万次/月） |
| 能否 @人 / 带按钮 | 不能 | 能 |

本项目**已有自建应用**（多维表格在用），所以私聊是零成本的额外通道，
而且比「自己拉个群再塞机器人」体验好得多。

收件人怎么来的：飞书没有「查我自己 open_id」的接口，但**应用信息里的
`creator_id` 就是创建者（= 你）的 open_id**——这个应用是你扫码建的，所以直接可用。
配置留空时会自动去取一次并缓存；显式填 `FEISHU_NOTIFY_RECEIVE_ID` 则优先用配置的。

失败一律只告警不抛：告警发不出去不该让整轮扫描失败（和群卡片同一原则）。
"""
from __future__ import annotations

import json
import logging

import httpx

from app.config import Settings
from app.core.feishu_api import OPEN_BASE, TenantToken
from app.notifiers.card import alert_card, review_card

log = logging.getLogger(__name__)

# 常见失败原因 → 人话。别的错误原样打出来，不猜。
_HINTS = {
    99991672: "应用未开通该接口权限，去开放平台「权限管理」加 im:message 并发版",
    99991663: "应用未发布或未生效，去「版本管理与发布」发一个版本",
    230002: "机器人不在该会话里，或被收件人拉黑",
    230013: "收件人不在应用的可用范围内，去「应用发布 → 可用范围」加上自己",
    230020: "机器人能力未开启，去开放平台「添加应用能力 → 机器人」打开",
}


class FeishuIMNotifier:
    name = "feishu_im"

    def __init__(self, settings: Settings) -> None:
        self.app_id = (settings.feishu_app_id or "").strip()
        self.receive_id = (settings.feishu_notify_receive_id or "").strip()
        self.receive_id_type = (settings.feishu_notify_receive_id_type or "open_id").strip()
        self._token = TenantToken(self.app_id, (settings.feishu_app_secret or "").strip())
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ 基础设施
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def resolve_receive_id(self) -> str:
        """拿到收件人 ID。没配就去问应用信息里的 `creator_id`，并缓存。"""
        if self.receive_id:
            return self.receive_id
        if not self.app_id:
            return ""

        client = self._http()
        token = await self._token.get(client, label="飞书消息")
        resp = await client.get(
            f"{OPEN_BASE}/application/v6/applications/{self.app_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"lang": "zh_cn"},
        )
        body = resp.json()
        creator = ((body.get("data") or {}).get("app") or {}).get("creator_id") or ""
        if not creator:
            log.warning("拿不到应用创建者 open_id（%s），告警无法私聊送达", body.get("msg"))
            return ""
        self.receive_id = creator
        self.receive_id_type = "open_id"
        log.info("告警私聊对象自动识别为应用创建者：%s", creator)
        return creator

    # ------------------------------------------------------------ 发送
    async def _send(self, card: dict, receive_id: str) -> None:
        """真正发一次。抽成方法是为了离线测试能整个替换掉（不碰网络）。"""
        client = self._http()
        token = await self._token.get(client, label="飞书消息")
        resp = await client.post(
            f"{OPEN_BASE}/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": self.receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400 or body.get("code") not in (0, None):
            code = body.get("code")
            hint = _HINTS.get(code, "")
            raise RuntimeError(
                f"飞书消息发送失败 http={resp.status_code} code={code} "
                f"msg={body.get('msg')}{'｜' + hint if hint else ''}"
            )

    async def _deliver(self, card: dict, what: str) -> bool:
        """解析收件人 + 发一张卡片。失败只降级，不抛。"""
        try:
            receive_id = await self.resolve_receive_id()
        except Exception as exc:  # noqa: BLE001
            log.error("飞书%s未发送（解析收件人失败）：%s", what, exc)
            return False

        if not receive_id:
            log.warning(
                "飞书%s未发送：没配 FEISHU_NOTIFY_RECEIVE_ID 且自动识别失败。"
                "把你自己的 open_id（或邮箱，配合 FEISHU_NOTIFY_RECEIVE_ID_TYPE=email）填进 .env",
                what,
            )
            return False

        try:
            await self._send(card, receive_id)
        except Exception as exc:  # noqa: BLE001 —— 发送失败是降级，不是故障
            log.error("飞书%s未发送：%s", what, exc)
            return False

        log.info(
            "飞书%s已私聊送达（%s=%s）：%s",
            what, self.receive_id_type, receive_id, what,
        )
        return True

    async def send_alerts(self, run_id: str, items: list[dict]) -> bool:
        if not items:
            return True
        return await self._deliver(alert_card(run_id, items), f"告警 {len(items)} 条")

    async def send_reviews(self, thread_id: str, items: list[dict]) -> bool:
        if not items:
            return True
        return await self._deliver(
            review_card(thread_id, items), f"待确认拟回复 {len(items)} 条"
        )


__all__ = ["FeishuIMNotifier"]
