"""模拟数据源（P0 默认，零依赖、零额度）。

它被特意写成**确定性的**，原因有两个：

1. **点赞数必须随轮次单调递增。** 算增量做的是 `curr - prev`，
   模拟值要是会跳来跳去，增量就会出现负数，演示时很难看。
   做法：`点赞 = 基准值 + 增长率 × 第几轮 × 每轮等效分钟数`。
   每个视频的基准值与增长率完全由 `video_id` 决定（hashlib 派生），
   所以同一视频每轮拿到的都是同一条曲线。
   （`video_id` 形如 `acc01_v03`，由账号在列表中的次序决定；账号按 id 升序读，
   新增账号只会在末尾追加，不会让已有视频的曲线变样。）

   注意是**按轮次推进**而不是按墙钟时间——否则手动连点两次触发扫描，
   两次间隔只有几秒，增量会是 0，演示时得干等 3 分钟才能看到告警。
   为了跨进程重启后不回落，`set_round_baseline()` 会把已入库的轮次数作为起点。

2. **必须有人为制造出来的跨阈值视频。** 演示时告警链路不触发，整个亮点就没了。
   所以每个账号的前两个视频被刻意分配成「爆款」增长率——在默认配置
   （`threshold=20`）下每轮必然产生告警；切到演示档（`threshold=10`）时告警更多。

长尾视频的增长率压得很低，这样「告警的只有那几条」，看起来才像真实场景。
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any

ACCOUNT_VIDEO_COUNT = 6

# 每轮扫描的等效分钟数：增长率是「每分钟增量」，乘以它就是每轮增量
MINUTES_PER_SCAN = 3.0

# (档位名, 每分钟点赞增量) —— 前两档刻意拉高，保证跨阈值
#
# ⚠ 每档的「每轮增量」= 增长率 × MINUTES_PER_SCAN，**必须 ≥ 1**，
#   否则整数截断会让相邻两轮拿到同一个点赞数、增量算成 0。
#   最慢的一档是 0.4 × 3 = 1.2，留了安全余量。
#   第二档给到 7.5（每轮 ≈ +22）而不是贴着阈值——7.0 落到 19.95 时会被截成 19，
#   告警就时有时无，演示会显得很不稳。
GROWTH_TIERS: list[tuple[str, float]] = [
    ("爆款", 8.0),   # 每轮 ≈ +24，threshold=20 时稳定告警
    ("爆款", 7.5),   # 每轮 ≈ +22，稳过阈值
    ("中速", 2.0),   # 每轮 ≈ +6，不告警
    ("中速", 1.2),   # 每轮 ≈ +3.6
    ("长尾", 0.6),   # 每轮 ≈ +1.8
    ("长尾", 0.4),   # 每轮 ≈ +1.2
]

TITLES = [
    "夜晚的城市灯光，配上这首歌",
    "翻唱一首老歌，评论区告诉我歌名",
    "剪辑练习：这旋律太洗脑了",
    "深夜电台 · 第一首",
    "吉他指弹挑战，猜猜是什么歌",
    "旅行 vlog 配乐合集",
]

# 曲目元信息。真实采集时这来自视频自带的 music 字段；
# mock 里按 video_id 确定性挑一个，供 identify_song 的「元信息优先」路径使用。
SONGS = [
    ("夜空中最亮的星", "逃跑计划"),
    ("平凡之路", "朴树"),
    ("起风了", "买辣椒也用券"),
    ("漠河舞厅", "柳爽"),
    ("兰亭序", "周杰伦"),
    ("孤勇者", "陈奕迅"),
]

# (评论内容, 期望是否命中关键词) —— 第二项只用于自测，运行时按内容实际匹配
COMMENT_POOL: list[tuple[str, bool]] = [
    ("这是什么歌啊，好好听", True),
    ("BGM 是什么？求歌名", True),
    ("歌名求告知，太戳我了", True),
    ("这首歌叫什么名字呀", True),
    ("不好听，别推荐了", False),          # ← 否定前缀陷阱，不该命中「好听」
    ("好听吗？我觉得一般", True),
    ("路过支持一下", False),
    ("第 100 个赞是我的", False),
    ("前奏一响就认出来了", False),
    ("求完整版，歌曲名是什么", True),
    ("没听出来是什么歌", True),
    ("好听好听，循环一整天", True),
]


def _seed_of(text: str) -> int:
    """稳定种子。不能用内置 hash()——它对字符串是随机化的，跨进程不一致。"""
    return int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)


class MockProvider:
    name = "mock"
    synthetic = True  # 合成数据源：只在真实源**不可用**时兜底，见 base.py

    def __init__(self) -> None:
        self._round = 0

    def set_round_baseline(self, rounds: int) -> None:
        """把「已入库的轮次数」作为计数起点。

        不做这件事的话，进程重启后计数归零、点赞数跟着回落，
        增量变负数、告警也不触发——演示时碰上重启很难解释。
        """
        self._round = max(0, int(rounds))

    async def fetch_videos(self, accounts: list[dict], days: int) -> list[dict]:
        self._round += 1
        now = datetime.now(timezone.utc)

        videos: list[dict] = []
        for acc_no, acc in enumerate(accounts, start=1):
            for i in range(ACCOUNT_VIDEO_COUNT):
                videos.append(self._make_video(acc, acc_no, i, days, now, self._round))
        return videos

    def _make_video(
        self, acc: dict, acc_no: int, index: int, days: int, now: datetime, round_no: int
    ) -> dict[str, Any]:
        # video_id 直接可读（acc01_v03），不要拿 sec_uid 切片——那样会得到
        # "ount_a_v01" 这种看着像被截断的串，演示时很容易被当成 bug。
        video_id = f"acc{acc_no:02d}_v{index + 1:02d}"

        # 每个视频的参数完全由 video_id 决定 —— 同一视频每轮返回一致的基准与增长率
        vrng = random.Random(_seed_of(video_id))
        _, rate = GROWTH_TIERS[index % len(GROWTH_TIERS)]

        base = vrng.randint(800, 42000)
        jitter = 1.0 + vrng.uniform(-0.05, 0.05)
        # 按轮次推进，不按墙钟时间 —— 见模块 docstring 的第 1 条
        likes = int(base + rate * round_no * MINUTES_PER_SCAN * jitter)

        publish_hours_ago = vrng.uniform(2.0, max(3.0, days * 24 - 1))
        publish_time = (now - timedelta(hours=publish_hours_ago)).isoformat(timespec="seconds")
        song_title, song_artist = SONGS[vrng.randrange(len(SONGS))]

        return {
            "video_id": video_id,
            "account": acc.get("name", ""),
            "title": TITLES[index % len(TITLES)],
            "publish_time": publish_time,
            "likes": likes,
            "comments": int(likes * vrng.uniform(0.010, 0.030)),
            "shares": int(likes * vrng.uniform(0.004, 0.020)),
            # music 是可选字段，不入快照表，只供评论子图识别曲目
            "music": {"title": song_title, "artist": song_artist},
            "source": self.name,
        }

    async def fetch_comments(self, video_id: str, limit: int = 20) -> list[dict]:
        crng = random.Random(_seed_of(video_id + ":comments"))
        pool = list(COMMENT_POOL)
        crng.shuffle(pool)

        now = datetime.now(timezone.utc)
        out: list[dict] = []
        for j, (content, _expected) in enumerate(pool[:limit]):
            out.append(
                {
                    "comment_id": f"{video_id}_c{j + 1:02d}",
                    "content": content,
                    "comment_time": (
                        now - timedelta(minutes=crng.randint(5, 600))
                    ).isoformat(timespec="seconds"),
                }
            )
        return out
