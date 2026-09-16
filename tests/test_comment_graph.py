"""评论子图的人工确认测试（interrupt 挂起 → Command(resume) 恢复）。

这里锁住三条容易踩空的性质：
① **挂起点是 human_review**，且此时《评论命中表》**已经**有 pending 记录。
   早先的写法把落库放在 human_review 之后 —— interrupt 会立刻中断，
   落库永远执行不到，前端「待确认」列表直接死锁。已修，用测试钉住。
② **keywords 必须能被节点读到**。LangGraph 会静默丢掉未在 State 里声明的键，
   所以 CommentState 里少了 `keywords` 字段时，关键词匹配会拿到空列表、
   静默命中 0 条（不报错，最难查）。已修，用测试钉住。
③ 恢复之后 status 才从 pending 变成 approved / ignored。
"""
from __future__ import annotations

from app.scan_service import run_scan

KEYWORDS = ["什么歌", "歌曲名", "歌名", "BGM", "好听"]


async def _make_one_thread(deps) -> str:
    """跑一轮扫描，拿到一个停在本轮新命中上的评论线程。

    `comment_scope` 默认是 `all`（本轮采集到的全部视频），所以**第 1 轮**就会
    起评论子图。这里刻意不再跑第 2 轮：第 2 轮同一条评论已经落过表，
    `persisted == 0` → `human_review` 不挂起（见 test_same_comment_..._only_once）。
    """
    r1 = await run_scan(deps, "manual")
    assert r1["comment_threads"], "第 1 轮就该起评论子图（comment_scope=all）"
    return r1["comment_threads"][0]


async def test_subgraph_suspends_at_human_review(deps) -> None:
    thread_id = await _make_one_thread(deps)

    snap = await deps.comment.aget_state({"configurable": {"thread_id": thread_id}})
    assert snap.next == ("human_review",), f"应停在 human_review，实际 {snap.next}"


async def test_pending_records_exist_before_human_confirmation(deps) -> None:
    """关键回归：挂起时就必须已经落库（否则前端拿不到待确认项）。"""
    thread_id = await _make_one_thread(deps)

    rows = await deps.storage.list_comment_hits(status="pending", limit=500)
    mine = [r for r in rows if r["thread_id"] == thread_id]
    assert mine, "human_review 挂起时评论命中记录就该已经落库"
    assert all(r["status"] == "pending" for r in mine)
    assert all(r["thread_id"] == thread_id for r in mine)
    # thread_id 必须和 run_id 一起落表，否则主图轨迹关联不上
    assert all(r["run_id"] == thread_id.split(":")[0] for r in mine)


async def test_keywords_reach_the_node(deps) -> None:
    """关键回归：State 里漏声明 keywords 会让命中数静默变成 0。"""
    thread_id = await _make_one_thread(deps)
    rows = [
        r for r in await deps.storage.list_comment_hits(status=None, limit=500)
        if r["thread_id"] == thread_id
    ]
    assert rows, "该线程应有命中记录"
    assert any(r["keywords"] for r in rows), "没有任何记录带命中关键词 → keywords 很可能没传进状态"
    for r in rows:
        for kw in (r["keywords"] or "").split(","):
            assert kw in KEYWORDS


async def test_drafts_are_generated_for_every_hit(deps) -> None:
    thread_id = await _make_one_thread(deps)
    rows = [
        r for r in await deps.storage.list_comment_hits(status=None, limit=500)
        if r["thread_id"] == thread_id
    ]
    assert all(r["draft"].strip() for r in rows), "每条命中都应有拟回复（模板兜底也算）"
    # 同一个视频下不该人人一字不差（措辞按 comment_id 变化）
    assert len({r["draft"] for r in rows}) > 1


