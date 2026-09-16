"""告警卡片模板。

飞书「群 webhook 卡片」和「应用消息卡片」用的是**同一套 JSON 结构**，
差别只在投递方式，所以渲染只写一份——两个渠道必须长得一样，
否则「群里看到的」和「私聊看到的」会慢慢跑偏。

卡片里的数字全部来自本轮真实的增量记录，不做任何美化或补零。

**链接只给真实视频**：mock provider 的 `video_id` 是 `acc01_v01` 这种，
拼出来的 `douyin.com/video/acc01_v01` 必然 404（收到告警的人点一下就知道数据是假的）。
所以判据收敛在 `core/video_id.py`，不是真实 aweme_id 就不给链接，
并在卡片上明说这是模拟数据——宁可少一行链接，也不能给个打不开的。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.video_id import video_url

CST = timezone(timedelta(hours=8))

# 一条卡片最多列几个视频；超出的折成一行「另有 N 条」
MAX_ITEMS = 20

TEMPLATES = {"red": "red", "blue": "blue", "orange": "orange"}

# 没有真实视频可跳转时的提示语
NO_LINK_HINT = "（无视频链接）"


def _link_line(video_id: object) -> str:
    """真实 id → 可点链接；模拟 id → 原样带上 id 并说明是模拟数据。

    模拟 id 也照常显示（`acc01_v01`），因为它能被快照表 / 日志直接对上，
    排查「这条是哪来的」时有用；但绝不给它拼链接。
    """
    url = video_url(video_id)
    if url:
        return f"\n[查看视频]({url})"
    vid = str(video_id or "").strip()
    if vid:
        return f"\n（模拟数据 {vid}，无真实视频可跳转）"
    return f"\n{NO_LINK_HINT}"


def _keywords_text(value: object) -> str:
    """命中关键词 → 「什么歌、好听」。

    `State` 里的 keywords 是匹配器给出的**列表**，但《评论命中表》里存的是
    逗号分隔的**字符串**。这里两种都收：直接 `"、".join("什么歌")` 会把字符串
    按单字拆开，渲染成「什、么、歌」——看着像乱码，还很难查。
    """
    if isinstance(value, str):
        value = [v.strip() for v in value.replace("，", ",").split(",")]
    items = [str(v).strip() for v in (value or []) if str(v).strip()]
    return "、".join(items) or "-"


def _lines(it: dict) -> list[str]:
    account = it.get("account") or "-"
    title = (it.get("title") or "").strip() or "（无标题）"
    if len(title) > 40:
        title = title[:40] + "…"

    prev = it.get("prev_likes", 0)
    curr = it.get("curr_likes", 0)
    delta = it.get("delta", 0)

    body = f"**{account}**　{title}\n点赞 {prev} → {curr}　增量 **+{delta}**"
    return [body + _link_line(it.get("video_id"))]


def alert_card(
    run_id: str,
    items: list[dict],
    *,
    header_template: str = "red",
    title_prefix: str = "点赞告警",
) -> dict:
    """把本轮告警渲染成一张飞书交互式卡片。"""
    threshold = next((it.get("threshold") for it in items if it.get("threshold") is not None), None)

    meta = f"**轮次** `{run_id}`"
    if threshold is not None:
        meta += f"　**阈值** {threshold}"
    meta += f"　**时间** {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}"

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": meta}},
        {"tag": "hr"},
    ]

    for it in items[:MAX_ITEMS]:
        for line in _lines(it):
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line}})
        elements.append({"tag": "hr"})

    if len(items) > MAX_ITEMS:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"另有 {len(items) - MAX_ITEMS} 条未展示"}
                ],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": TEMPLATES.get(header_template, "red"),
            "title": {"tag": "plain_text", "content": f"{title_prefix} · {len(items)} 条"},
        },
        "elements": elements,
    }


def review_card(thread_id: str, items: list[dict], *, header_template: str = "orange") -> dict:
    """把「有待确认的拟回复」渲染成一张飞书交互式卡片。

    这是选做链路里「发送提醒」那一步的载体。要点：
    - **评论原文 + 拟回复必须都给出**——只给「有 3 条待确认」等于没提醒，
      收到的人还得回看板点开才知道要确认什么。
    - 明确写「**不会自动发送**」。这是合规红线，也要让收到提醒的人放心。
    """
    first = items[0] if items else {}
    account = first.get("account") or "-"
    video_title = (first.get("video_title") or "").strip() or "（无标题）"
    if len(video_title) > 36:
        video_title = video_title[:36] + "…"
    song_title = (first.get("song_title") or "").strip()
    song_artist = (first.get("song_artist") or "").strip()
    song = ""
    if song_title:
        song = f"　**曲目**《{song_title}》" + (f" / {song_artist}" if song_artist else "")
    elif song_artist:
        song = f"　**演唱** {song_artist}"

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{account}**　{video_title}\n"
                    f"**轮次** `{thread_id}`{song}"
                    # 空列表时不显示「模拟数据」提示——那会误导成「这批是假的」
                    f"{_link_line(first.get('video_id')) if items else ''}"
                ),
            },
        },
        {"tag": "hr"},
    ]

    for it in items[:MAX_ITEMS]:
        keywords = _keywords_text(it.get("keywords"))
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**评论**：{it.get('content') or ''}\n"
                        f"**命中**：{keywords}\n"
                        f"**拟回复**：{it.get('draft') or '（未生成）'}"
                    ),
                },
            }
        )
        elements.append({"tag": "hr"})

    if len(items) > MAX_ITEMS:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"另有 {len(items) - MAX_ITEMS} 条未展示"}
                ],
            }
        )

    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": "拟回复仅待你确认，本系统不会自动发送到抖音。",
                }
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": TEMPLATES.get(header_template, "orange"),
            "title": {"tag": "plain_text", "content": f"待确认拟回复 · {len(items)} 条"},
        },
        "elements": elements,
    }


__all__ = ["alert_card", "review_card", "MAX_ITEMS"]
