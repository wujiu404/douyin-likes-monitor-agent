"""业务存储的窄接口。

刻意做成**面向业务**的窄接口（而不是通用 CRUD），原因有二：
1. 业务节点不感知鉴权与后端细节，测试时注入内存 fake 就能跑通全图；
2. 幂等键的查询由存储层提供（`exists_*`），不能让业务层自己拼 SQL 或自己判重。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StorageProvider(Protocol):
    # ---------------- 生命周期 ----------------
    async def init(self) -> None:
        """建表 / 建索引 / 灌入默认数据。幂等，可重复调用。"""
        ...

    async def close(self) -> None: ...

    # ---------------- 配置表（控制面）----------------
    async def list_config(self) -> list[dict]:
        """返回全部配置项（含 key/value/type/scope/note）。"""
        ...

    async def get_config(self) -> dict[str, Any]:
        """返回已按 type 解析好的字典，直接给业务节点用。"""
        ...

    async def set_config(self, key: str, value: Any) -> None: ...

    # ---------------- 表 1 监控账号 ----------------
    async def list_accounts(self, only_enabled: bool = True) -> list[dict]: ...

    async def upsert_account(self, acc: dict) -> None: ...

    async def update_account(self, old_sec_uid: str, fields: dict) -> bool:
        """按**原 sec_uid** 定位账号并局部更新。

        为什么不能只用 `upsert_account`：它是按 sec_uid 匹配的，改 sec_uid 本身
        （短链解析回标准形态、账号换绑）会匹配不到 → 变成「插一条新的、旧的还在」
        的重复账号。这个接口专门用来就地改字段，返回是否真的改了行。
        """
        ...

    async def delete_account(self, sec_uid: str) -> None: ...

    # ---------------- 表 2 视频快照 ----------------
    async def exists_snapshot(self, idem_key: str) -> bool: ...

    async def batch_create_snapshots(self, rows: list[dict]) -> int: ...

    async def previous_snapshot(self, video_id: str, run_id: str) -> dict | None:
        """取该视频**本轮之前**最近一条快照，用于算增量。"""
        ...

    async def list_snapshots(self, limit: int = 100, video_id: str | None = None) -> list[dict]: ...

    # ---------------- 表 3 增量与告警 ----------------
    async def exists_delta(self, idem_key: str) -> bool: ...

    async def create_deltas(self, rows: list[dict]) -> int: ...

    async def list_deltas(self, limit: int = 100, only_alert: bool = False) -> list[dict]: ...

    async def mark_alerted(self, idem_keys: list[str], when: str) -> int: ...

    # ---------------- 表 4 评论命中 ----------------
    async def upsert_comment_hits(self, rows: list[dict]) -> int: ...

    async def list_comment_hits(self, status: str | None = None, limit: int = 100) -> list[dict]: ...

    async def decide_comment_hits(self, decisions: dict[str, str]) -> int:
        """人工确认结果落库。decisions: {comment_id: 'approved'|'ignored'}

        按 **comment_id** 定位，不按 thread_id：命中记录的去重键只含 comment_id
        （一条评论一生只落一次表），跨轮次复用同一条记录，
        所以确认时要按评论找，而不是按「哪一轮发现的」找。
        """
        ...

    # ---------------- 表 5 扫描轮次 ----------------
    async def create_round(self, row: dict) -> None: ...

    async def finish_round(self, run_id: str, **fields: Any) -> None: ...

    async def list_rounds(self, limit: int = 50) -> list[dict]: ...

    # ---------------- 推送日志（点赞告警 + 评论提醒共用）----------------
    async def try_log_alert(
        self, run_id: str, video_id: str, channel: str, payload: str, kind: str = "alert"
    ) -> bool:
        """尝试登记一次推送。返回 True 表示本次是新推送，False 表示已推过（幂等拦截）。

        `kind`：`alert` = 点赞告警，`review` = 待确认拟回复的提醒。
        两类推送共用一张日志表（都是「往外发了一条消息」），靠 kind 区分——
        这样「今天到底推出去多少条、有没有推失败」只有一个地方要查。
        """
        ...

    async def list_alerts(self, limit: int = 50, kind: str | None = None) -> list[dict]: ...

    # ---------------- 概览 ----------------
    async def stats(self) -> dict:
        """看板顶部的汇总数字。"""
        ...
