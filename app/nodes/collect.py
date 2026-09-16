"""节点 2：collect_videos —— 采集作品指标。

副作用：无（只读外部数据）。

**provider 降级链收在这个节点内部**，不做成三条条件边——降级是采集的实现细节，
不是业务流程的分支。图只暴露一个「采集成功 / 失败」的结果。

并发粒度由 provider 自己决定：批量接口（thirdparty / mock）一次拿全部；
需要逐账号抓取的（browser）在内部用 `core.ratelimit.run_per_account`，
遵守「账号间可并发、单账号内必须串行」这条风控纪律。

采集全部失败时**不抛异常**——把错误记进 state 继续往下走，
这样《扫描轮次表》里能留下一条「本轮采集失败」的记录，比整轮静默消失好。
"""
from __future__ import annotations

import logging

from app.graph.state import MonitorState
from app.providers.base import ProviderError
from app.providers.registry import ProviderRegistry

log = logging.getLogger(__name__)


def make_collect_videos(registry: ProviderRegistry):
    async def collect_videos(state: MonitorState) -> dict:
        accounts = state.get("accounts", [])
        days = int(state.get("lookback_days", 3))

        # 「一个启用的账号都没有」是配置状态，不是采集错误。
        # 不放行的话，空列表会让降级链一路试到 mock、最后报「降级链全部失败」——
        # 这条日志会把人误导到 provider 上去查，其实该去账号表里加账号。
        if not accounts:
            log.warning("本轮没有启用中的监控账号，跳过采集（去「账号」页添加）")
            return {
                "videos": [],
                "provider_trace": ["skipped:no_accounts"],
                "source": "",
                "note": "本轮无启用账号，未采集",
            }

        try:
            videos, trace, source = await registry.fetch_videos(accounts, days)
        except ProviderError as exc:
            log.error("采集失败：%s", exc)
            return {
                "videos": [],
                "provider_trace": [str(exc)],
                "source": "",
                "errors": list(state.get("errors", [])) + [f"collect_videos: {exc}"],
                "error_count": int(state.get("error_count", 0)) + 1,
                "note": f"采集失败：{exc}",
            }

        # 采集异常与账号级结论（降级原因、哪个账号没抓到、哪个账号窗口内没新作品）。
        # 以前这些只进日志，轮次台账一片干净，用户看板里只会看到
        # 「扫描成功但快照/增量/告警一行都没动」——2026-09-16 的提问就是这么来的。
        # 现在原样落到 state.note 上，台账里能直接读出原因。
        warnings = list(getattr(registry, "collect_warnings", []) or [])
        failures = int(getattr(registry, "collect_failures", 0) or 0)
        extra: dict = {}
        if warnings:
            extra = {
                "errors": list(state.get("errors", [])) + warnings,
                "error_count": int(state.get("error_count", 0)) + failures,
                "note": "；".join(warnings),
            }

        # 真实源成功执行但窗口内没有任何作品 —— 这是**数据状态**，不是故障。
        # 必须显式跳过：以前这里返回空会让降级链接着用 mock 顶上，
        # 于是「账号这几天没发作品」被伪装成「采集到了 12 条视频」，看板上分不出真假。
        if not videos:
            reason = (
                f"回看 {days} 天内没有新作品（数据源：{' → '.join(trace) or '无'}），本轮跳过"
            )
            if warnings:
                reason = f"{reason}｜{'；'.join(warnings)}"
            log.warning("%s", reason)
            return {
                "videos": [],
                "provider_trace": trace,
                "source": "",
                "skip_reason": reason,
                **extra,
            }

        log.info("采集到 %d 条视频，来源=%s，链路=%s", len(videos), source, " → ".join(trace))
        return {"videos": videos, "provider_trace": trace, "source": source, **extra}

    return collect_videos
