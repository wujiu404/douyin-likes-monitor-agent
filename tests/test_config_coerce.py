"""配置值转换测试 —— 钉住「显式 0」和「没填」是两件事。

回归背景：`load_accounts` 原本写 `int(cfg.get("threshold", 20) or 20)`，
`or` 把 0 当缺失。用户在飞书《配置表》填 0（语义「只要涨就告警」）被静默顶回 20，
而且日志打印的是 cfg 原值 0、进 state 的是 20，看日志根本发现不了。

同时钉住空单元格 → None（"没填"）而不是 0，否则「清空阈值那格」
会被读成「阈值 = 0」。两个后端（sqlite / 飞书）共用 cast_config，一起验。
"""
from __future__ import annotations

import pytest

from app.core.coerce import as_int, as_str, cast_config
from app.nodes.accounts import DEFAULT_LOOKBACK_DAYS, DEFAULT_THRESHOLD, make_load_accounts
from app.storage.feishu import _as_text, _cast as feishu_cast
from app.storage.sqlite import _cast as sqlite_cast


# ---------------------------------------------------------------- as_int

@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),          # ★ 核心：显式 0 必须保留
        ("0", 0),
        ("0.0", 0),
        (-1, -1),
        (5, 5),
        ("12", 12),
        (None, 20),      # 没填
        ("", 20),        # 空白
        ("   ", 20),
        ("abc", 20),     # 非法
    ],
)
def test_as_int_keeps_zero(value, expected) -> None:
    assert as_int(value, DEFAULT_THRESHOLD) == expected


def test_as_str_keeps_zero() -> None:
    assert as_str("0", "x") == "0"
    assert as_str(0, "x") == "0"
    assert as_str(None, "x") == "x"
    assert as_str("   ", "x") == "x"


# ---------------------------------------------------------------- cast_config（两个后端共用）

@pytest.mark.parametrize("cast", [sqlite_cast, feishu_cast])
def test_cast_blank_is_none_not_zero(cast) -> None:
    assert cast("", "int") is None
    assert cast("   ", "int") is None
    assert cast(None, "int") is None
    assert cast("abc", "int") is None


@pytest.mark.parametrize("cast", [sqlite_cast, feishu_cast])
def test_cast_explicit_zero_survives(cast) -> None:
    assert cast("0", "int") == 0
    assert cast(0, "int") == 0


def test_feishu_cast_handles_rich_text_segments() -> None:
    """飞书文本列可能回富文本片段数组，_as_text 要先归一化再解析。"""
    assert feishu_cast([{"text": "15"}], "int") == 15
    assert feishu_cast([{"text": ""}], "int") is None
    assert _as_text([{"text": "ab"}, {"text": "c"}]) == "abc"


# ---------------------------------------------------------------- 节点层：阈值真的传下去了

class _CfgStorage:
    """只实现 load_accounts 需要的两个方法。"""

    def __init__(self, cfg: dict, accounts: list[dict]) -> None:
        self._cfg = cfg
        self._accounts = accounts

    async def get_config(self) -> dict:
        return dict(self._cfg)

    async def list_accounts(self, only_enabled: bool = True) -> list[dict]:
        return [a for a in self._accounts if a.get("enabled") or not only_enabled]


ACC = [{"name": "A", "sec_uid": "s1", "enabled": 1}]


async def test_node_passes_zero_threshold_through() -> None:
    node = make_load_accounts(_CfgStorage({"threshold": "0"}, ACC))
    out = await node({})
    assert out["threshold"] == 0, "阈值填 0 必须原样进 state，不能被 or 顶成默认值"


async def test_node_falls_back_when_threshold_missing() -> None:
    node = make_load_accounts(_CfgStorage({}, ACC))
    out = await node({})
    assert out["threshold"] == DEFAULT_THRESHOLD


async def test_node_falls_back_when_threshold_blank() -> None:
    """飞书里把阈值那格清空 → cast 给 None → 回落默认值（不是 0）。"""
    node = make_load_accounts(_CfgStorage({"threshold": feishu_cast("", "int")}, ACC))
    out = await node({})
    assert out["threshold"] == DEFAULT_THRESHOLD


async def test_node_keeps_zero_lookback_days() -> None:
    node = make_load_accounts(_CfgStorage({"lookback_days": 0}, ACC))
    out = await node({})
    assert out["lookback_days"] == 0
    assert out["lookback_days"] != DEFAULT_LOOKBACK_DAYS
