"""曲目识别与拟回复的测试。

钉住的是一条真实数据踩出来的坑：抖音 UGC 场景下 `music.title` 是
「@示例账号创作的原声」——**作者标记，不是歌名**。早期直接信这个字段，
拟回复变成「这首是《@示例账号创作的原声》— 示例账号」这种胡话。

**说错比说没查到更糟**：说没查到只是信息缺失，说错是给人错误答案。
所以这里的断言分两半：
① 噪声标题必须被丢掉；
② 丢掉了也不能编，只能如实说「没查到」，但「是谁唱的」这条信息要保住。
"""
from __future__ import annotations

from app.nodes.comments.draft import _fallback
from app.nodes.comments.song import identify_song, looks_like_song_name

# 真实采集到的形态
REAL_ORIGINAL_SOUND = {"title": "@示例账号创作的原声", "artist": "示例账号"}


# ---------------------------------------------------------------- 噪声判定
def test_original_sound_titles_are_rejected() -> None:
    assert looks_like_song_name("@示例账号创作的原声") is False
    assert looks_like_song_name("示例账号创作的原声") is False
    assert looks_like_song_name("原声") is False
    assert looks_like_song_name("@某某") is False
    assert looks_like_song_name("纯音乐") is False
    assert looks_like_song_name("") is False


def test_real_song_names_are_accepted() -> None:
    assert looks_like_song_name("孤勇者") is True
    assert looks_like_song_name("夜空中最亮的星") is True
    # 歌名里带「原声」两字的极端情况会误杀，这是已知的取舍：
    # 误杀只会退化成「没查到」，误判会输出胡话，两害相权取其轻。
    assert looks_like_song_name("Faded") is True


# ---------------------------------------------------------------- 识别顺序
def test_meta_is_used_when_it_looks_like_a_song() -> None:
    song = identify_song(
        {"music": {"title": "孤勇者", "artist": "陈奕迅"}, "account": "某号"}, []
    )
    assert song == {"title": "孤勇者", "artist": "陈奕迅", "source": "meta"}


def test_original_sound_is_skipped_but_artist_kept() -> None:
    """核心回归：原声标题不能当歌名，但歌手信息要留下。"""
    song = identify_song({"music": REAL_ORIGINAL_SOUND, "account": "示例账号"}, [])
    assert song["title"] == ""
    assert song["artist"] == "示例账号"
    assert song["source"] == "unknown"


def test_caption_bracket_wins_over_nothing() -> None:
    """作者常在文案里写「带来一首原创歌曲《心碎的梦》」——这是比原声标记可靠得多的来源。"""
    song = identify_song(
        {
            "music": REAL_ORIGINAL_SOUND,
            "title": "2025年最后一天给大家带来一首原创歌曲《心碎的梦》，愿大家新年都好",
            "account": "示例账号",
        },
        [],
    )
    assert song["title"] == "心碎的梦"
    assert song["artist"] == "示例账号"
    assert song["source"] == "caption"


def test_comment_named_song_is_used_as_last_resort() -> None:
    song = identify_song(
        {"music": REAL_ORIGINAL_SOUND, "title": "现场版 #唱歌", "account": "示例账号"},
        [{"content": "这是什么歌"}, {"content": "歌名是 漠河舞厅，很好听"}],
    )
    assert song["title"] == "漠河舞厅"
    assert song["source"] == "comment"


def test_question_in_comment_is_not_mistaken_for_an_answer() -> None:
    """「歌名是什么」是在问，不是在答——不能把「什么」抠成歌名。"""
    song = identify_song(
        {"music": REAL_ORIGINAL_SOUND, "title": "无", "account": "A"},
        [{"content": "歌名是什么？求告知"}, {"content": "曲名为啥不写"}],
    )
    assert song["title"] == ""


def test_bracket_in_comment_is_accepted() -> None:
    song = identify_song(
        {"music": REAL_ORIGINAL_SOUND, "title": "无", "account": "A"},
        [{"content": "这首《起风了》我循环一整年"}],
    )
    assert song["title"] == "起风了"


# ---------------------------------------------------------------- 措辞
def test_fallback_never_emits_original_sound_as_a_title() -> None:
    """最后一道闸：即使 title 被污染，也不能把「创作的原声」当歌名糊出去。"""
    draft = _fallback(
        {"video_id": "v1", "account": "示例账号"},
        {"title": "@示例账号创作的原声", "artist": "示例账号"},
        "c1",
    )
    assert "原声" in draft or "没查到" in draft or "查不到" in draft
    assert "《@示例账号创作的原声》" not in draft
    assert "— 示例账号，喜欢可以搜来听听" not in draft


def test_fallback_says_who_sings_when_title_unknown() -> None:
    """不知道歌名也要把知道的（谁唱的）说出来，而不是一句「没查到」了事。"""
    draft = _fallback(
        {"video_id": "v1", "account": "示例账号"},
        {"title": "", "artist": "示例账号"},
        "c1",
    )
    assert "示例账号" in draft
    assert "歌名" in draft


def test_fallback_with_title_contains_song_and_artist() -> None:
    """选做要求：拟回复要包含歌手和歌曲名。"""
    draft = _fallback(
        {"video_id": "v1", "account": "A"},
        {"title": "孤勇者", "artist": "陈奕迅"},
        "c1",
    )
    assert "孤勇者" in draft and "陈奕迅" in draft


def test_fallback_is_stable_for_the_same_comment() -> None:
    """同一条评论的措辞必须可复现（不能用随机化的内置 hash）。"""
    args = ({"video_id": "v1", "account": "A"}, {"title": "孤勇者", "artist": "陈奕迅"}, "c1")
    assert _fallback(*args) == _fallback(*args)


def test_fallback_varies_between_comments() -> None:
    """同一视频下的多条评论措辞不能一字不差，否则像复制粘贴。"""
    video, song = {"video_id": "v1", "account": "A"}, {"title": "孤勇者", "artist": "陈奕迅"}
    texts = {_fallback(video, song, f"c{i}") for i in range(8)}
    assert len(texts) > 1
