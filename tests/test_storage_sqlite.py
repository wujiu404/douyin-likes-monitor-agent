"""SQLite 业务存储的测试。

核心断言是**幂等**：同一把幂等键写两次，库里只能有一条。
这不是优化，而是 checkpointer 重放节点时数据不脏的前提。
"""
from __future__ import annotations

from app.core.idempotency import alert_key, comment_key, delta_key, new_run_id, snapshot_key


async def test_seed_defaults(storage) -> None:
    cfg = await storage.get_config()
    assert cfg["threshold"] == 300                     # int 已按 type 解析（出厂口径见 DEFAULT_CONFIG）
    assert cfg["scan_mode"] == "cron"
    assert cfg["comment_keywords"].startswith("什么歌")
    # 评论扫描范围出厂是 all：选做要求独立于「点赞增量超阈值」，不能被阈值拴住
    assert cfg["comment_scope"] == "all"
    accounts = await storage.list_accounts()
    assert len(accounts) == 2


async def test_init_is_idempotent(storage) -> None:
    """重复 init 不该把默认账号灌两遍。"""
    await storage.init()
    accounts = await storage.list_accounts()
    assert len(accounts) == 2
    assert len(await storage.list_config()) == 8


async def test_snapshot_idempotency(storage) -> None:
    run_id = new_run_id()
    key = snapshot_key(run_id, "acc01_v01")
    row = {
        "idem_key": key, "run_id": run_id, "scanned_at": "2026-09-15T08:00:00+00:00",
        "account": "测试账号 A", "video_id": "acc01_v01", "title": "t",
        "publish_time": "", "likes": 100, "comments": 3, "shares": 1, "source": "mock",
    }
    assert await storage.exists_snapshot(key) is False
    await storage.batch_create_snapshots([row])
    assert await storage.exists_snapshot(key) is True

    # 同键再写一次（模拟重放）→ 不新增
    await storage.batch_create_snapshots([row])
    snaps = await storage.list_snapshots(video_id="acc01_v01")
    assert len(snaps) == 1


async def test_previous_snapshot_skips_current_run(storage) -> None:
    base = {
        "scanned_at": "2026-09-15T08:00:00+00:00", "account": "A", "video_id": "v1",
        "title": "t", "publish_time": "", "comments": 0, "shares": 0, "source": "mock",
    }
    await storage.batch_create_snapshots(
        [{**base, "idem_key": snapshot_key("R1", "v1"), "run_id": "R1", "likes": 100}]
    )
    # 本轮是 R2，还没落库 → 上一轮应为 R1
    prev = await storage.previous_snapshot("v1", "R2")
    assert prev is not None and prev["run_id"] == "R1" and prev["likes"] == 100

    await storage.batch_create_snapshots(
        [{**base, "idem_key": snapshot_key("R2", "v1"), "run_id": "R2", "likes": 130}]
    )
    # 本轮是 R2 → 应取 R1，而不是 R2 自己
    prev = await storage.previous_snapshot("v1", "R2")
    assert prev["run_id"] == "R1"
    # 本轮是 R3 → 应取 R2（最近一条）
    prev = await storage.previous_snapshot("v1", "R3")
    assert prev["run_id"] == "R2" and prev["likes"] == 130


async def test_delta_idempotency_and_mark_alerted(storage) -> None:
    run_id = "R2"
    key = delta_key(run_id, "v1")
    rows = [{
        "idem_key": key, "run_id": run_id, "video_id": "v1", "account": "A",
        "title": "t", "prev_likes": 100, "curr_likes": 130, "delta": 30, "is_alert": True,
    }]
    await storage.create_deltas(rows)
    await storage.create_deltas(rows)          # 重放
    deltas = await storage.list_deltas()
    assert len(deltas) == 1
    assert deltas[0]["alerted"] == 0

    assert await storage.mark_alerted([key], "2026-09-15T09:00:00+00:00") == 1
    deltas = await storage.list_deltas()
    assert deltas[0]["alerted"] == 1
    assert deltas[0]["alert_time"] == "2026-09-15T09:00:00+00:00"
    # 未被标记的键 → 影响 0 行
    assert await storage.mark_alerted(["不存在"], "x") == 0


async def test_alert_log_dedup(storage) -> None:
    """同一 (run, video, channel) 只允许登记一次——这是推送级幂等。"""
    assert await storage.try_log_alert("R1", "v1", "local", "{}") is True
    assert await storage.try_log_alert("R1", "v1", "local", "{}") is False
    # 换渠道或换视频都算新的
    assert await storage.try_log_alert("R1", "v1", "feishu_card", "{}") is True
    assert await storage.try_log_alert("R2", "v1", "local", "{}") is True
    assert len(await storage.list_alerts()) == 3


