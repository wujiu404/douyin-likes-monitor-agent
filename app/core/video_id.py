"""抖音视频 ID 的形态判据。

为什么要单独一个模块：这个判据有两个使用方，而它们的**语义完全一致**——
「这个 ID 是不是能拼出有效抖音链接的真实 aweme_id」。

- `app/providers/browser.py` —— 采集评论前先校验（mock 的 `acc01_v01` 拼不出视频页）
- `app/notifiers/card.py` —— 决定卡片上给不给可点链接、要不要标注「模拟数据」

两处各写一份正则必然会跑偏（本项目在 `_cast`/`cast_config` 上已经踩过一次
同样的坑），所以收敛到一处。
"""
from __future__ import annotations

import re

# 抖音 aweme_id：纯数字，实际 19 位；放宽到 15~25 位以兼容历史与未来的长度变化。
# mock provider 生成的是 `acc01_v01` 这种可读 id —— 它**刻意**不长得像真实 ID，
# 所以这里能干净地把两者分开，不需要读额外的来源字段。
REAL_VIDEO_ID_RE = re.compile(r"[0-9]{15,25}")


def is_real_video_id(video_id: object) -> bool:
    """是不是能拼出有效抖音链接的真实 aweme_id。"""
    return bool(REAL_VIDEO_ID_RE.fullmatch(str(video_id or "").strip()))


def video_url(video_id: object) -> str:
    """真实 ID → 抖音视频页链接；否则返回空串（调用方据此决定不给链接）。

    不在这里替调用方做兜底拼接：拿 `acc01_v01` 拼出来的链接一定是 404，
    宁可返回空串让上层少显示一行。
    """
    return f"https://www.douyin.com/video/{video_id}" if is_real_video_id(video_id) else ""
