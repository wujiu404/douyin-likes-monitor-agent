"""幂等键。

LangGraph checkpointer 在恢复执行时**可能重放节点**，所以每个有副作用的节点
（写快照、写告警、发提醒）都必须先查幂等键再去写。

⚠ 这不是优化项，而是一致性边界成立的前提：飞书写入与 checkpoint 落盘**不在同一事务内**
（见 docs/01-技术方案-修订版.md 4.1），恢复重放时可能出现「快照已写、checkpoint 未落」
或反之。没有幂等键，重放就会产生脏数据。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

CST = timezone(timedelta(hours=8))


def new_run_id() -> str:
    """生成轮次标识，形如 R20260915-160930-a1b2。"""
    return "R{}-{}".format(datetime.now(CST).strftime("%Y%m%d-%H%M%S"), uuid4().hex[:4])


def snapshot_key(run_id: str, video_id: str) -> str:
    """快照表去重键。"""
    return f"{run_id}:{video_id}"


def delta_key(run_id: str, video_id: str) -> str:
    """增量记录去重键。"""
    return f"{run_id}:{video_id}:delta"


def alert_key(run_id: str, video_id: str) -> str:
    """告警推送去重键——同一轮同一视频只推一次。"""
    return f"{run_id}:{video_id}:alert"


def comment_key(comment_id: str) -> str:
    """评论命中记录去重键。

    **只按 comment_id，不含 thread_id。** 这是刻意改过的：

    早先的键是 `{thread_id}:{comment_id}`，于是「同一条评论」在每一轮扫描里
    都会重新落一条记录。评论 id 是全局唯一的，同一条评论被重复采到就是同一条，
    不是「不同的人问了同一件事」——按轮次区分只会让《评论命中表》里堆满
    一模一样的行（演示档 3 分钟一轮，肉眼可见地在翻倍），待确认列表也被灌水。

    现在：一条评论**一生只落一次表**，跨轮次复用那条 pending 记录，
    人工确认的结果也不会被下一轮覆盖回 pending。
    """
    return f"comment:{comment_id}"