async def test_comment_hits_and_decide(storage) -> None:
    thread = "R1:acc01_v01"
    rows = [
        {
            "idem_key": comment_key(f"c{i}"), "thread_id": thread, "run_id": "R1",
            "video_id": "acc01_v01", "account": "A", "comment_id": f"c{i}",
            "content": f"评论 {i}", "comment_time": "", "keywords": "好听",
            "song_title": "孤勇者", "song_artist": "陈奕迅", "draft": "回复",
        }
        for i in range(3)
    ]
    assert await storage.upsert_comment_hits(rows) == 3
    # 重放：键按 comment_id 去重，同一条评论不会再落一行
    assert await storage.upsert_comment_hits(rows) == 0
    assert len(await storage.list_comment_hits()) == 3
    assert len(await storage.list_comment_hits(status="pending")) == 3

    # 确认按 comment_id 定位，与哪一轮发现的无关
    changed = await storage.decide_comment_hits({"c0": "approved", "c1": "ignored"})
    assert changed == 2
    assert len(await storage.list_comment_hits(status="approved")) == 1
    assert len(await storage.list_comment_hits(status="ignored")) == 1
    assert len(await storage.list_comment_hits(status="pending")) == 1


async def test_account_upsert_and_delete(storage) -> None:
    await storage.upsert_account({"name": "新号", "sec_uid": "u_new", "homepage": "", "enabled": True})
    assert len(await storage.list_accounts()) == 3
    await storage.upsert_account({"name": "改过名的号", "sec_uid": "u_new", "enabled": False})
    assert len(await storage.list_accounts()) == 2              # 禁用后不计入 enabled 列表
    assert len(await storage.list_accounts(only_enabled=False)) == 3
    named = [a for a in await storage.list_accounts(only_enabled=False) if a["sec_uid"] == "u_new"]
    assert named[0]["name"] == "改过名的号"

    await storage.delete_account("u_new")
    assert len(await storage.list_accounts(only_enabled=False)) == 2


async def test_update_account_can_change_sec_uid_in_place(storage) -> None:
    """就地改 sec_uid —— 短链解析回标准形态时必须走这条，不能 upsert。

    `upsert_account` 按 sec_uid 匹配，改了 sec_uid 就匹配不到 → 变成「插一条新的、
    旧的还在」，账号表里留下一个永远解析失败的僵尸账号（本项目真实踩过）。
    """
    before = len(await storage.list_accounts(only_enabled=False))
    old_uid = "MS4wLjABAAAA_demo_account_a"
    new_uid = "MS4wLjABAAAAtesttesttesttest"
    ok = await storage.update_account(old_uid, {"sec_uid": new_uid, "name": "A 改过名"})
    assert ok is True

    after = await storage.list_accounts(only_enabled=False)
    assert len(after) == before, "就地更新不能新增行"
    assert not [a for a in after if a["sec_uid"] == old_uid], "旧的 sec_uid 应当已经不存在"
    row = [a for a in after if a["sec_uid"] == new_uid]
    assert row and row[0]["name"] == "A 改过名"

    # 只更新传进来的字段：enabled 未被提及，仍是原来的 1
    await storage.update_account(new_uid, {"note": "只改备注"})
    row = [a for a in await storage.list_accounts(only_enabled=False) if a["sec_uid"] == new_uid][0]
    assert row["note"] == "只改备注" and row["enabled"] == 1

    assert await storage.update_account("不存在", {"note": "x"}) is False
    assert await storage.update_account(new_uid, {}) is False


async def test_round_ledger(storage) -> None:
    await storage.create_round({"run_id": "R1", "trigger_type": "manual", "started_at": "2026-09-15T08:00:00+00:00"})
    await storage.finish_round("R1", video_count=12, alert_count=4, source="mock", thread_ids="a,b")
    rows = await storage.list_rounds()
    assert len(rows) == 1
    assert rows[0]["video_count"] == 12
    assert rows[0]["alert_count"] == 4
    assert rows[0]["thread_ids"] == "a,b"
    assert rows[0]["finished_at"]

    # create_round 用 INSERT OR REPLACE 语义，同一 run_id 不该产生第二行
    await storage.create_round({"run_id": "R1", "trigger_type": "manual", "started_at": "x"})
    assert len(await storage.list_rounds()) == 1


async def test_set_config_roundtrip(storage) -> None:
    await storage.set_config("threshold", 10)
    assert (await storage.get_config())["threshold"] == 10
    await storage.set_config("brand_new_key", "hello")       # 未知键 → 新增
    assert (await storage.get_config())["brand_new_key"] == "hello"


async def test_stats_shape(storage) -> None:
    st = await storage.stats()
    for k in ("rounds", "accounts", "snapshots", "deltas", "alerts", "alerts_sent",
              "pending_comments", "comment_hits", "config"):
        assert k in st
    assert st["accounts"] == 2
    assert st["rounds"] == 0
    assert st["last_round"] is None
    assert alert_key("R", "v") == "R:v:alert"
