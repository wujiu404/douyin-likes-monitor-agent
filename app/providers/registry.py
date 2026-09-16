"""provider 注册表与降级链执行。

降级链语义（**三种「拿不到数据」必须分开对待**，都是踩过的坑）：

| 情况 | 含义 | 处理 |
|---|---|---|
| 真实源**没接**（`ProviderNotConfigured`） | 本来就没有这一档（没买第三方服务 / 没配浏览器目录） | 安静降级；链尾是 mock 就让它兜底 |
| 真实源**抛 ProviderError / 异常** | 配置本来是好的，现在坏了（登录态过期、被风控、接口报错） | 记下原因，**不再让 mock 顶替**，本轮如实失败 |
| 真实源**成功执行但返回空** | 明确知道现在没有数据（视频真没评论 / 账号窗口内没发新作） | **停止让 mock 兜底**，把空结果如实返回 |

第二条（「真实源坏了不许 mock 顶替」）与第一条的区别只有一句话：
**mock 是「没有真实源可用」的兜底，不是「真实源坏了」的兜底。**
`ProviderNotConfigured` 走前者（没接就是没接，mock 顶上让演示不空），
其它失败走后者（宁可本轮失败，也不往真实库里写一批看不出假的行）。
判据落在 trace 上：`mock:skipped:real-source-unavailable`。

第三条是后加的。没有它的时候，一个 8 天没发作品的账号会被 mock 补上 12 条假视频、
一条 0 评论的视频会被 mock 补上 12 条假评论，然后照常入库、照常推飞书——
看板上分不出真假（详见 `app/core/video_id.py` 与 base.py 的 `synthetic`）。

第四条（2026-09-16 补）：**降级和账号级失败必须留下痕迹**。以前它们只写日志，
《扫描轮次表》上干净得很，用户看到的是「扫描成功，但快照/增量/告警一行都没动」，
只能来问为什么。现在 `collect_warnings` / `collect_failures` 把它们交给
`collect_videos` 节点写进轮次备注；`ProviderNotConfigured` 例外——
「本来就没接」是预期内的降级，不写台账。

每次尝试都记进 `trace`，最终写进《视频快照表》的「数据来源」字段——
演示时能直接回答「这一轮的数据是模拟的还是真实的」。
"""
from __future__ import annotations

import logging

from app.config import Settings
from app.providers.base import ProviderError, ProviderNotConfigured, VideoProvider
from app.providers.browser import BrowserProvider
from app.providers.mock import MockProvider
from app.providers.thirdparty import ThirdpartyProvider

log = logging.getLogger(__name__)


