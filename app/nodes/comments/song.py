"""评论子图节点 3：identify_song —— 识别曲目。

顺序（先便宜后昂贵，先可靠后兜底）：

| 序 | 来源 | 说明 |
|---|---|---|
| ① | `video.music.title` | 第三方曲库时就是真实歌名；**但 UGC 原声时要丢掉**（见下） |
| ② | 视频文案里的《…》 | 作者常在文案里写「带来一首原创歌曲《心碎的梦》」 |
| ③ | 评论里写出的歌名 | 《xxx》 或「歌名是 xxx」 |
| ④ | 兜底 | 返回空 title，措辞退化成「没查到」 |

## 为什么必须过滤「原声」

抖音的 `music.title` 在 UGC 场景下是**作者标记而不是歌名**：

```
music = {"title": "@示例账号创作的原声", "artist": "示例账号"}
```

早期版本直接信这个字段，于是拟回复变成
「这首是**《@示例账号创作的原声》** — 示例账号，喜欢可以搜来听听～」——
一串看着像有内容、实际是胡话的回复。**这比说"没查到"更糟**：
说没查到只是信息缺失，说错是给人错误答案。

`artist` 仍然保留（`music.author` 就是演唱/创作者），所以「歌名未知但有歌手」时
措辞还能说出来是谁唱的——信息不丢，只是不编歌名。

音频指纹识别（ACRCloud 之类）留到 P1：要额外依赖和额度。
真实数据下确实存在「识别不出」的情况，此时**如实说识别不出**，不猜。

副作用：无。
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_TITLE_BRACKET = re.compile(r"《([^》]{1,24})》")
_NAMED = re.compile(r"(?:歌名|歌曲名|曲名)\s*(?:是|为|叫)\s*(?!什么|啥|哪)([^\s，,。！!？?的]{1,24})")

# 「不是歌名」的形态：
#   @xxx创作的原声 / xxx的原声 / 纯音乐标称 / 光秃秃的 @xxx
_NOISE_TITLES = {"纯音乐", "轻音乐", "背景音乐", "bgm", "BGM", "原声"}


def looks_like_song_name(title: str) -> bool:
    """判断一个字符串像不像**歌名**（而不是原声标记 / 作者标记）。"""
    t = (title or "").strip()
    if not t:
        return False
    if "原声" in t:            # 「@某某创作的原声」——UGC 原声标记
        return False
    if t.startswith("@"):      # 「@某某」——作者标记
        return False
    if t in _NOISE_TITLES:
        return False
    return True


def _from_text(text: str) -> str:
    """从一段文本里抠出可能的歌名，抠不到返回空串。"""
    for pattern in (_TITLE_BRACKET, _NAMED):
        m = pattern.search(text or "")
        if m:
            candidate = m.group(1).strip()
            if looks_like_song_name(candidate):
                return candidate
    return ""


def identify_song(video: dict | None, comments: list[dict] | None) -> dict:
    video = video or {}
    music = video.get("music") or {}

    artist = str(music.get("artist") or "").strip()
    # `video.account` 就是作者昵称（采集时按账号名填的）——歌手未知时它作为「演唱者」兜底
    author = str(video.get("account") or video.get("author") or "").strip()
    fallback_artist = artist or author

    # ① 曲库元信息（只信看起来像歌名的）
    meta_title = str(music.get("title") or "").strip()
    if looks_like_song_name(meta_title):
        return {"title": meta_title, "artist": fallback_artist, "source": "meta"}

    # ② 视频文案里的《…》
    caption_title = _from_text(video.get("title") or "")
    if caption_title:
        return {"title": caption_title, "artist": fallback_artist, "source": "caption"}

    # ③ 评论里写出来的
    for c in comments or []:
        title = _from_text(c.get("content") or "")
        if title:
            return {"title": title, "artist": fallback_artist, "source": "comment"}

    # ④ 识别不出。**artist 仍然返回**——措辞时能说出是谁唱的，只是不编歌名。
    return {"title": "", "artist": fallback_artist, "source": "unknown"}


def make_identify_song():
    async def identify_song_node(state: dict) -> dict:
        song = identify_song(state.get("video"), state.get("comments"))
        log.info(
            "曲目识别：%s %s（来源=%s）",
            song.get("title") or "未识别出歌名",
            f"— {song['artist']}" if song.get("artist") else "",
            song.get("source"),
        )
        return {"song": song}

    return identify_song_node
