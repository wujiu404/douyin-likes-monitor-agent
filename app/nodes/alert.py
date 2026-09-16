"""节点 5 / 6：判定与推送。

`decide_alerts` —— 副作用：**写《增量与告警表》**。幂等键 `run_id:video_id:delta`。
    这张表的产出物就是判定结果本身（含 `is_alert` 字段），所以判定和落表是一个节点，
    不必再拆。写入用 `INSERT OR IGNORE`，重放安全。

`send_alerts` —— 副作用：**推告警**。幂等键 `run_id:video_id:alert`。
    推之前先用 `storage.try_log_alert` 抢一次唯一键，抢到才真的发。
    这样「重放节点」和「并发触发」两种情况都不会重复推送。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core.idempotency import alert_key, delta_key
from app.graph.state import MonitorState
from app.notifiers.base import Notifier
from app.storage.base import StorageProvider

log = logging.getLogger(__name__)


def make_decide_alerts(storage: StorageProvider):
    async def decide_alerts(state: MonitorState) -> dict:
        run_id = state["run_id"]
        threshold = int(state.get("threshold", 20))

        rows: list[dict] = []
        alerts: list[dict] = []

        for d in state.get("deltas", []):
            is_alert = (not d.get("is_first")) and d["delta"] > threshold
            rows.append(
                {
                    "idem_key": delta_key(run_id, d["video_id"]),
                    "run_id": run_id,
                    "video_id": d["video_id"],
                    "account": d.get("account", ""),
                    "title": d.get("title", ""),
                    "prev_likes": d["prev_likes"],
                    "curr_likes": d["curr_likes"],
                    "delta": d["delta"],
                    "is_alert": is_alert,
                }
            )
            if is_alert:
                alerts.append(d)

        if rows:
            await storage.create_deltas(rows)
        log.info("判定完成：阈值 %s，命中 %d / %d 条", threshold, len(alerts), len(rows))
        return {"alerts": alerts}

    return decide_alerts


def make_send_alerts(storage: StorageProvider, notifier: Notifier):
    async def send_alerts(state: MonitorState) -> dict:
        run_id = state["run_id"]
        alerts = state.get("alerts", [])
        threshold = state.get("threshold")

        if not alerts:
            return {"alert_summary": {"channel": notifier.name, "sent": 0, "deduped": 0}}

        # 逐条抢幂等锁：抢到的才是「本轮第一次推」
        # 推给渠道的副本额外带上 threshold —— 消息里要写清「超过多少算告警」，
        # 否则收到告警的人没法判断这条是松是紧。落 alert_log 的 payload 仍是原始值。
        fresh: list[dict] = []
        for a in alerts:
            payload = json.dumps(a, ensure_ascii=False)
            if await storage.try_log_alert(
                run_id, a["video_id"], notifier.name, payload, "alert"
            ):
                fresh.append({**a, "threshold": threshold} if threshold is not None else a)

        deduped = len(alerts) - len(fresh)
        if not fresh:
            log.info("本轮 %d 条告警已推送过，幂等拦截", len(alerts))
            return {"alert_summary": {"channel": notifier.name, "sent": 0, "deduped": deduped}}

        sent = await notifier.send_alerts(run_id, fresh)
        if sent:
            keys = [delta_key(run_id, a["video_id"]) for a in fresh]
            await storage.mark_alerted(keys, datetime.now(timezone.utc).isoformat(timespec="seconds"))

        log.info("推送 %d 条告警（渠道=%s，成功=%s，幂等拦截 %d）", len(fresh), notifier.name, sent, deduped)
        return {
            "alert_summary": {
                "channel": notifier.name,
                "sent": len(fresh) if sent else 0,
                "deduped": deduped,
                "delivered": bool(sent),
            }
        }

    return send_alerts
