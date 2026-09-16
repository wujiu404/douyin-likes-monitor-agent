"""告警渠道层。

四个后端，`NOTIFIER_BACKEND` 里用 `+` 可任意组合：

| 值 | 收件人 | 前提 |
|---|---|---|
| `local` | 本地看板 + 控制台日志 | 无（默认） |
| `feishu_im` | **你本人的飞书私聊** | 已有自建应用即可 |
| `feishu_card` | 某个飞书群 | 需要建群自定义机器人拿 webhook |
| `local+feishu_im` | 上面两个都要 | — |
"""
from __future__ import annotations

from app.config import Settings
from app.notifiers.base import Notifier

def _split(backend: str) -> list[str]:
    raw = (backend or "local").replace("+", ",")
    names = [p.strip().lower() for p in raw.split(",") if p.strip()]
    # 去重保序：`local+local` 不该发两遍
    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


def _make_one(settings: Settings, name: str) -> Notifier:
    if name == "feishu_im":
        from app.notifiers.feishu_im import FeishuIMNotifier

        return FeishuIMNotifier(settings)
    if name == "feishu_card":
        from app.notifiers.feishu_card import FeishuCardNotifier

        return FeishuCardNotifier(settings)

    from app.notifiers.local import LocalNotifier

    return LocalNotifier(settings)


def build_notifier(settings: Settings) -> Notifier:
    notifiers = [_make_one(settings, name) for name in _split(settings.notifier_backend)]
    if len(notifiers) == 1:
        return notifiers[0]

    from app.notifiers.composite import CompositeNotifier

    return CompositeNotifier(notifiers)


__all__ = ["Notifier", "build_notifier"]
