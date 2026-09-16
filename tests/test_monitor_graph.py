"""监控主图的端到端测试（全部走 mock，不碰网络）。

覆盖三件在架构上定死、必须被测试锁住的事：
① **首见视频不告警**——不然新视频一出现就是一个巨大"增量"，全是噪声；
② **增量跨阈值才告警，且告警推送幂等**——同一轮同一视频只推一次；
③ **评论子图是 fire-and-forget**——主图不等它，自己先结束。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from langgraph.types import Command

from app.nodes.comment_trigger import pending_background_count
from app.scan_service import run_scan


async def test_first_round_has_no_alerts(deps) -> None:
    """第 1 轮全是首见视频：增量记 0、不告警。

    但评论子图**照跑**：`comment_scope` 默认 `all`，选做的「扫描视频评论」
    不该被基础要求 4 的阈值拴住（阈值 300 时真实小账号永远不告警，
    早期版本「只扫告警视频」会让选做永远不跑）。
    """
    result = await run_scan(deps, "manual")
    assert result["source"] == "mock"
    assert result["video_count"] == 12          # 2 个账号 × 6 条
    assert result["delta_count"] == 12
    assert result["alert_count"] == 0
    assert len(result["comment_threads"]) == 12  # all：每条视频都扫评论

    deltas = await deps.storage.list_deltas()
    assert all(d["delta"] == 0 for d in deltas)
    assert all(d["is_alert"] == 0 for d in deltas)


async def test_second_round_alerts_on_hot_videos(deps) -> None:
    await run_scan(deps, "manual")
    r2 = await run_scan(deps, "manual")

    # 每个账号前两条是「爆款」→ 2 账号 × 2 = 4 条告警
    assert r2["alert_count"] == 4
    # 评论子图起 12 个（每个视频一个），告警视频只是其中 4 个
    assert len(r2["comment_threads"]) == 12
    # thread_id 必须与 run_id 一起落表，否则主图轨迹里的评论结果关联不上
    for tid in r2["comment_threads"]:
        assert tid.startswith(r2["run_id"] + ":")

    # 告警只落在增量超阈值的视频上，且增量都是正的
    alerts = await deps.storage.list_deltas(only_alert=True)
    assert len(alerts) == 4
    assert all(d["delta"] > 20 for d in alerts)
    assert r2["alert_summary"]["channel"] == "local"
    assert r2["alert_summary"]["sent"] == 4
    assert r2["alert_summary"]["deduped"] == 0
    assert r2["alert_summary"]["delivered"] is True


async def test_third_round_dedupes_nothing_because_key_has_run_id(deps) -> None:
    """幂等键含 run_id → 下一轮是**新的**告警，不该被上一轮挡掉。"""
    await run_scan(deps, "manual")
    r2 = await run_scan(deps, "manual")
    r3 = await run_scan(deps, "manual")
    assert r2["alert_summary"]["sent"] == 4
    assert r3["alert_summary"]["sent"] == 4

    # 只看点赞告警：推送日志里还有 kind=review 的评论提醒（两类共用一张表）
    alerts = await deps.storage.list_alerts(limit=100, kind="alert")
    assert len(alerts) == 8
    # 每个 (run_id, video_id) 只出现一次——这就是推送级幂等
    keys = [(a["run_id"], a["video_id"]) for a in alerts]
    assert len(keys) == len(set(keys))


async def test_round_ledger_written(deps) -> None:
    await run_scan(deps, "manual")
    rows = await deps.storage.list_rounds()
    assert len(rows) == 1
    r = rows[0]
    assert r["trigger_type"] == "manual"
    assert r["video_count"] == 12
    assert r["source"] == "mock"
    assert r["finished_at"]                 # finally 里写上了
    assert r["error_count"] == 0


async def test_scan_response_is_self_describing(deps) -> None:
    """扫描返回时就该带上待确认评论数，前端不用再轮询一次。"""
    await run_scan(deps, "manual")
    r2 = await run_scan(deps, "manual")
    assert r2["pending_comments"] > 0
    assert pending_background_count() == 0   # 收尾时已全部排空


async def test_scan_response_exposes_note_and_error_count(deps) -> None:
    """备注与错误数必须出现在扫描响应里。

    「200 但 video_count=0」有两种完全不同的原因：采集坏了、账号真没新作品。
    响应里不带 note 的话，看板和自检脚本都只能猜（2026-09-16 用户就是这么被绕进去的）。
    """
    r = await run_scan(deps, "manual")

    assert "note" in r and "error_count" in r
    assert r["error_count"] == 0


async def test_deltas_are_monotonic_positive_after_first_round(deps) -> None:
    await run_scan(deps, "manual")
    await run_scan(deps, "manual")
    await run_scan(deps, "manual")
    rows = [d for d in await deps.storage.list_deltas(limit=200) if d["run_id"] != ""]
    non_first = [d for d in rows if d["prev_likes"] != d["curr_likes"] or d["delta"] != 0]
    assert non_first, "第 2 轮起应该全是非零增量"
    assert all(d["delta"] > 0 for d in non_first), "mock 的点赞数单调递增，不该出现负增量"


# ---------------------------------------------------------------- 快照 / 增量一致性
async def test_snapshot_written_once_per_run_per_video(deps) -> None:
    await run_scan(deps, "manual")
    await run_scan(deps, "manual")
    snaps = await deps.storage.list_snapshots(limit=200)
    keys = [s["idem_key"] for s in snaps]
    assert len(keys) == len(set(keys))
    assert len(keys) == 24                  # 12 条 × 2 轮


# ---------------------------------------------------------------- fire-and-forget
async def test_comment_subgraph_does_not_block_main_graph(deps) -> None:
    """把评论子图换成一个「永不返回」的协程，主图也必须照常收敛。

    这是架构里最硬的一条：`run_comment_graph` 若改成 await，调度
    （max_instances=1）、run 轨迹收敛、轮次台账三件事会同时坏掉。
    """
    await run_scan(deps, "manual")           # 先跑一轮，让第 2 轮有增量

    started = asyncio.Event()

    async def never_returns(thread_id: str, video: dict, run_id: str) -> None:
        started.set()
        await asyncio.sleep(3600)            # 模拟"永远挂着"

    deps.comment_runner = never_returns
    # 收尾窗口压到 0.2s，这样测的就是「主图本身快不快」，
    # 而不是「等子图等了多久」（等子图那部分有它自己的超时上界）。
    deps.settings.comment_drain_timeout = 0.2

    t0 = time.perf_counter()
    result = await run_scan(deps, "manual")
    elapsed = time.perf_counter() - t0

    assert result["alert_count"] == 4
    assert len(result["comment_threads"]) == 12
    # 主图没被子图拖住：即便 12 个子图永不返回，扫描也在秒级内返回
    assert elapsed < 3.0, f"主图被子图阻塞了 {elapsed:.1f}s"
    assert started.is_set()
    # 台账照样写完（说明主图确实 END 了，不是卡住）
    rounds = await deps.storage.list_rounds()
    assert rounds[0]["thread_ids"]
    assert rounds[0]["error_count"] == 0


async def test_comment_graph_disabled(deps) -> None:
    deps.settings.enable_comment_graph = False
    await run_scan(deps, "manual")
    r2 = await run_scan(deps, "manual")
    assert r2["alert_count"] == 4
    assert r2["comment_threads"] == []
    assert len(await deps.storage.list_comment_hits()) == 0
