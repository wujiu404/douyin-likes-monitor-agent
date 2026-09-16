"""关键词命中（纯 Python 规则，**不调 LLM**）。

要拒绝的典型场景：「不好听」里含「好听」，但它表达的是负面评价，
不该被当成「有人在夸」而触发拟回复。

做法：看命中词**前面两个字符**里有没有否定词。这是个粗糙但可解释的启发式——
它明确的失效场景是「不好听吗？其实挺好」，这种情况交给人工确认那一步兜住。

本模块是纯函数、无 IO，是测试的重点覆盖对象。
"""
from __future__ import annotations

NEGATIONS = ("不", "没", "别", "无", "非", "勿", "讨厌", "难听", "差点", "算不上", "算不上")


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """返回命中的关键词：去重、按字典序排序，保证调用方拿到稳定结果。"""
    if not text:
        return []

    hits: list[str] = []
    for kw in keywords:
        if not kw:
            continue
        start = 0
        while True:
            idx = text.find(kw, start)
            if idx < 0:
                break
            prefix = text[max(0, idx - 2):idx]
            if not any(neg in prefix for neg in NEGATIONS):
                hits.append(kw)
                break
            # 这次被否定词挡掉了，往后找下一次出现
            start = idx + len(kw)
    return sorted(set(hits))


def has_hit(text: str, keywords: list[str]) -> bool:
    return bool(match_keywords(text, keywords))


# ---------------------------------------------------------------- 图节点


def make_match_keywords():
    """评论子图节点 2：match_keywords —— 纯规则命中，**不调 LLM**。"""

    async def match_keywords_node(state: dict) -> dict:
        import logging

        keywords = state.get("keywords") or []
        hits: list[dict] = []
        for c in state.get("comments", []):
            matched = match_keywords(c.get("content", ""), keywords)
            if matched:
                hits.append({**c, "keywords": matched})
        logging.getLogger(__name__).info(
            "命中关键词的评论 %d / %d 条", len(hits), len(state.get("comments", []))
        )
        return {"hits": hits}

    return match_keywords_node
