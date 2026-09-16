"""演示档 / 正式档的档位定义 —— **全项目唯一一份**。

为什么要有这个文件：档位切换要在两个地方用（看板按钮、`tools/demo_mode.py`
命令行），如果两边各写一份常量，迟早漂移。事实上已经漂移过一次：

- CLI 的演示档带 `provider_chain=mock`，看板按钮**没带**；
- 看板「恢复默认节奏」把 `threshold` 写成 20，正式档其实是 300。

带出的故障现象很隐蔽：从看板切「一键切演示档」后，节奏确实变成 3 分钟一轮、
阈值也确实降到 10，看起来一切正常；但 `provider_chain` 还是 `browser,mock`，
真实数据源排在第一且登录态正常 → 每轮都走真实采集，**永远轮不到 mock**。
真实账号 3 分钟内的点赞增量接近 0，于是「没有 mock 增量、也没有飞书告警」。
表面像 mock 坏了，实际是档位只切了一半。

⚠ `provider_chain` 是档位的一部分，**切档必须带上它**：
演示档要的是确定性数据（`mock`），否则「增量 → 超阈值 → 推送」这段
在真实账号上根本触发不了（真实账号不会 3 分钟涨 20 个赞）。
"""
from __future__ import annotations

# 正式档：完全对齐题目基础要求 1（12/18/22 点扫描）与要求 4（增量 > 300 提醒）
FORMAL: dict[str, str | int] = {
    "scan_mode": "cron",
    "scan_cron_hours": "12,18,22",
    "threshold": 300,
    "lookback_days": 3,
    # 真实源优先、mock 只在真实源「没接」时兜底（见 providers/registry.py 的三态语义）
    "provider_chain": "browser,mock",
    "comment_scope": "all",
}

# 演示档：完全对齐题目基础要求 5（3 分钟一轮、阈值降到 20）
DEMO: dict[str, str | int] = {
    "scan_mode": "interval",
    "scan_interval_minutes": 3,
    "threshold": 20,
    "lookback_days": 3,
    # 演示档必须是纯 mock：真实账号的点赞增量撑不起「超阈值 → 告警」这条动线
    "provider_chain": "mock",
    "comment_scope": "all",
}

PRESETS: dict[str, dict[str, str | int]] = {"formal": FORMAL, "demo": DEMO}

# 两个档位共涉及的全部键。恢复配置时要按这个集合来写，
# 否则漏掉某个键（比如 provider_chain）就会留下「半个档位」的状态。
PRESET_KEYS: tuple[str, ...] = tuple(sorted(set(FORMAL) | set(DEMO)))

# 别名 → 标准名，容忍一些口语写法
_ALIASES: dict[str, str] = {
    "demo": "demo", "on": "demo", "test": "demo", "演示": "demo", "测试": "demo",
    "formal": "formal", "off": "formal", "prod": "formal", "正式": "formal",
}


def normalize_preset_name(name: str) -> str:
    """口语写法 → 标准档位名；名字不认识就抛 `ValueError`（由调用方转成 400）。"""
    key = _ALIASES.get(str(name).strip().lower())
    if key is None:
        raise ValueError(f"未知档位 {name!r}，可用：demo（演示档）/ formal（正式档）")
    return key


def resolve_preset(name: str) -> dict[str, str | int]:
    """按名字取档位定义。"""
    return PRESETS[normalize_preset_name(name)]


def preset_of(cfg: dict) -> str:
    """反推当前配置属于哪个档位 —— 只看能不能对上，不做模糊判断。

    返回值：`demo` / `formal` / `mixed`（两边都不完全匹配，说明是手工改出来的状态）。
    """
    for name, preset in PRESETS.items():
        if all(str(cfg.get(k)) == str(v) for k, v in preset.items()):
            return name
    return "mixed"


__all__ = [
    "FORMAL", "DEMO", "PRESETS", "PRESET_KEYS",
    "normalize_preset_name", "resolve_preset", "preset_of",
]
