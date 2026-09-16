"""业务存储 · SQLite 实现。

单连接 + 一把锁把所有 DB 访问串行化——对演示规模足够，也彻底避免 SQLite 写并发问题。
所有写操作都走幂等键（`INSERT OR IGNORE` 或调用前的 `exists_*` 查询）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.core.coerce import cast_config

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
CST = timezone(timedelta(hours=8))

# (key, value, type, scope, note)
DEFAULT_CONFIG: list[tuple[str, str, str, str, str]] = [
    ("scan_mode", "cron", "str", "调度", "cron=按固定时点 | interval=按固定间隔"),
    ("scan_cron_hours", "12,18,22", "str", "调度", "cron 模式下的扫描时点"),
    ("scan_interval_minutes", "3", "int", "调度", "interval 模式下的扫描间隔（分钟）"),
    ("threshold", "300", "int", "阈值", "点赞增量超过它就告警；演示档可改成 10 或 20"),
    ("lookback_days", "3", "int", "采集", "只采集最近 N 天发布的视频"),
    ("comment_keywords", "什么歌,歌曲名,歌名,BGM,好听", "str", "采集", "评论命中关键词，逗号分隔"),
    ("provider_chain", "browser,mock", "str", "采集",
     "数据源降级链，按顺序尝试；演示档填 mock 即为确定性模拟数据"),
    ("comment_scope", "all", "str", "采集",
     "评论扫哪些视频：all=本轮采集的全部 | alerted=仅有告警的"),
]

DEFAULT_ACCOUNTS: list[tuple[str, str, str, str]] = [
    ("测试账号 A", "MS4wLjABAAAA_demo_account_a", "https://www.douyin.com/user/MS4wLjABAAAA_demo_account_a", "演示用"),
    ("测试账号 B", "MS4wLjABAAAA_demo_account_b", "https://www.douyin.com/user/MS4wLjABAAAA_demo_account_b", "演示用"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cast(value: Any, type_: str) -> Any:
    """把《配置表》里的「值」转成目标类型（实现见 `app/core/coerce.py`，与飞书共用）。"""
    return cast_config(value, type_)


class SqliteStorage:
    """实现了 StorageProvider 协议。"""

    backend = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 生命周期
    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._migrate()
        await self._db.commit()
        await self._seed()
        log.info("业务存储就绪：%s", self.path)

    async def _migrate(self) -> None:
        """对**已存在的库**补字段。

        `schema.sql` 里全是 `CREATE TABLE IF NOT EXISTS`——老库表已经在了，
        新加的列不会生效。所以新增列必须在这里显式补，
        并且要幂等（每次启动都会跑）。
        """
        assert self._db is not None
        cols = {row[1] for row in await (await self._db.execute("PRAGMA table_info(alert_log)")).fetchall()}
        if "kind" not in cols:
            # 推送日志原本只记点赞告警，现在也记评论提醒，需要区分类型
            await self._db.execute(
                "ALTER TABLE alert_log ADD COLUMN kind TEXT NOT NULL DEFAULT 'alert'"
            )
            log.info("存储迁移：alert_log 补上 kind 列")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _seed(self) -> None:
        for key, value, type_, scope, note in DEFAULT_CONFIG:
            await self._exec(
                "INSERT OR IGNORE INTO config(key, value, type, scope, note, updated_at) VALUES (?,?,?,?,?,?)",
                (key, value, type_, scope, note, _now()),
            )
        now = _now()
        for name, sec_uid, homepage, note in DEFAULT_ACCOUNTS:
            await self._exec(
                "INSERT OR IGNORE INTO accounts(name, sec_uid, homepage, enabled, note, created_at) "
                "VALUES (?,?,?,1,?,?)",
                (name, sec_uid, homepage, note, now),
            )

    # ------------------------------------------------------------ 底层
    async def _exec(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        assert self._db is not None, "存储未初始化"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            await self._db.commit()
            return cur

    async def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self._db is not None, "存储未初始化"
        async with self._lock:
            cur = await self._db.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
            return [dict(r) for r in rows]

    async def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------ 配置表
    async def list_config(self) -> list[dict]:
        return await self._query(
            "SELECT key, value, type, scope, note, updated_at FROM config ORDER BY scope, key"
        )

    async def get_config(self) -> dict[str, Any]:
        rows = await self._query("SELECT key, value, type FROM config")
        return {r["key"]: _cast(r["value"], r["type"]) for r in rows}

    async def set_config(self, key: str, value: Any) -> None:
        row = await self._one("SELECT type FROM config WHERE key = ?", (key,))
        if row is None:
            await self._exec(
                "INSERT INTO config(key, value, type, scope, note, updated_at) VALUES (?,?,?,?,?,?)",
                (key, str(value), "str", "通用", "", _now()),
            )
            return
        if isinstance(value, bool):
            stored = "true" if value else "false"
        else:
            stored = str(value)
        await self._exec(
            "UPDATE config SET value = ?, updated_at = ? WHERE key = ?", (stored, _now(), key)
        )

    # ------------------------------------------------------------ 表 1 账号
    async def list_accounts(self, only_enabled: bool = True) -> list[dict]:
        sql = "SELECT * FROM accounts"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return await self._query(sql)

    async def upsert_account(self, acc: dict) -> None:
        existing = await self._one("SELECT id FROM accounts WHERE sec_uid = ?", (acc["sec_uid"],))
        if existing:
            await self._exec(
                "UPDATE accounts SET name=?, homepage=?, enabled=?, note=? WHERE sec_uid=?",
                (
                    acc.get("name", ""),
                    acc.get("homepage", ""),
                    1 if acc.get("enabled", True) else 0,
                    acc.get("note", ""),
                    acc["sec_uid"],
                ),
            )
        else:
            await self._exec(
                "INSERT INTO accounts(name, sec_uid, homepage, enabled, note, created_at) VALUES (?,?,?,?,?,?)",
                (
                    acc.get("name", ""),
                    acc["sec_uid"],
                    acc.get("homepage", ""),
                    1 if acc.get("enabled", True) else 0,
                    acc.get("note", ""),
                    _now(),
                ),
            )

    async def update_account(self, old_sec_uid: str, fields: dict) -> bool:
        allowed = {"name", "sec_uid", "homepage", "enabled", "note"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        if "enabled" in sets:
            sets["enabled"] = 1 if sets["enabled"] else 0
        assign = ", ".join(f"{k}=?" for k in sets)
        cur = await self._exec(
            f"UPDATE accounts SET {assign} WHERE sec_uid = ?", (*sets.values(), old_sec_uid)
        )
        return cur.rowcount > 0

    async def delete_account(self, sec_uid: str) -> None:
        await self._exec("DELETE FROM accounts WHERE sec_uid = ?", (sec_uid,))

    # ------------------------------------------------------------ 表 2 快照
    async def exists_snapshot(self, idem_key: str) -> bool:
        return await self._one("SELECT 1 FROM snapshots WHERE idem_key = ?", (idem_key,)) is not None

    async def batch_create_snapshots(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        db = self._db
        assert db is not None
        async with self._lock:
            await db.executemany(
                "INSERT OR IGNORE INTO snapshots"
                "(idem_key, run_id, scanned_at, account, video_id, title, publish_time,"
                " likes, comments, shares, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["idem_key"], r["run_id"], r["scanned_at"], r["account"], r["video_id"],
                        r.get("title", ""), r.get("publish_time", ""),
                        int(r.get("likes", 0)), int(r.get("comments", 0)), int(r.get("shares", 0)),
                        r.get("source", "mock"),
                    )
                    for r in rows
                ],
            )
            await db.commit()
            return len(rows)

    async def previous_snapshot(self, video_id: str, run_id: str) -> dict | None:
        return await self._one(
            "SELECT * FROM snapshots WHERE video_id = ? AND run_id != ? ORDER BY id DESC LIMIT 1",
            (video_id, run_id),
        )

    async def list_snapshots(self, limit: int = 100, video_id: str | None = None) -> list[dict]:
        if video_id:
            return await self._query(
                "SELECT * FROM snapshots WHERE video_id = ? ORDER BY id DESC LIMIT ?",
                (video_id, limit),
            )
        return await self._query("SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ 表 3 增量与告警
    async def exists_delta(self, idem_key: str) -> bool:
        return await self._one("SELECT 1 FROM deltas WHERE idem_key = ?", (idem_key,)) is not None

    async def create_deltas(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        db = self._db
        assert db is not None
        async with self._lock:
            await db.executemany(
                "INSERT OR IGNORE INTO deltas"
                "(idem_key, run_id, video_id, account, title, prev_likes, curr_likes, delta, is_alert, alerted)"
                " VALUES (?,?,?,?,?,?,?,?,?,0)",
                [
                    (
                        r["idem_key"], r["run_id"], r["video_id"], r.get("account", ""), r.get("title", ""),
                        int(r.get("prev_likes", 0)), int(r.get("curr_likes", 0)),
                        int(r.get("delta", 0)), 1 if r.get("is_alert") else 0,
                    )
                    for r in rows
                ],
            )
            await db.commit()
            return len(rows)

    async def list_deltas(self, limit: int = 100, only_alert: bool = False) -> list[dict]:
        sql = "SELECT * FROM deltas"
        if only_alert:
            sql += " WHERE is_alert = 1"
        sql += " ORDER BY id DESC LIMIT ?"
        return await self._query(sql, (limit,))

    async def mark_alerted(self, idem_keys: list[str], when: str) -> int:
        if not idem_keys:
            return 0
        marks = ",".join("?" * len(idem_keys))
        cur = await self._exec(
            f"UPDATE deltas SET alerted = 1, alert_time = ? WHERE idem_key IN ({marks})",
            (when, *idem_keys),
        )
        return cur.rowcount or 0

    # ------------------------------------------------------------ 表 4 评论命中
    async def upsert_comment_hits(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        db = self._db
        assert db is not None
        async with self._lock:
            cur = await db.executemany(
                "INSERT OR IGNORE INTO comment_hits"
                "(idem_key, thread_id, run_id, video_id, account, comment_id, content, comment_time,"
                " keywords, song_title, song_artist, draft, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                [
                    (
                        r["idem_key"], r["thread_id"], r.get("run_id", ""), r["video_id"],
                        r.get("account", ""), r.get("comment_id", ""), r.get("content", ""),
                        r.get("comment_time", ""), r.get("keywords", ""),
                        r.get("song_title", ""), r.get("song_artist", ""), r.get("draft", ""),
                        r.get("created_at") or _now(),
                    )
                    for r in rows
                ],
            )
            await db.commit()
            # 返回**真正插入**的条数，而不是 len(rows)：命中记录按 comment_id 去重，
            # 同一轮里也可能有已被前几轮记录过的评论（INSERT OR IGNORE 静默跳过）。
            # 日志里说「落表 N 条」就得是 N 条新的，否则和表里的实际增长对不上。
            return max(int(cur.rowcount or 0), 0)

    async def list_comment_hits(self, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM comment_hits"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id DESC LIMIT ?"
        return await self._query(sql, (*params, limit))

    async def decide_comment_hits(self, decisions: dict[str, str]) -> int:
        """decisions: {comment_id: 'approved'|'ignored'}

        按 comment_id 定位（命中记录的去重键只含 comment_id），
        这样无论人工是从「发现它的那一轮」还是「之后某一轮」的线程点进来的，都能改到。
        """
        if not decisions:
            return 0
        now = _now()
        changed = 0
        for comment_id, status in decisions.items():
            cur = await self._exec(
                "UPDATE comment_hits SET status = ?, decided_at = ? WHERE comment_id = ?",
                (status, now, comment_id),
            )
            changed += cur.rowcount or 0
        return changed

    # ------------------------------------------------------------ 表 5 扫描轮次
    async def create_round(self, row: dict) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO scan_rounds"
            "(run_id, trigger_type, started_at, account_count, video_count, source, alert_count, error_count, thread_ids, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                row["run_id"], row.get("trigger_type", "manual"), row["started_at"],
                int(row.get("account_count", 0)), int(row.get("video_count", 0)),
                row.get("source", ""), int(row.get("alert_count", 0)),
                int(row.get("error_count", 0)), row.get("thread_ids", ""), row.get("note", ""),
            ),
        )

    async def finish_round(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields.setdefault("finished_at", _now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self._exec(f"UPDATE scan_rounds SET {cols} WHERE run_id = ?", (*fields.values(), run_id))

    async def list_rounds(self, limit: int = 50) -> list[dict]:
        # ⚠ 必须带上 rowid 兜底：started_at 只精确到秒，手动连点两次扫描会落在
        #   同一秒里，只按 started_at DESC 排的话「最近一轮」是**随机**的
        #   （看板会显示错轮次，扫描台账的首行也不对）。rowid 是插入顺序，正好是需要的次级键。
        return await self._query(
            "SELECT * FROM scan_rounds ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------ 告警日志
    async def try_log_alert(
        self, run_id: str, video_id: str, channel: str, payload: str, kind: str = "alert"
    ) -> bool:
        cur = await self._exec(
            "INSERT OR IGNORE INTO alert_log(run_id, video_id, channel, kind, payload, sent_at)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, video_id, channel, kind, payload, _now()),
        )
        return (cur.rowcount or 0) > 0

    async def list_alerts(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        if kind:
            return await self._query(
                "SELECT * FROM alert_log WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)
            )
        return await self._query("SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------ 概览
    async def stats(self) -> dict:
        async def _count(table: str, where: str = "") -> int:
            row = await self._one(f"SELECT COUNT(*) AS n FROM {table} {where}")
            return int(row["n"]) if row else 0

        last = await self._one(
            # 同 list_rounds：started_at 同秒时要靠 rowid 兜底，否则「最近一轮」会取错
            "SELECT * FROM scan_rounds ORDER BY started_at DESC, rowid DESC LIMIT 1"
        )
        return {
            "rounds": await _count("scan_rounds"),
            "accounts": await _count("accounts", "WHERE enabled = 1"),
            "snapshots": await _count("snapshots"),
            "deltas": await _count("deltas"),
            "alerts": await _count("deltas", "WHERE is_alert = 1"),
            # 「已推送」只数点赞告警，别把评论提醒也算进去——
            # 两个数字回答的是不同问题（涨了多少 vs 有几条等你拍板），混在一起就都不准了
            "alerts_sent": await _count("alert_log", "WHERE kind = 'alert'"),
            "reviews_notified": await _count("alert_log", "WHERE kind = 'review'"),
            "pending_comments": await _count("comment_hits", "WHERE status = 'pending'"),
            "comment_hits": await _count("comment_hits"),
            "last_round": last,
            "config": await self.get_config(),
        }