def _is_synthetic(provider: object) -> bool:
    """该 provider 是不是合成（假）数据源。目前只有 mock。"""
    return bool(getattr(provider, "synthetic", False))


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.chain: list[str] = settings.provider_chain_list or ["mock"]
        self._providers: dict[str, VideoProvider] = {
            "mock": MockProvider(),
            "thirdparty": ThirdpartyProvider(settings),
            "browser": BrowserProvider(settings),
        }
        # 上一轮采集里「需要让人看见」的提示 + 其中算真异常的条数。
        # 由 `collect_videos` 节点读走，写进《扫描轮次表》的备注与 error_count。
        # 为什么需要它：降级和「某个账号没抓到」以前只进日志，轮次台账上是一片
        # 岁月静好，用户看到的是「扫描成功但三张表都没动」，然后来问为什么
        # （2026-09-16 用户就是这么问的）。现在原因直接落在台账上。
        # ⚠ 实例属性而非返回值：采集是串行的（max_instances=1 + ScanBusy），
        #   和 `chain` 的处理方式一致。
        self.collect_warnings: list[str] = []
        self.collect_failures: int = 0

    def set_chain(self, names: list[str]) -> list[str]:
        """覆盖本轮生效的降级链，返回**实际生效**的链。

        降级链是**控制面**参数（和阈值、调度时点一样从《配置表》读），不是基础设施参数：
        「正式跑真实数据 / 演示跑确定性 mock」是操作者随时会切的开关，
        要求改 .env 再重启就太笨重了。留空则退回 .env 里的 `PROVIDER_CHAIN`。

        已知 provider 之外的条目会被丢掉并记一条警告——写错名字要看得见，
        不能静默变成「这条链是空的」然后报「降级链全部失败」。
        """
        clean = [n.strip().lower() for n in names if str(n).strip()]
        known = [n for n in clean if n in self._providers]
        unknown = [n for n in clean if n not in self._providers]
        if unknown:
            log.warning("《配置表》的 provider_chain 里有未知数据源 %s，已忽略", unknown)
        self.chain = known or (self.settings.provider_chain_list or ["mock"])
        return self.chain

    def get(self, name: str) -> VideoProvider | None:
        return self._providers.get(name)

    def _note_failure(self, text: str) -> None:
        """记一条「必须让人看见」的采集异常（进轮次台账 + error_count）。"""
        self.collect_warnings.append(text)
        self.collect_failures += 1

    def _collect_account_notes(self, provider: object) -> None:
        """把 provider 的**账号级**结论收上来（目前只有 browser 会给）。

        这些提示不进 error_count —— 「账号 3 天内没发新作品」是数据状态不是故障，
        但必须写进台账：用户新增账号后扫描却什么都没看到时，
        台账里那一行「账号『X』回看 3 天内没有新作品」就是答案。
        """
        notes = list(getattr(provider, "account_notes", None) or [])
        self.collect_warnings.extend(str(n) for n in notes)
        self.collect_failures += int(getattr(provider, "account_failures", 0) or 0)

    def set_round_baseline(self, rounds: int) -> None:
        """把「已入库轮次数」告诉需要它的 provider（目前只有 mock）。

        目的见 `MockProvider.set_round_baseline`：避免进程重启后模拟点赞数回落，
        否则重启会凭空产生一批负增量。
        """
        for provider in self._providers.values():
            hook = getattr(provider, "set_round_baseline", None)
            if callable(hook):
                hook(rounds)

    async def fetch_videos(self, accounts: list[dict], days: int) -> tuple[list[dict], list[str], str]:
        """返回 (videos, trace, 实际生效的 provider 名)。

        - 有数据：`(videos, trace, name)`
        - **真实源成功但确实没数据**：`([], trace, "")` —— 调用方按「本轮无新作品」跳过，
          而不是当作故障（更不能用 mock 顶上）
        - 全都不可用：抛 `ProviderError`

        副作用：`collect_warnings` / `collect_failures` 记下本轮所有「需要让人看见」
        的采集异常（降级原因 + 账号级结论），供 `collect_videos` 写进轮次台账。
        """
        trace: list[str] = []
        real_empty = False  # 真实源已经明确回答过「现在没有数据」
        hard_failed: list[str] = []  # 配置本来是好的、现在坏了的真实源
        self.collect_warnings = []
        self.collect_failures = 0
        for name in self.chain:
            provider = self._providers.get(name)
            if provider is None:
                trace.append(f"{name}:unknown")
                continue
            if real_empty and _is_synthetic(provider):
                # 真实源说了没数据，还想用 mock 补一批假的？那是造假，不是降级。
                trace.append(f"{name}:skipped:real-source-empty")
                continue
            if hard_failed and _is_synthetic(provider):
                # 真实源**坏了**才轮到合成源 —— 这不是降级，是往真实库里混假数据。
                # 「没接真实源」（ProviderNotConfigured）和「真实源坏了」必须分开：
                # 前者 mock 兜底是对的（没接就是没接），后者一兜底，
                # 用户就会在《视频快照表》里看到一批 source=mock 的行，
                # 还以为本轮采集成功了。宁可本轮失败并把原因写上台账。
                trace.append(f"{name}:skipped:real-source-unavailable")
                continue
            try:
                videos = await provider.fetch_videos(accounts, days)
            except ProviderNotConfigured as exc:
                # 这一档本来就没接：安静降级，不写台账（否则每轮都挂一句废话）
                log.info("provider %s 未配置，降级：%s", name, exc)
                trace.append(f"{name}:failed")
                continue
            except ProviderError as exc:
                log.warning("provider %s 不可用，降级：%s", name, exc)
                trace.append(f"{name}:failed")
                self._note_failure(f"{name} 不可用：{exc}")
                if not _is_synthetic(provider):
                    hard_failed.append(f"{name}：{exc}")
                continue
            except Exception:  # noqa: BLE001 - 任何异常都降级，不能让整轮扫描挂掉
                log.exception("provider %s 抛异常，降级", name)
                trace.append(f"{name}:error")
                self._note_failure(f"{name} 抛异常（详情见日志）")
                if not _is_synthetic(provider):
                    hard_failed.append(f"{name}：抛异常")
                continue

            self._collect_account_notes(provider)
            if videos:
                trace.append(f"{name}:ok")
                return videos, trace, name

            # 成功执行但空结果：记成 empty 而不是 failed —— 这两者的后续处理完全不同
            trace.append(f"{name}:empty")
            if not _is_synthetic(provider):
                real_empty = True

        if real_empty:
            log.info("真实数据源均已成功执行但没有数据，按空结果返回（不降级到模拟数据）：%s", trace)
            return [], trace, ""
        if hard_failed:
            # 有真实源被配置过但坏了，且没有别的真实源顶上 —— 本轮如实失败。
            # 错误信息会被 collect_videos 原样写进《扫描轮次表》的备注，
            # 所以把链路也带上：看台账的人不用去翻日志就能知道 mock 为什么没兜底。
            raise ProviderError(
                "真实数据源不可用，且拒绝用模拟数据顶替（本轮不写任何数据）："
                + "；".join(hard_failed)
                + f"（链路：{' → '.join(trace)}）"
            )
        raise ProviderError(f"降级链全部失败：{trace}")

    async def fetch_comments(self, video_id: str, limit: int = 20) -> tuple[list[dict], str]:
        """评论采集同样走降级链，语义与 `fetch_videos` 一致。

        ⚠ 关键区别：真实源抓到 0 条评论**是合法的结果**（小账号的视频常常没人评论），
        必须原样返回空列表，绝不能让 mock 补一批假评论——那会让「评论识别」这个
        选做功能在演示时看起来在工作，实际全是编的。
        """
        real_empty = False
        for name in self.chain:
            provider = self._providers.get(name)
            if provider is None:
                continue
            if real_empty and _is_synthetic(provider):
                log.info("视频 %s 真实源没有评论，跳过合成数据源 %s", video_id, name)
                continue
            try:
                comments = await provider.fetch_comments(video_id, limit)
            except Exception as exc:  # noqa: BLE001 - 单源失败就试下一个
                log.debug("provider %s 取评论失败（%s）：%s", name, video_id, exc)
                continue
            if comments:
                return comments, name
            if not _is_synthetic(provider):
                real_empty = True
        return [], ""
