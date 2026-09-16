"""图的状态定义。

`MonitorState.errors` 是普通 list 而**不是** `Annotated[list[str], add]`——
主图的边完全串行、没有并行分支，reducer 在这里没有收益，只会引入「未来时」的复杂度。
如果将来把「每账号采集」拆成并行节点（LangGraph 的 Send API），
这里必须换回 reducer，否则并发写入会互相覆盖。
"""
from __future__ import annotations

from typing import Literal, TypedDict


class Account(TypedDict, total=False):
    name: str
    sec_uid: str
    homepage: str


class VideoMetric(TypedDict, total=False):
    video_id: str
    account: str
    title: str
    publish_time: str
    likes: int
    comments: int
    shares: int
    source: str


class DeltaItem(TypedDict, total=False):
    video_id: str
    account: str
    title: str
    prev_likes: int
    curr_likes: int
    delta: int


class MonitorState(TypedDict, total=False):
    # ---- 轮次标识 ----
    run_id: str
    trigger: Literal["cron", "manual"]
    started_at: str

    # ---- 配置快照（只在 load_accounts 节点读一次，本轮内保持一致）----
    threshold: int
    lookback_days: int
    comment_keywords: list[str]
    provider_chain: list[str]
    comment_scope: str

    # ---- 采集 ----
    accounts: list[Account]
    videos: list[VideoMetric]
    provider_trace: list[str]
    source: str

    # ---- 落表与计算 ----
    snapshot_rows: list[dict]
    deltas: list[DeltaItem]
    alerts: list[DeltaItem]
    alert_summary: dict

    # ---- 评论子图（fire-and-forget，只留 thread_id 不回流结果）----
    comment_threads: list[str]

    # ---- 收尾 ----
    error_count: int
    errors: list[str]
    note: str
    skip_reason: str
    finished_at: str


class CommentState(TypedDict, total=False):
    """评论子图状态。

    ⚠ LangGraph 只保留 State 里**声明过**的键，节点或入口传入的未声明字段会被静默丢掉。
    所以 `comment_trigger` 塞进来的 `keywords` 必须在这里声明，
    否则命中规则会拿到空词表、一条都命中不了（这类 bug 不报错，只是结果为空）。
    """

    run_id: str
    thread_id: str
    video: VideoMetric
    keywords: list[str]

    comments: list[dict]
    hits: list[dict]
    song: dict
    drafts: list[dict]

    decisions: dict[str, str]     # {comment_id: 'approved' | 'ignored'}
    persisted: int
    notified: int
    decided: int
