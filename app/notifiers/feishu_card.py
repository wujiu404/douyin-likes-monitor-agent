"""飞书群卡片告警（可选渠道）。

用**群自定义机器人 webhook**：不需要自建应用、不需要开发者后台，
也不消耗开放平台的 API 调用额度——对个人使用是门槛最低的一条路。
获取方式：群设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 webhook 地址。

未配置 `FEISHU_WEBHOOK` 时不发送（返回 False），上层记为「未送达」，
但**不会让整轮扫描失败**——告警发不出去不该拖垮采集。
"""
from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.notifiers.card import alert_card, review_card

log = logging.getLogger(__name__)


class FeishuCardNotifier:
    name = "feishu_card"

    def __init__(self, settings: Settings) -> None:
        self.webhook = (settings.feishu_webhook or "").strip()

    def build_card(self, run_id: str, items: list[dict]) -> dict:
        # 渲染逻辑与「应用消息」渠道共用，群卡片用红色 header 更醒目
        return alert_card(run_id, items, header_template="red")

    async def _post(self, card: dict, what: str) -> bool:
        if not self.webhook:
            log.warning(
                "未配置 FEISHU_WEBHOOK，%s未发送（切换 NOTIFIER_BACKEND=local 可走本地看板）", what
            )
            return False

        payload = {"msg_type": "interactive", "card": card}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook, json=payload)
            if resp.status_code >= 400:
                log.error("飞书卡片发送失败：HTTP %s %s", resp.status_code, resp.text[:200])
                return False
            body = resp.json() if resp.content else {}
            if body.get("code") not in (0, None):
                log.error("飞书卡片发送失败：%s", body)
                return False
            log.info("飞书卡片已发送：%s", what)
            return True
        except Exception:  # noqa: BLE001
            log.exception("飞书卡片发送异常")
            return False

    async def send_alerts(self, run_id: str, items: list[dict]) -> bool:
        if not items:
            return True
        return await self._post(self.build_card(run_id, items), f"{len(items)} 条告警")

    async def send_reviews(self, thread_id: str, items: list[dict]) -> bool:
        if not items:
            return True
        return await self._post(review_card(thread_id, items), f"{len(items)} 条待确认拟回复")
