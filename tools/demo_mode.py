"""演示档 / 正式档一键切换（走《配置表》控制面，**不需要重启**）。

对应面试题的两条要求：
- 基础要求 1：每天 12:00 / 18:00 / 22:00 扫描一次  →  正式档 `scan_mode=cron`
- 基础要求 4：点赞增量超过 300 时发送提醒          →  正式档 `threshold=300`
- 基础要求 5：演示时扫描间隔改 3 分钟、阈值改 10 或 20  →  演示档 `interval/3` + `threshold=20`

一次要改 6 格，手工在《配置表》里点很容易漏一格（比如只改了间隔没改阈值，
演示时全场安静）。所以做成一条命令 —— 档位定义在 `app/core/presets.py`，
看板上的按钮调的是同一个接口，两边不会各写一份再漂移。

⚠ **必须走 HTTP 接口改，不能直接写库。**
`PUT /api/config/preset` 除了写库，还会让运行中的进程**立刻重载调度器**；
直接写 SQLite 只有数据变了，进程里的 APScheduler 还挂着旧 job
（cron 模式下要等到 12 点那次触发才重读，演示档的 3 分钟间隔等于没生效）。
服务没在跑时才退回直接写库——那种情况下本来也没有调度器需要重载。

用法：
    .venv/Scripts/python.exe tools/demo_mode.py            # 看当前是什么档
    .venv/Scripts/python.exe tools/demo_mode.py --on       # 切演示档
    .venv/Scripts/python.exe tools/demo_mode.py --off      # 切回正式档

演示档把 `provider_chain` 也切成 `mock`：真实账号 3 分钟内点赞增量基本是 0，
阈值再低也告警不了；mock 每档每轮增量 ≥1，能让「增量 → 超阈值 → 推送」这段
确定性跑起来（题目允许用模拟数据）。真实采集方案用 `--off` 恢复的 browser 档。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.presets import (  # noqa: E402
    FORMAL,
    PRESET_KEYS,
    resolve_preset,
    normalize_preset_name,
    preset_of,
)

# 兼容旧引用：这两个名字以前定义在本文件里
DEMO = resolve_preset("demo")


# ---------------------------------------------------------------- 在线：走接口
async def _read_live(base: str) -> tuple[dict, dict] | None:
    """服务在跑吗？在跑就顺带拿回配置与调度状态。"""
    try:
        async with httpx.AsyncClient(base_url=base, timeout=8) as c:
            cfg = (await c.get("/api/config")).json()["resolved"]
            sch = (await c.get("/api/scan/status")).json()["scheduler"]
        return cfg, sch
    except Exception:  # noqa: BLE001 - 服务没起是正常情况
        return None


async def _write_live(base: str, name: str) -> tuple[dict, dict, dict] | None:
    """走接口把整个档位一次切完，接口里会调 scheduler.reload()，调度立刻跟着变。"""
    try:
        # 一次写 6 格 + 飞书双写，给足超时
        async with httpx.AsyncClient(base_url=base, timeout=90) as c:
            resp = await c.put("/api/config/preset", json={"name": name})
            resp.raise_for_status()
            changed = resp.json().get("changed", {})
            cfg = (await c.get("/api/config")).json()["resolved"]
            sch = (await c.get("/api/scan/status")).json()["scheduler"]
        return cfg, sch, changed
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] 走接口切档失败（{exc}），退回直接写库。")
        return None


# ---------------------------------------------------------------- 离线：直接写库
async def _build_mirror():
    """只建句柄不跑全量回填 —— 回填要为每行发 PUT，白烧飞书额度。"""
    from app.storage.feishu import FeishuStorage
    from app.storage.mirror import MirrorStorage
    from app.storage.sqlite import SqliteStorage

    settings = get_settings()
    primary = SqliteStorage(settings.db_file)
    await primary.init()

    secondary = None
    try:
        secondary = FeishuStorage(settings, seed_defaults=False)
        await secondary.init()
    except Exception as exc:  # noqa: BLE001 - 飞书不可用时只改本地
        print(f"[警告] 飞书镜像不可用（{exc}），这次只改本地。两边可能不一致，"
              f"下次启动会按三路合并规则收敛。")
        secondary = None

    return MirrorStorage(primary, secondary), settings


async def _write_offline(changes: dict) -> dict:
    mirror, _settings = await _build_mirror()
    try:
        for k, v in changes.items():
            await mirror.set_config(k, v)
        return await mirror.get_config()
    finally:
        await mirror.close()


# ---------------------------------------------------------------- 展示
def _render(cfg: dict) -> list[str]:
    name = preset_of(cfg)
    if name == "demo":
        mode = "演示档"
    elif name == "formal":
        mode = "正式档"
    else:
        # 只看单格很容易以为切好了（比如节奏和阈值都变了、数据源链没变），
        # 所以这里显式报「混合档位」，把半个档位的状态点出来。
        mode = "⚠ 混合档位（既不是完整演示档，也不是完整正式档）"

    if cfg.get("scan_mode") == "interval":
        cadence = f"每 {cfg.get('scan_interval_minutes')} 分钟一轮"
    else:
        cadence = f"每天 {cfg.get('scan_cron_hours')} 点各一轮"

    lines = [
        f"当前档位：{mode}",
        f"  扫描节奏   {cadence}",
        f"  告警阈值   增量 > {cfg.get('threshold')}",
        f"  回看窗口   最近 {cfg.get('lookback_days')} 天",
        f"  数据源链   {cfg.get('provider_chain')}",
        f"  评论范围   {cfg.get('comment_scope')}",
    ]
    if name == "mixed":
        demo_diffs = [k for k in PRESET_KEYS if str(cfg.get(k)) != str(resolve_preset("demo")[k])]
        formal_diffs = [k for k in PRESET_KEYS if str(cfg.get(k)) != str(FORMAL[k])]
        lines.append(
            f"  偏离演示档 {len(demo_diffs)} 格 / 偏离正式档 {len(formal_diffs)} 格 → "
            f"建议 --on 或 --off 整体切一次"
        )
    return lines


async def main(target: str | None) -> None:
    settings = get_settings()
    base = f"http://{settings.app_host}:{settings.app_port}"

    live = await _read_live(base)
    if live:
        before, sch = live
    else:
        before = await _read_offline()
        sch = None

    for line in _render(before):
        print(line)
    if sch:
        print(f"  调度器     {sch.get('description')}，下次 {sch.get('next_run_at')}")

    if target is None:
        print("\n（--on 切演示档 / --off 切回正式档）")
        return

    name = normalize_preset_name(target)
    changes = resolve_preset(name)
    print(f"\n→ 切到{'演示' if name == 'demo' else '正式'}档，改这几格：")

    if live:
        written = await _write_live(base, name)
        if written is not None:
            after, sch_after, changed = written
            if changed:
                for k, move in changed.items():
                    print(f"   {k:<22} {move['from']} → {move['to']}")
            else:
                print("   （每一格都已经是目标值，无需改动）")
            print(f"\n调度器已重载：{sch_after.get('description')}，下次 {sch_after.get('next_run_at')}")
            print("**服务进程一直没重启**——这就是「配置表是控制面」的意思。")
            for line in _render(after):
                print(line)
            return
        print("   （接口不可用，改为直接写库）")

    after = await _write_offline(changes)
    for k in changes:
        print(f"   {k:<22} {before.get(k)} → {after.get(k)}")
    print("\n已写入《配置表》。⚠ 服务没在跑（或接口不可用），所以没有活跃调度器需要重载；"
          "\n  如果服务其实在跑，请重启它或从看板再改一次配置，否则新节奏不会生效。")
    for line in _render(after):
        print(line)


async def _read_offline() -> dict:
    mirror, _settings = await _build_mirror()
    try:
        return await mirror.get_config()
    finally:
        await mirror.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="演示档 / 正式档一键切换")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--on", dest="target", action="store_const", const="demo", help="切演示档")
    g.add_argument("--off", dest="target", action="store_const", const="formal", help="切回正式档")
    p.set_defaults(target=None)
    asyncio.run(main(p.parse_args().target))
