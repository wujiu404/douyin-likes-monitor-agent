"""视频 ID 判据的测试。

这个判据决定两件事：卡片上给不给可点链接、browser 肯不肯去抓这个视频的评论。
两处语义必须一致，所以判据本身也要被钉住。
"""
from __future__ import annotations

from app.core.video_id import is_real_video_id, video_url


def test_real_aweme_id_is_recognized() -> None:
    for vid in ("7412345678901234567", "7684976770591853834", "123456789012345"):
        assert is_real_video_id(vid), vid


def test_simulated_and_dirty_ids_are_rejected() -> None:
    for vid in ("acc01_v01", "acc02_v06", "", None, "  ", "abc", "12345", "74123456789012345678901234"):
        assert not is_real_video_id(vid), vid


def test_video_url_only_for_real_ids() -> None:
    assert video_url("7684976770591853834") == "https://www.douyin.com/video/7684976770591853834"
    # 模拟 id 拼出来必然 404，所以返回空串让调用方少显示一行
    assert video_url("acc01_v01") == ""
    assert video_url("") == ""
    assert video_url(None) == ""


def test_whitespace_is_tolerated() -> None:
    assert is_real_video_id("  7684976770591853834  ")