async def test_resume_applies_decisions(deps) -> None:
    thread_id = await _make_one_thread(deps)
    config = {"configurable": {"thread_id": thread_id}}

    before = [
        r for r in await deps.storage.list_comment_hits(status=None, limit=500)
        if r["thread_id"] == thread_id
    ]
    assert len(before) >= 2

    from langgraph.types import Command

    decisions = {r["comment_id"]: ("approved" if i == 0 else "ignored") for i, r in enumerate(before)}
    await deps.comment.ainvoke(Command(resume={"decisions": decisions}), config)

    snap = await deps.comment.aget_state(config)
    assert snap.next == (), f"恢复后子图应交出控制权，实际停在 {snap.next}"

    after = {
        r["comment_id"]: r["status"]
        for r in await deps.storage.list_comment_hits(status=None, limit=500)
        if r["thread_id"] == thread_id
    }
    assert after == decisions
    assert list(after.values()).count("approved") == 1
    assert list(after.values()).count("ignored") == len(before) - 1


async def test_resume_is_scoped_to_one_thread(deps) -> None:
    """恢复一个线程不该动到别的线程——每个视频一条独立线程。"""
    r1 = await run_scan(deps, "manual")
    threads = r1["comment_threads"]
    assert len(threads) >= 2

    target, other = threads[0], threads[1]
    rows = [
        r for r in await deps.storage.list_comment_hits(status=None, limit=500)
        if r["thread_id"] == target
    ]
    from langgraph.types import Command

    await deps.comment.ainvoke(
        Command(resume={"decisions": {r["comment_id"]: "approved" for r in rows}}), 
        {"configurable": {"thread_id": target}},
    )

    other_rows = [
        r for r in await deps.storage.list_comment_hits(status=None, limit=500)
        if r["thread_id"] == other
    ]
    assert other_rows, "另一个线程的记录仍应在"
    assert all(r["status"] == "pending" for r in other_rows), "不该被连带改动"
    # 另一个线程仍然挂着，等它自己的人工确认
    snap = await deps.comment.aget_state({"configurable": {"thread_id": other}})
    assert snap.next == ("human_review",)


async def test_persist_node_is_idempotent_on_replay(deps) -> None:
    """checkpointer 恢复时会重放节点，`persist_drafts` 因此必须幂等。

    直接在节点层面测（而不是绕整张图）：这是最容易在重放时产生脏数据的地方，
    幂等键是 `thread_id:comment_id`。
    """
    from app.nodes.comments.review import make_persist_drafts

    node = make_persist_drafts(deps.storage)
    state = {
        "thread_id": "R_REPLAY:acc01_v01",
        "run_id": "R_REPLAY",
        "video": {"video_id": "acc01_v01", "account": "测试账号 A"},
        "song": {"title": "孤勇者", "artist": "陈奕迅"},
        "hits": [
            {"comment_id": "c1", "content": "这是什么歌", "comment_time": "", "keywords": ["什么歌"]},
            {"comment_id": "c2", "content": "BGM 求歌名", "comment_time": "", "keywords": ["BGM", "歌名"]},
        ],
        "drafts": [
            {"comment_id": "c1", "draft": "回复一"},
            {"comment_id": "c2", "draft": "回复二"},
        ],
    }

    first = await node(state)
    assert first["persisted"] == 2

    await node(state)                       # ← 重放同一个节点
    await node(state)                       # ← 再重放一次

    rows = await deps.storage.list_comment_hits(status=None, limit=500)
    assert len(rows) == 2, "重放不该产生重复的评论命中记录"
    assert {r["status"] for r in rows} == {"pending"}


async def test_apply_decisions_is_idempotent_on_replay(deps) -> None:
    """`apply_decisions` 重放只是把同样的状态再写一遍。"""
    from app.nodes.comments.review import make_apply_decisions, make_persist_drafts

    thread = "R_REPLAY2:acc01_v01"
    await make_persist_drafts(deps.storage)(
        {
            "thread_id": thread, "run_id": "R_REPLAY2",
            "video": {"video_id": "acc01_v01", "account": "A"}, "song": {},
            "hits": [{"comment_id": "c1", "content": "x", "comment_time": "", "keywords": ["好听"]}],
            "drafts": [{"comment_id": "c1", "draft": "d"}],
        }
    )
    node = make_apply_decisions(deps.storage)
    state = {"thread_id": thread, "decisions": {"c1": "approved"}}
    await node(state)
    await node(state)

    rows = [r for r in await deps.storage.list_comment_hits(status=None, limit=500) if r["thread_id"] == thread]
    assert len(rows) == 1
    assert rows[0]["status"] == "approved"


