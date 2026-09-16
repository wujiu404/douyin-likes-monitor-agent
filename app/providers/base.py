"""数据源 provider 协议。

降级链（thirdparty → browser → mock）的逻辑**收在 `collect_videos` 节点内部**，
不做成三条条件边——降级是采集的实现细节，不是业务流程的分支。
图只暴露一个「采集成功 / 失败」的结果。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """数据源不可用。降级链遇到它就换下一个。"""


class ProviderNotConfigured(ProviderError):
    """这一档数据源**本来就没接**（缺 .env 配置 / 缺依赖 / 占位实现）。

    与普通 `ProviderError` 的区别在于「要不要惊动人」：
    - `ProviderNotConfigured`：预期内的降级（没接第三方服务、没配浏览器目录），
      安静走到下一档，**不写进《扫描轮次表》的备注**——否则每轮都挂一句
      「thirdparty 未配置」和一直响的警报器没区别。
    - `ProviderError` 的其它情况（登录态过期、被风控、接口报错）：配置本来是好的，
      现在坏了 → 必须留在台账上，否则用户看到的是「扫描成功但什么都没动」。
    """


@runtime_checkable
class VideoProvider(Protocol):
    name: str

    # 合成数据源标记：只有 mock 为 True。
    #
    # 降级链用它区分两种「没有数据」——这是本项目最容易出错的地方：
    #   ① 真实源**抛异常** = 我不知道有没有数据 → 允许继续降级（含降级到 mock）
    #   ② 真实源**成功但返回空** = 我明确知道现在没有数据 → **禁止 mock 兜底**
    #
    # 没有这条区分时，真实视频（评论数 0）会被 mock 补上 12 条假评论，
    # 真实账号（3 天内没发新作）会被 mock 补上 12 条假视频，而且看不出是假的。
    synthetic: bool

    async def fetch_videos(self, accounts: list[dict], days: int) -> list[dict]:
        """返回视频指标列表，字段见 `VideoMetric` 约定：

        video_id / account / title / publish_time / likes / comments / shares / source
        """
        ...

    async def fetch_comments(self, video_id: str, limit: int = 20) -> list[dict]:
        """返回评论列表：comment_id / content / comment_time。"""
        ...


# 采集结果的字段约定，写在这里是为了让 mock / thirdparty / browser 三个实现对齐
VIDEO_FIELDS = (
    "video_id",
    "account",
    "title",
    "publish_time",
    "likes",
    "comments",
    "shares",
    "source",
)
