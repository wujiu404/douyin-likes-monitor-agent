"""FeishuStorage 的离线测试（用假 BitableClient，不碰网络）。

钉住的是一类**只在同进程生命周期内出现**的 bug：新建记录后往内存幂等索引里
回填的是空串而不是真实 record_id，于是「按幂等键更新」静默失效，
而重启（reindex 重建出真 id）之后又好了 —— 这种最难查。

用例都是纯内存的，`client` 被替换成 FakeClient，`tables` 直接手填。
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.storage.feishu import FeishuStorage


class FakeClient:
    """记账用的假 BitableClient。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, list[dict]]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.searchable: dict[tuple[str, str], list[dict]] = {}
        self._seq = 0

    async def batch_create(self, table_id: str, rows: list[dict]) -> list[dict]:
        """返回**创建结果**（含 record_id），和真实实现一致。"""
        self.created.append((table_id, rows))
        out = []
        for fields in rows:
            self._seq += 1
            out.append({"record_id": f"rec{self._seq:03d}", "fields": dict(fields)})
        return out

    async def update_record(self, table_id: str, record_id: str, fields: dict) -> None:
        self.updated.append((table_id, record_id, fields))

    async def search(self, table_id: str, field: str, values: list[str]) -> list[dict]:
        out: list[dict] = []
        for v in values:
            out.extend(self.searchable.get((table_id, v), []))
        return out


@pytest.fixture
def storage() -> FeishuStorage:
    st = FeishuStorage(
        Settings(
            feishu_app_id="cli_x",
            feishu_app_secret="s",
            feishu_app_token="t",
        ),
        seed_defaults=False,
    )
    st.client = FakeClient()  # type: ignore[assignment]
    st.tables = {"deltas": "tblD", "snapshots": "tblS", "comment_hits": "tblC", "config": "tblF"}
    return st


def _delta_row(idem_key: str) -> dict:
    return {
        "idem_key": idem_key, "run_id": "R1", "video_id": "v1", "account": "A",
        "title": "t", "prev_likes": 1, "curr_likes": 5, "delta": 4, "is_alert": True,
    }


async def test_created_deltas_are_indexed_with_real_record_id(storage) -> None:
    """★ 回归：新建增量的幂等索引必须存 record_id，不能只标记「存在」。"""
    n = await storage.create_deltas([_delta_row("R1:v1:delta")])
    assert n == 1
    assert storage._delta_index["R1:v1:delta"] == "rec001"


async def test_mark_alerted_updates_the_right_record(storage) -> None:
    """★ 回归：推送成功后，《增量与告警表》的「已推送」要真的被写进去。

    早期 `create_deltas` 往索引里塞空串，`mark_alerted` 拿空串当 record_id 去 PUT，
    结果「本轮新建的告警行」永远标不上已推送（重启后才正常）。
    """
    await storage.create_deltas([_delta_row("R1:v1:delta")])
    changed = await storage.mark_alerted(["R1:v1:delta"], "2026-01-01T00:00:00+00:00")

    assert changed == 1
    table, rid, fields = storage.client.updated[-1]
    assert table == "tblD"
    assert rid == "rec001", "必须更新真实 record_id，不能是空串"
    assert fields["已推送"] is True
    assert fields["推送时间"] == "2026-01-01T00:00:00+00:00"


async def test_mark_alerted_ignores_unknown_key(storage) -> None:
    """索引里压根没有的键（没建过）不该去更新任何记录。"""
    assert await storage.mark_alerted(["R9:v9:delta"], "now") == 0
    assert storage.client.updated == []


async def test_mark_alerted_falls_back_to_search(storage) -> None:
    """索引里只记了「存在」没记 id 时，兜底查一次而不是直接放弃。"""
    storage._delta_index["R1:v1:delta"] = ""       # 模拟索引不完整
    storage.client.searchable[("tblD", "R1:v1:delta")] = [
        {"record_id": "rec777", "fields": {"幂等键": "R1:v1:delta"}}
    ]
    changed = await storage.mark_alerted(["R1:v1:delta"], "now")
    assert changed == 1
    assert storage.client.updated[-1][1] == "rec777"
    assert storage._delta_index["R1:v1:delta"] == "rec777", "查到后要缓存下来"


async def test_batch_create_returns_records_not_count(storage) -> None:
    """BitableClient.batch_create 的契约：返回记录本身，调用方才拿得到 record_id。"""
    created = await storage.client.batch_create("tblS", [{"a": 1}, {"a": 2}])
    assert isinstance(created, list) and len(created) == 2
    assert all("record_id" in c for c in created)


async def test_snapshots_and_comments_also_indexed(storage) -> None:
    await storage.batch_create_snapshots([
        {"idem_key": "R1:v1", "run_id": "R1", "scanned_at": "t", "account": "A",
         "video_id": "v1", "title": "x", "likes": 1, "comments": 0, "shares": 0, "source": "browser"},
    ])
    assert storage._snap_index["R1:v1"] == "rec001"

    await storage.upsert_comment_hits([
        {"idem_key": "R1:v1:c1", "thread_id": "R1:v1", "run_id": "R1", "video_id": "v1",
         "account": "A", "comment_id": "c1", "content": "什么歌", "keywords": "什么歌",
         "song_title": "s", "song_artist": "a", "draft": "d", "created_at": "t"},
    ])
    assert storage._comment_index["R1:v1:c1"] == "rec002"
