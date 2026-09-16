"""本地告警渠道（默认）。

不依赖任何外部服务：把告警打进控制台，同时落 `alert_log` 表供前端看板展示。
迁移到新机器时零配置就能跑。
"""
from __future__ import annotations

import logging

from app.config import Settings

log = logging.getLogger("alert")


class LocalNotifier:
    name = "local"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    async def send_alerts(self, run_id: str, items: list[dict]) -> bool:
        if not items:
            return True

        lines = [f"【告警】{run_id} 共 {len(items)} 条超阈值"]
        for it in items:
            lines.append(
                "  · {account} / {title}  点赞 {prev} → {curr}（+{delta}）".format(
                    account=it.get("account", "-"),
                    title=(it.get("title") or "")[:24],
                    prev=it.get("prev_likes", 0),
                    curr=it.get("curr_likes", 0),
                    delta=it.get("delta", 0),
                )
            )
        for line in lines:
            log.warning(line)
        return True

    async def send_reviews(self, thread_id: str, items: list[dict]) -> bool:
        if not items:
            return True

        lines = [f"【待确认拟回复】{thread_id} 共 {len(items)} 条评论命中"]
        for it in items:
            lines.append(
                "  · 评论：{content}\n    命中：{kw}\n    拟回复：{draft}".format(
                    content=(it.get("content") or "")[:40],
                    kw="、".join(it.get("keywords") or []) or "-",
                    draft=(it.get("draft") or "")[:60],
                )
            )
        lines.append("  （仅待确认，系统不会自动发送）")
        for line in lines:
            log.warning(line)
        return True
