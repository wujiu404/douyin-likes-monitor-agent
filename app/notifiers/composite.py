"""多渠道并存。

`NOTIFIER_BACKEND` 支持用 `+` 串多个渠道（和 `STORAGE_BACKEND=sqlite+feishu` 同一套写法）：

```env
NOTIFIER_BACKEND=local+feishu_im
```

典型诉求就是「本地看板要记一笔，同时也要推到我飞书」——这两个都要，不是二选一。

语义上有一条刻意的选择：**只有全部渠道都成功才算送达**（返回 `all(...)`）。
否则「飞书挂了但本地日志写成功」会被记成已推送，你就永远不会发现推送坏了。
单个渠道抛异常会被兜住，不会连带影响后面的渠道。
"""
from __future__ import annotations

import logging

from app.notifiers.base import Notifier

log = logging.getLogger(__name__)


class CompositeNotifier:
    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = list(notifiers)
        self.name = "+".join(n.name for n in self.notifiers)

    async def send_alerts(self, run_id: str, items: list[dict]) -> bool:
        return await self._fanout("告警", items, lambda n: n.send_alerts(run_id, items))

    async def send_reviews(self, thread_id: str, items: list[dict]) -> bool:
        return await self._fanout("评论提醒", items, lambda n: n.send_reviews(thread_id, items))

    async def _fanout(self, label: str, items: list[dict], call) -> bool:
        if not items:
            return True

        results: dict[str, bool] = {}
        for n in self.notifiers:
            try:
                results[n.name] = bool(await call(n))
            except Exception:  # noqa: BLE001 —— 一个渠道炸了不能拖累其他渠道
                log.exception("告警渠道 %s 发送%s异常", n.name, label)
                results[n.name] = False

        ok = all(results.values())
        log.info(
            "%s多渠道推送：%s → %s",
            label,
            "、".join(f"{k}={'成功' if v else '失败'}" for k, v in results.items()),
            "已送达" if ok else "有渠道未送达",
        )
        return ok

    async def aclose(self) -> None:
        for n in self.notifiers:
            closer = getattr(n, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    log.warning("关闭告警渠道 %s 失败", n.name, exc_info=True)


__all__ = ["CompositeNotifier"]
