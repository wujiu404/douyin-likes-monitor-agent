"""browser provider 的纯函数解析测试（离线，不依赖 Playwright/网络）。

样例载荷对齐抖音 web 端 XHR 的真实字段名
（aweme_list[].aweme_id/statistics/create_time/music、comments[].cid/text）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.providers.browser import parse_aweme, parse_aweme_list, parse_comment


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _aweme(aid: str, created_ts: int, likes: int = 100) -> dict:
    return {
        "aweme_id": aid,
        "desc": "夏天最后一支冰汽水 #vlog",
        "create_time": created_ts,
        "statistics": {"digg_count": likes, "comment_count": 5, "share_count": 2},
        "music": {"title": "晚风", "author": "伍佰"},
        "author": {"nickname": "某博主"},
    }


def test_parse_aweme_full() -> None:
    ts = _now_ts() - 3600
    v = parse_aweme(_aweme("7400000000000000001", ts), "账号A")
    assert v is not None
    assert v["video_id"] == "7400000000000000001"
    assert v["account"] == "某博主"  # author.nickname 优先于传入账号名
    assert v["likes"] == 100 and v["comments"] == 5 and v["shares"] == 2
    assert v["music"] == {"title": "晚风", "artist": "伍佰"}
    assert v["source"] == "browser"
    assert v["publish_time"].endswith("+00:00")


def test_parse_aweme_dirty_items_return_none() -> None:
    ts = _now_ts()
    assert parse_aweme({}, "x") is None                       # 缺 aweme_id
    assert parse_aweme({"aweme_id": "1"}, "x") is None        # 缺 create_time → None
    assert parse_aweme({"aweme_id": "", "create_time": ts}, "x") is None
    # statistics 缺失不致命，走 0 默认
    v = parse_aweme({"aweme_id": "7401", "create_time": ts}, "账号A")
    assert v is not None and v["likes"] == 0


def test_parse_aweme_list_filters_by_window() -> None:
    now = _now_ts()
    fresh = _aweme("741", now - 3600, likes=10)
    old = _aweme("742", now - 10 * 86400)  # 10 天前，超出 3 天窗口
    out, passed = parse_aweme_list(
        {"aweme_list": [fresh, old], "has_more": 0}, "账号A", days=3
    )
    assert [v["video_id"] for v in out] == ["741"]
    assert passed is True  # 出现窗口外视频 → 可以停止滚动


def test_parse_aweme_list_all_fresh() -> None:
    now = _now_ts()
    out, passed = parse_aweme_list(
        {"aweme_list": [_aweme("741", now - 60), _aweme("742", now - 120)]},
        "账号A",
        days=3,
    )
    assert len(out) == 2
    assert passed is False


def test_parse_comment() -> None:
    c = parse_comment({"cid": "743", "text": "什么歌呀好好听", "create_time": _now_ts() - 60})
    assert c == {
        "comment_id": "743",
        "content": "什么歌呀好好听",
        "comment_time": c["comment_time"],
    }
    assert c["comment_time"].endswith("+00:00")


def test_parse_comment_dirty() -> None:
    assert parse_comment({}) is None
    assert parse_comment({"cid": "743"}) is None          # 没 text → None
    assert parse_comment({"text": "x"}) is None           # 没 cid → None
    # create_time 缺失 → comment_time 为空串但不炸
    c = parse_comment({"cid": "1", "text": "hi"})
    assert c is not None and c["comment_time"] == ""


def test_parse_comment_truncates_long_text() -> None:
    c = parse_comment({"cid": "1", "text": "长" * 600})
    assert c is not None and len(c["content"]) == 500
