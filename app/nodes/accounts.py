"""节点 1：load_accounts —— 读配置 + 取启用账号。

副作用：无。

这是每轮扫描里**唯一读《配置表》的地方**。读完把 threshold / lookback_days /
comment_keywords 放进 state，后面的节点只读 state，不再回表。

这样一轮扫描内的配置是一个**一致的快照**——不会出现「算增量时阈值还是 20、
判告警时被人改成 10」这种撕裂。配置表是控制面，控制面在一轮内必须是常量。

`provider_chain` 是唯一的例外：它不走 state，而是直接 set 到 registry 上。
因为评论子图（fire-and-forget 的独立线程）也要用它，那里拿不到主图的 state；
而 registry 是单例，扫描又是串行的（`max_instances=1` + `ScanBusy`），
所以「每轮开头设一次」既安全又省掉一路参数传递。**写入点仍然只有这里一处**。

⚠ 解析数字一律走 `as_int`，**不要**写 `int(cfg.get("threshold", 20) or 20)`：
`or` 会把显式填的 0 当缺失，静默顶回默认值。踩过一次，见 app/core/coerce.py。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.core.coerce import as_int, as_str
from app.graph.state import MonitorState
from app.storage.base import StorageProvider

log = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))

DEFAULT_THRESHOLD = 20
DEFAULT_LOOKBACK_DAYS = 3


def make_load_accounts(storage: StorageProvider, registry=None):
    async def load_accounts(state: MonitorState) -> dict:
        cfg = await storage.get_config()
        accounts = await storage.list_accounts(only_enabled=True)

        # 配置只在这里解析一次，之后的节点（含告警判定、采集）都吃这份快照
        threshold = as_int(cfg.get("threshold"), DEFAULT_THRESHOLD)
        lookback_days = as_int(cfg.get("lookback_days"), DEFAULT_LOOKBACK_DAYS)
        keywords_raw = as_str(cfg.get("comment_keywords"))
        keywords = [k.strip() for k in keywords_raw.replace("，", ",").split(",") if k.strip()]

        chain_raw = as_str(cfg.get("provider_chain"))
        chain = registry.set_chain(chain_raw.split(",")) if registry is not None else []

        # 评论扫谁：默认 all（本轮采集到的全部视频）。
        # 只扫告警视频看着更省额度，但基础要求 4 的阈值是 300，真实小账号根本不告警，
        # 于是「选做的评论识别」永远跑不起来 —— 两个要求被隐式耦合了。
        scope = as_str(cfg.get("comment_scope"), "all").strip().lower() or "all"
        if scope not in ("all", "alerted"):
            log.warning("comment_scope=%r 不认识，按 all 处理", scope)
            scope = "all"

        snapshot = {
            "threshold": threshold,
            "lookback_days": lookback_days,
            "comment_keywords": keywords,
            "provider_chain": chain,
            "comment_scope": scope,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        # ⚠ 「一条启用账号都没有」不抛异常。它是配置状态，不是故障：
        # 图照常走到 END，轮次台账记 error_count=0 + 一条 note，对外返回 409。
        # 早先这里 raise RuntimeError，结果空账号被记成「降级链全部失败」，
        # 排查方向直接被带偏。见 app/core/errors.py 的说明。
        if not accounts:
            reason = "没有启用中的监控账号，请先在「监控账号」页添加"
            log.warning("本轮跳过：%s", reason)
            return {"accounts": [], **snapshot, "skip_reason": reason}

        log.info(
            "载入 %d 个账号，阈值=%d，回看=%d 天，关键词=%s，数据源链=%s，评论扫描范围=%s",
            len(accounts), threshold, lookback_days, keywords,
            " → ".join(chain) or "(默认)", scope,
        )
        return {"accounts": accounts, **snapshot, "skip_reason": ""}

    return load_accounts
