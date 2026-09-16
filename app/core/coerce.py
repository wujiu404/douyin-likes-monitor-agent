"""配置值转换：把「没填」和「填了 0」区分开。

背景（2026-09-15 线上踩到的真 bug）。`load_accounts` 原本写的是：

    "threshold": int(cfg.get("threshold", 20) or 20)

`or` 把 **0 当成缺失**。用户在飞书《配置表》里把阈值填成 0（语义是「只要涨就告警」），
会被静默顶回 20；更阴的是日志那行打印的是 `cfg` 里的原值 0，
而真正进 state 的是 20 —— 两边对不上，看日志完全看不出哪里错了。

所以凡是从配置面读数字，一律走 `as_int`：
**只有 缺失 / 空白 / 无法解析 才回落默认值，显式 0 必须原样保留。**

配套改动：`app/storage/feishu.py::_cast` 对空单元格返回 `None`（"未填"），
不再返回 0 —— 否则「清空单元格」会被读成「阈值 = 0」，告警直接炸掉。
两处一起改，才能让「0」和「空」在链路上一直是两件事。
"""
from __future__ import annotations

from typing import Any


def as_int(value: Any, default: int) -> int:
    """转 int。缺失/空白/非法 → default；0 原样返回。"""
    if value is None:
        return default
    if isinstance(value, bool):          # bool 是 int 的子类，先挡掉，避免 True→1 的意外
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))          # 容忍 "10.0" 这种从飞书数字列读回来的写法
    except (TypeError, ValueError):
        return default


def as_str(value: Any, default: str = "") -> str:
    """转 str。None → default；其余 strip 后原样（同样不用 `or`，避免 0 被吃掉）。"""
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def cast_config(value: Any, type_: str, to_text: Any = None) -> Any:
    """把《配置表》里的「值」转成目标类型。sqlite 与飞书两个后端共用这一份实现。

    `to_text` 是各后端的取值器：飞书的文本列可能返回富文本片段数组，
    要传 `_as_text` 归一化；SQLite 直接 `str` 即可。

    ⚠ 空单元格返回 **None（"没填"）而不是 0**。
    早年这里 `int("")` 失败后返回 0，于是「用户把阈值那格清空」会被读成
    「阈值 = 0」→ 每条增量都告警。空和 0 是两件事，必须分开；
    下游 `as_int` 会把 None 回落到默认值。
    """
    text_of = to_text or (lambda v: str(v))
    if value is None:
        return None
    if type_ == "int":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = text_of(value).strip()
        if not text:
            return None
        try:
            return int(float(text))     # 容忍 "10.0" 这种从数字列读回来的写法
        except (TypeError, ValueError):
            return None
    if type_ == "bool":
        text = text_of(value).strip().lower()
        if not text:
            return None
        return text in ("1", "true", "yes", "on", "是")
    return text_of(value)


__all__ = ["as_int", "as_str", "cast_config"]
