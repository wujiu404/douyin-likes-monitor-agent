"""评论子图节点 4：draft_reply —— **全项目唯一调 LLM 的节点**。

这是刻意的：判阈值、算增量、解析点赞数、关键词命中全部用代码，
LLM 只负责「把已经确定的信息说成人话」这一件事。

`LLM_ENABLED=false` 或调用失败时走模板兜底——链路照样跑通，只是措辞生硬一点。
**LLM 不可用不该让整条评论链路失效**，这是可用性要求，不是偷懒。

副作用：无（只生成文本，不落库、不发评论）。
再次强调合规红线：拟回复**只落表 + 推提醒**，绝不调用抖音的评论发布接口。
"""
from __future__ import annotations

import logging

import httpx

from app.config import Settings
from app.nodes.comments.song import looks_like_song_name

log = logging.getLogger(__name__)

TEMPLATES = [
    "这首是《{title}》{artist}，喜欢可以搜来听听～",
    "评论区好多人问，曲名是《{title}》{artist}",
    "《{title}》{artist}，拿去不谢～",
]

# 知道是谁唱的、但查不到正式歌名时的措辞。
# 真实数据里这一档很常见（抖音 UGC 原声场景），**宁可说没查到，也不要编一个歌名**。
ARTIST_ONLY_TEMPLATES = [
    "这段是 {artist} 的原声，正式歌名我暂时没查到～",
    "{artist} 唱的这段，歌名我还没查到，你听一下前奏～",
    "评论里问的这首是 {artist} 的作品，正式歌名暂时查不到～",
]

SYSTEM_PROMPT = (
    "你在帮短视频作者回复评论。作者只想知道歌名。"
    "写一句 20 字以内、口语化的中文回复，直接说出曲名和歌手，不要加话题标签、不要用引号。"
    "如果不知道曲名，就礼貌地说没查到、请对方听一下前奏。"
)


def _pick(templates: list[str], seed: str) -> str:
    # 用 comment_id 的字符和做确定性选模板——不能用内置 hash()，它对字符串是随机化的，
    # 会导致同一条评论在重启前后得到不同措辞。
    # 之所以按 comment_id 而不是 video_id：同一个视频下往往有多条命中评论，
    # 按 video_id 会让它们的回复一字不差，看起来像复制粘贴。
    return templates[sum(ord(ch) for ch in seed) % len(templates)]


def _fallback(video: dict, song: dict, comment_id: str = "") -> str:
    title = song.get("title") or ""
    artist = song.get("artist") or ""
    seed = comment_id or video.get("video_id", "")

    # 最后一道闸：`identify_song` 已经过滤过噪声，这里再用**同一个**判据兜一次。
    # 拟回复是给人看的东西，宁可说"没查到"，也不能把「@某某创作的原声」当歌名糊出去。
    if title and looks_like_song_name(title):
        artist_part = f" — {artist}" if artist else ""
        return _pick(TEMPLATES, seed).format(title=title, artist=artist_part)

    if artist:
        return _pick(ARTIST_ONLY_TEMPLATES, seed).format(artist=artist)
    return "抱歉这首我暂时没查到曲名，你听一下前奏～"


async def _llm_draft(settings: Settings, comment: str, song: dict) -> str | None:
    if not settings.llm_enabled or not settings.llm_api_key:
        return None

    song_desc = song.get("title") or "（未知曲目）"
    if song.get("artist"):
        song_desc += f" — {song['artist']}"

    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"评论原文：{comment}\n已知曲目：{song_desc}"},
        ],
        "temperature": 0.7,
        "max_tokens": 80,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=payload,
            )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - LLM 挂了就走模板，不让链路失败
        log.warning("LLM 拟回复失败，改用模板兜底：%s", exc)
        return None


def make_draft_reply(settings: Settings):
    async def draft_reply(state: dict) -> dict:
        video = state.get("video") or {}
        song = state.get("song") or {}
        hits = state.get("hits") or []

        drafts: list[dict] = []
        for h in hits:
            content = h.get("content", "")
            text = await _llm_draft(settings, content, song)
            source = "llm"
            if not text:
                text = _fallback(video, song, h.get("comment_id", ""))
                source = "template"
            drafts.append(
                {
                    "comment_id": h.get("comment_id", ""),
                    "content": content,
                    "keywords": h.get("keywords", []),
                    "draft": text,
                    "generated_by": source,
                }
            )

        log.info("生成 %d 条拟回复（%s）", len(drafts), "LLM" if settings.llm_enabled else "模板")
        return {"drafts": drafts}

    return draft_reply
