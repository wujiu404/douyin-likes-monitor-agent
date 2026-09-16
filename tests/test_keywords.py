"""关键词命中的纯函数测试。

这是全项目最该被覆盖的地方：它是**唯一**决定「哪条评论值得回复」的判据，
而且必须是不调 LLM 的确定性代码。否定词陷阱是这里的重点。
"""
from __future__ import annotations

import pytest

from app.nodes.comments.keywords import has_hit, match_keywords

KW = ["什么歌", "歌曲名", "歌名", "BGM", "好听"]


# ---------------------------------------------------------------- 基本命中
@pytest.mark.parametrize(
    "text,expected",
    [
        ("这是什么歌啊，好好听", ["什么歌", "好听"]),
        ("BGM 是什么？求歌名", ["BGM", "歌名"]),
        ("歌名求告知，太戳我了", ["歌名"]),
        ("求完整版，歌曲名是什么", ["歌曲名"]),
        ("没听出来是什么歌", ["什么歌"]),
        ("好听好听，循环一整天", ["好听"]),
        ("歌曲名是什么", ["歌曲名"]),
    ],
)
def test_hits(text: str, expected: list[str]) -> None:
    assert match_keywords(text, KW) == expected


@pytest.mark.parametrize(
    "text",
    [
        "第 100 个赞是我的",
        "路过支持一下",
        "前奏一响就认出来了",
        "这首歌叫什么名字呀",     # 「叫什么名」不是「歌名」
        "",
    ],
)
def test_non_hits(text: str) -> None:
    assert match_keywords(text, KW) == []


# ---------------------------------------------------------------- 否定词陷阱
@pytest.mark.parametrize(
    "text",
    [
        "不好听，别推荐了",     # 「不」紧贴在「好听」前一格
        "这首歌不好听",         # 「不」在窗口内
        "别刷好听了",           # 「别」在窗口内
    ],
)
def test_negation_blocks_hit(text: str) -> None:
    """否定词落在命中词**前两格**内 → 不命中「好听」。"""
    assert match_keywords(text, KW) == []


def test_negation_only_blocks_that_occurrence() -> None:
    """被否掉的是**那一次出现**，后面再出现一次仍应命中。"""
    assert match_keywords("不好听？后面越听越好听", KW) == ["好听"]


# ------------------------------------------- 已知失效场景（固化行为，不是 bug）
@pytest.mark.parametrize(
    "text",
    [
        "没觉得好听",           # 「没」距离「好听」4 格，落在两格窗口外
        "难听死了，哪里好听",   # 「难听」是内容而非紧邻否定，且「哪里」不在否定表内
        "好听吗？我觉得一般",   # 否定/转折在命中词**后面**，逆向否定抓不到
    ],
)
def test_known_false_positives(text: str) -> None:
    """这些是启发式的**已知失效场景**，刻意断言成"会误命中"来固化现状。

    为什么要测"错的那一面"：这样一旦有人改宽/改窄窗口，测试会立刻提醒
    这个行为变了。这类边界靠人工确认那一步兜住——不是靠把规则越堆越复杂。
    """
    assert match_keywords(text, KW) == ["好听"]


# ---------------------------------------------------------------- 结构性质
def test_result_is_deduped_and_sorted() -> None:
    """重复出现只记一次，且结果按字典序——调用方要能拿到稳定顺序。"""
    got = match_keywords("歌名歌名BGM什么歌", KW)
    assert got == ["BGM", "什么歌", "歌名"]
    assert got == sorted(set(got))
    assert got.count("歌名") == 1


def test_ngram_keywords_are_independent() -> None:
    """「歌名」不会因为「歌曲名」存在而重复计入，两者各自独立扫描。"""
    assert match_keywords("歌曲名是什么", KW) == ["歌曲名"]


def test_empty_inputs() -> None:
    assert match_keywords("", KW) == []
    assert match_keywords("这是什么歌", []) == []
    # 空串与 None 都被跳过，等价于「没有配置任何关键词」
    assert match_keywords("这是什么歌", ["", None]) == []      # type: ignore[list-item]


def test_has_hit_agrees_with_match_keywords() -> None:
    for t in ["这是什么歌", "不好听", "路过支持一下", ""]:
        assert has_hit(t, KW) == bool(match_keywords(t, KW))
