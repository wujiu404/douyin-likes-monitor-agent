"""告警渠道协议。

刻意与存储解耦：存储回答「数据放哪」，告警回答「怎么通知」。
两者都是 provider，各自可换——默认全本地，正式演示时切飞书。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    name: str

    async def send_alerts(self, run_id: str, items: list[dict]) -> bool:
        """把本轮告警汇总成**一条**消息发出。

        items 每项形如：
            {video_id, account, title, prev_likes, curr_likes, delta}

        幂等由调用方（`send_alerts` 节点）负责——它先用
        `storage.try_log_alert(run_id, video_id, channel)` 抢锁，
        抢到才调用这里。所以本方法可以假定「这一条确实该发」。
        """
        ...

    async def send_reviews(self, thread_id: str, items: list[dict]) -> bool:
        """把「有待确认的拟回复」汇总成**一条**消息发出。

        items 每项形如：
            {comment_id, content, keywords, draft, video_id, account, video_title,
             song_title, song_artist}

        和 `send_alerts` 是**两类不同的通知**，刻意不合成一个方法：
        一个说「数据涨了」，一个说「有事等你拍板」，收件人的动作完全不同。

        幂等同样由调用方（`notify_reviews` 节点）负责。
        """
        ...