async def test_same_comment_recorded_and_reminded_only_once(deps) -> None:
    """同一条评论跨轮次**只落一次表、只提醒一次、也不重复挂起**。

    幂等键按 `comment_id`，不含 `thread_id`。早先含 thread_id，于是每轮扫描都把
    同一批评论重新记一遍——演示档 3 分钟一轮，表里肉眼可见地翻倍，
    待确认列表被灌水，人还得从一堆一模一样的行里挑。
    评论 id 是全局唯一的：同一条评论被重复采到就是同一条，不是「不同的人问同一件事」。
    """
    r1 = await run_scan(deps, "manual")
    r2 = await run_scan(deps, "manual")
    r3 = await run_scan(deps, "manual")

    rows = await deps.storage.list_comment_hits(status=None, limit=1000)
    cids = [r["comment_id"] for r in rows]
    assert cids, "第 1 轮之后应该已有评论命中记录"
    assert len(cids) == len(set(cids)), "同一条评论不该出现两行"

    t1 = [t for t in r1["comment_threads"] if t.endswith("acc01_v01")][0]
    t3 = [t for t in r3["comment_threads"] if t.endswith("acc01_v01")][0]
    assert t1 != t3, "每轮仍然是独立的子图线程"

    by_video = [r for r in rows if r["video_id"] == "acc01_v01"]
    assert by_video, "该视频应有命中记录"
    # 记录保留在「第一次发现它的那一轮」的线程上，不随后续轮次搬家
    assert {r["thread_id"] for r in by_video} == {t1}
    assert all(r["run_id"] == t1.split(":")[0] for r in by_video)

    # 第 2 轮起（同一条评论已落过表）没有任何新命中 → 不为老评论再挂起一次
    t2 = [t for t in r2["comment_threads"] if t.endswith("acc01_v01")][0]
    snap = await deps.comment.aget_state({"configurable": {"thread_id": t2}})
    assert snap.next == (), f"没有新命中时不该再挂起，实际停在 {snap.next}"

    # 提醒也只推一次（幂等键 (comment_review, comment_id, 渠道)）
    reminders = await deps.storage.list_alerts(limit=1000, kind="review")
    assert reminders, "评论命中应该推过提醒（选做要求的最后一步）"
    keys = [(r["run_id"], r["video_id"]) for r in reminders]
    assert len(keys) == len(set(keys)), "同一条评论不该被提醒两次"


async def test_comment_scope_alerted_scans_only_alerted_videos(deps) -> None:
    """`comment_scope` 的两种取值，以及默认值确实是 `all`。

    选做要求「扫描视频评论」并没有和「点赞增量超阈值」绑定。默认只扫告警视频的话，
    基础要求 4 的阈值 300 一设上去，真实账号永远不告警 → 选做永远不跑。
    """
    from app.providers.mock import ACCOUNT_VIDEO_COUNT

    root = await deps.storage.get_config()
    assert root["comment_scope"] == "all", "默认必须是 all，否则选做会被基础要求 4 隐式关掉"

    accounts = len(await deps.storage.list_accounts())
    r1 = await run_scan(deps, "manual")
    assert len(r1["comment_threads"]) == accounts * ACCOUNT_VIDEO_COUNT, "all：每个视频一条线程"
    assert len(await deps.storage.list_comment_hits(status=None, limit=1000)) > 0

    # 阈值拉到不可能命中 → 本轮一定没有告警；再切到 alerted，应当一条都不扫
    await deps.storage.set_config("threshold", 10**9)
    await deps.storage.set_config("comment_scope", "alerted")
    r2 = await run_scan(deps, "manual")
    assert r2["alert_count"] == 0
    assert r2["comment_threads"] == [], "alerted 且无告警时不该起评论子图"
