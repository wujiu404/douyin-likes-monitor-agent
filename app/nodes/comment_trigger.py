"""节点 7：run_comment_graph —— **发射后不管**。

⚠ 这个节点的语义必须写死为 fire-and-forget，理由见
   docs/01-技术方案-修订版.md §3.4.1。如果改成 `await` 子图完成，会同时破坏三件事：

   ① **调度** —— 主图 run 挂着不结束，`max_instances=1` + `coalesce=True`
      会让下一轮扫描永远排不上；
   ② **可观测性** —— 「一个 run 的轨迹可以 grep 出来」要求 run 能收敛；
   ③ **台账** —— 每轮要往《扫描轮次表》写汇总，主图不结束就写不成。

   所以：这里只 `ainvoke` 子图就**立刻返回**，主图随即走到 END。
   评论确认走 `POST /api/reviews/{thread_id}` 独立回口，结果直接写《评论命中表》，
   **不回流主图**。代价是主图轨迹里不含评论最终结果，需靠 thread_id 关联
   （readme / 风险清单里已注明）。

另外，扫哪些视频由《配置表》的 `comment_scope` 决定：

- `all`（默认）—— 本轮采集到的**全部**视频。选做的原始要求就是「扫描视频评论」，
  没有和「点赞增量超阈值」绑在一起；早期版本只扫告警视频，看着省额度，实际把
  两个要求隐式耦合了：基础要求 4 的阈值是 **300**，真实小账号永远不告警，
  于是选做的评论识别永远跑不起来。
- `alerted` —— 只扫有告警的视频（爆款才有人问歌名，省额度/降风控），
  代价是必须同时把阈值调低才会生效。

重复扫描不会重复打扰人：《评论命中表》按 `comment_id` 去重，评论提醒的幂等键
（`comment_review` 命名空间）也**不含 run_id**，同一条评论一生只提醒一次。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from app.config import Settings
from app.graph.state import MonitorState

log = logging.getLogger(__name__)

# 必须持有后台任务的强引用：asyncio 只保存弱引用，不持有的话任务
# 可能在跑到一半时被 GC 掉——这是很隐蔽的坑。
_BACKGROUND: set[asyncio.Task] = set()


def spawn_background(coro: Awaitable) -> asyncio.Task:
    task = asyncio.create_task(coro, name="comment-subgraph")  # type: ignore[arg-type]
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


def pending_background_count() -> int:
    return len(_BACKGROUND)


async def drain_background(timeout: float = 15.0) -> int:
    """等所有在飞的评论子图跑完（跑到 interrupt 挂起就算"跑完"）。

    两个用途：① 测试里收尾，避免任务被 GC 时抛出 "Task was destroyed but it is
    pending" 噪声；② 关停时给在飞任务一个收尾窗口。超时不抛异常——
    子图挂起本身就是**正常状态**，不是错误。
    """
    if not _BACKGROUND:
        return 0
    tasks = list(_BACKGROUND)
    done, still = await asyncio.wait(tasks, timeout=timeout)
    if still:
        log.warning(
            "仍有 %d 个评论子图任务未在 %.1fs 内收尾（通常是已挂起等待人工确认，属正常）",
            len(still), timeout,
        )
    return len(done)


def cancel_background() -> int:
    """取消所有在飞的评论子图任务。**只给测试用**。

    生产路径上不取消：子图跑到 interrupt 挂起本身就是正常状态，取消它等于丢掉
    待人工确认的记录。测试里用它是为了不把「永不返回」的假任务留给下一个用例。
    """
    n = 0
    for task in list(_BACKGROUND):
        task.cancel()
        n += 1
    return n


CommentRunner = Callable[[str, dict, str], Awaitable[None]]


def make_run_comment_graph(settings: Settings, runner: CommentRunner):
    async def run_comment_graph(state: MonitorState) -> dict:
        if not settings.enable_comment_graph:
            return {"comment_threads": []}

        scope = state.get("comment_scope") or "all"
        if scope == "alerted":
            targets = list(state.get("alerts") or [])
            if not targets:
                log.info("本轮无告警视频，跳过评论子图（comment_scope=alerted）")
                return {"comment_threads": []}
        else:
            # 全部视频。用采集结果里的完整 video（带 music 元信息）——
            # 告警条目只有点赞数，没有曲目信息，评论子图识别不了歌名。
            targets = list(state.get("videos") or [])
            if not targets:
                log.info("本轮没有采集到视频，跳过评论子图")
                return {"comment_threads": []}

        run_id = state["run_id"]
        keywords = state.get("comment_keywords", [])
        by_id = {v["video_id"]: v for v in state.get("videos", [])}

        threads: list[str] = []
        for item in targets:
            video_id = item["video_id"]
            video = dict(by_id.get(video_id, item))
            video["keywords"] = keywords
            thread_id = f"{run_id}:{video_id}"
            threads.append(thread_id)
            # ↓↓↓ 不 await。这就是 fire-and-forget 的全部实现。
            spawn_background(runner(thread_id, video, run_id))

        log.info("已发射 %d 个评论子图任务（不阻塞主图），thread_id=%s", len(threads), threads)
        return {"comment_threads": threads}

    return run_comment_graph
