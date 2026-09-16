"""档位定义（presets）与切档接口的回归测试。

这一组存在的理由：档位定义原本有**两份**（看板按钮一份、`tools/demo_mode.py` 一份），
已经漂移过，并造成一次很隐蔽的故障：

    从看板点「一键切演示档」，节奏确实变成 3 分钟一轮、阈值也确实降了，
    看起来一切正常；但 `provider_chain` 没被切，还是 `browser,mock` ——
    真实数据源排第一且登录态正常，于是每轮都走真实采集，永远轮不到 mock。
    真实账号 3 分钟内的点赞增量接近 0 → 「没有 mock 增量、也没有飞书告警」。

表面像 mock 坏了，实际是档位只切了一半。所以这里既钉后端的档位定义，
也钉「前端不许自己拼档位键值对」—— 前端只准调 `/api/config/preset`。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.presets import (
    DEMO,
    FORMAL,
    PRESET_KEYS,
    normalize_preset_name,
    preset_of,
    resolve_preset,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """真装配一个 app（不起端口、不碰飞书），但**不预改任何配置**。

    test_api.py 里那个同名夹具会把 threshold 预置成 20（为了稳定触发告警），
    对档位测试来说那反而是干扰 —— 档位判断要求配置停在明确的一档上，
    所以这里单开一个干净起点，装配方式与它保持一致。
    """
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("NOTIFIER_BACKEND", "local")
    monkeypatch.setenv("PROVIDER_CHAIN", "browser,mock")
    # 同 conftest.settings：别让真实 .env 的浏览器配置漏进测试
    monkeypatch.setenv("BROWSER_USER_DATA_DIR", "")
    monkeypatch.setenv("LLM_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    transport = ASGITransport(app=main.app)
    async with main.app.router.lifespan_context(main.app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    get_settings.cache_clear()


# ---------------------------------------------------------------- 定义本身
# 这两档「改哪几格」可以不同（cron 档不需要 interval_minutes，反之亦然），
# 但共有的关键格必须两边都定义 —— 少一个就意味着切过去时那格留着上一档的值。
SHARED_KEYS = ("scan_mode", "threshold", "lookback_days", "provider_chain", "comment_scope")


@pytest.mark.parametrize("key", SHARED_KEYS)
def test_both_presets_define_the_shared_keys(key: str) -> None:
    assert key in FORMAL and key in DEMO, f"{key} 必须两档都定义"


@pytest.mark.parametrize("key", ["scan_mode", "threshold", "provider_chain"])
def test_switching_presets_actually_changes_something(key: str) -> None:
    """这三格是档位的区分点，两档取同值就等于「切了跟没切一样」。

    （`lookback_days` / `comment_scope` 两档故意相同，不在此列。）
    """
    assert str(FORMAL[key]) != str(DEMO[key])


def test_switching_lands_on_a_recognizable_preset() -> None:
    """无论从哪一档切到哪一档，结果都必须能被 `preset_of` 认出来（不能变 mixed）。

    这条直接钉住「档位只切一半」的故障：如果某档漏定义了 `provider_chain`，
    切过去之后这一格会留着上一档的值，落在 mixed 上。
    """
    from_demo = dict(DEMO)
    from_demo.update(FORMAL)          # 演示档 → 正式档：只覆盖正式档定义的格
    assert preset_of(from_demo) == "formal"

    from_formal = dict(FORMAL)
    from_formal.update(DEMO)          # 正式档 → 演示档
    assert preset_of(from_formal) == "demo"


def test_demo_is_pure_mock_and_low_threshold() -> None:
    """演示档必须是纯 mock：真实账号撑不起「增量超阈值 → 告警」这条动线。"""
    assert DEMO["provider_chain"] == "mock"
    assert str(DEMO["threshold"]) == "20"
    assert DEMO["scan_mode"] == "interval"
    assert DEMO["scan_interval_minutes"] == 3


def test_formal_matches_interview_requirements() -> None:
    """正式档对齐题目基础要求：12/18/22 点扫描、增量 > 300 提醒、真实源优先。"""
    assert FORMAL["scan_mode"] == "cron"
    assert FORMAL["scan_cron_hours"] == "12,18,22"
    assert str(FORMAL["threshold"]) == "300"
    assert str(FORMAL["provider_chain"]).startswith("browser")


def test_preset_keys_is_the_union() -> None:
    assert set(PRESET_KEYS) == set(FORMAL) | set(DEMO)
    assert tuple(PRESET_KEYS) == tuple(sorted(PRESET_KEYS))


# ---------------------------------------------------------------- 名字解析
@pytest.mark.parametrize("alias", ["demo", "on", "DEMO", "  demo  ", "演示"])
def test_alias_resolves_to_demo(alias: str) -> None:
    assert normalize_preset_name(alias) == "demo"
    assert resolve_preset(alias) == DEMO


@pytest.mark.parametrize("alias", ["formal", "off", "prod", "正式"])
def test_alias_resolves_to_formal(alias: str) -> None:
    assert resolve_preset(alias) == FORMAL


def test_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="未知档位"):
        normalize_preset_name("production-ish")


# ---------------------------------------------------------------- 反推当前档位
def test_preset_of_recognizes_full_match() -> None:
    assert preset_of(dict(DEMO)) == "demo"
    assert preset_of(dict(FORMAL)) == "formal"


def test_preset_of_flags_half_switched_config() -> None:
    """节奏和阈值都改了、数据源没改 —— 必须报「混合」，不能报演示档。

    这就是用户看到的现象：以为自己在演示档，其实数据源还在真实源上。
    """
    half = dict(FORMAL)
    half.update({"scan_mode": "interval", "scan_interval_minutes": 3, "threshold": 10})
    assert preset_of(half) == "mixed"


# ---------------------------------------------------------------- 切档接口
async def test_preset_endpoint_applies_the_whole_preset(client) -> None:
    await client.put("/api/config/preset", json={"name": "formal"})   # 归一化起点
    r = await client.put("/api/config/preset", json={"name": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert body["preset"] == "demo"

    res = body["resolved"]
    assert res["scan_mode"] == "interval"
    assert str(res["threshold"]) == "20"
    assert str(res["provider_chain"]).strip() == "mock", "演示档没切数据源就会没有 mock 增量"
    # 前端 toast 要报「改了几格」，所以 changed 得带上真正变动的键
    assert "provider_chain" in body["changed"]


async def test_preset_endpoint_switches_back_to_real_source(client) -> None:
    await client.put("/api/config/preset", json={"name": "formal"})   # 归一化起点
    await client.put("/api/config/preset", json={"name": "demo"})
    body = (await client.put("/api/config/preset", json={"name": "formal"})).json()
    res = body["resolved"]
    assert body["preset"] == "formal"
    assert res["scan_mode"] == "cron"
    assert str(res["threshold"]) == "300"
    assert "browser" in str(res["provider_chain"])


async def test_preset_endpoint_is_idempotent(client) -> None:
    """重复切同一档：不报错，且第二次没有可改的格。"""
    # 先归一化起点，别依赖出厂种子恰好等于哪一档
    await client.put("/api/config/preset", json={"name": "formal"})
    first = (await client.put("/api/config/preset", json={"name": "demo"})).json()
    second = (await client.put("/api/config/preset", json={"name": "demo"})).json()
    assert first["changed"]
    assert second["changed"] == {}
    assert second["resolved"] == first["resolved"]


async def test_preset_endpoint_rejects_unknown_name(client) -> None:
    r = await client.put("/api/config/preset", json={"name": "nope"})
    assert r.status_code == 400
    assert "未知档位" in r.json()["detail"]


async def test_config_exposes_current_preset(client) -> None:
    """`/api/config` 要能直接告诉前端「现在是哪一档」，包括「半档」这个状态。"""
    await client.put("/api/config/preset", json={"name": "formal"})
    body = (await client.get("/api/config")).json()
    assert body["preset"] == "formal"

    # 只改一格 → 混合档位（前端会高亮提示）
    await client.put("/api/config", json={"key": "provider_chain", "value": "mock"})
    body = (await client.get("/api/config")).json()
    assert body["preset"] == "mixed"

    await client.put("/api/config/preset", json={"name": "formal"})
    assert (await client.get("/api/config")).json()["preset"] == "formal"


async def test_preset_write_is_verified_and_repairs_a_lost_write(client, monkeypatch) -> None:
    """写完必须读回来核对：少写一格要自动补上，不能留个「半套档位」。

    成因是真实的：`set_config` 逐格写、`get_config()` 优先读飞书 —— 飞书那一步
    慢半拍或被限流，读回来的就是旧值或半套，看起来像「切了但没生效」。
    2026-09-16 用户连点 4 次「恢复正式档」就是这个体验（每次都不知道成没成）。
    """
    from app.storage.sqlite import SqliteStorage

    real = SqliteStorage.set_config
    dropped = {"n": 0}

    async def flaky(self, key, value):
        # 只丢**第一次** provider_chain 的写，模拟「这一步没落地」
        if key == "provider_chain" and dropped["n"] == 0:
            dropped["n"] += 1
            return
        await real(self, key, value)

    monkeypatch.setattr(SqliteStorage, "set_config", flaky)

    await client.put("/api/config/preset", json={"name": "formal"})   # 归一化起点
    body = (await client.put("/api/config/preset", json={"name": "demo"})).json()

    assert dropped["n"] == 1, "这次测试得真的丢过一次写"
    assert body["verified"] is True, "核对发现少了 provider_chain，应当补写后通过"
    assert body["preset"] == "demo"
    assert str(body["resolved"]["provider_chain"]).strip() == "mock"


async def test_concurrent_preset_writes_do_not_leave_a_half_preset(client) -> None:
    """两个切档请求同时打进来：锁保证串行，最终读到的必须是**完整**的某一档。

    以前没有这把锁：6 格 × 2 个请求的写入会交叉，`GET /api/config` 可能返回
    谁也没定义过的 `mixed`（看板会高亮「⚠ 混合档位」，用户以为切失败了）。
    """
    await client.put("/api/config/preset", json={"name": "formal"})   # 归一化起点
    await asyncio.gather(
        client.put("/api/config/preset", json={"name": "demo"}),
        client.put("/api/config/preset", json={"name": "formal"}),
    )
    final = (await client.get("/api/config")).json()
    assert final["preset"] in ("demo", "formal"), f"并发切档后停在半套档位：{final['preset']}"


# ---------------------------------------------------------------- 前端不漂移
def test_frontend_uses_the_preset_endpoint() -> None:
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "/config/preset" in src, "前端切档必须走服务端档位定义"


@pytest.mark.parametrize("literal", [
    "scan_mode: 'interval'",
    "scan_cron_hours: '12,18,22'",
    "provider_chain: 'mock'",
    "provider_chain: 'browser,mock'",
])
def test_frontend_does_not_hardcode_preset_values(literal: str) -> None:
    """前端不许出现档位键值对 —— 一旦出现，说明又开始各写一份了。

    上一次就是这么坏掉的：按钮里只写了 scan_mode / interval_minutes / threshold，
    漏掉 provider_chain，于是「切换成功」但数据源没换。
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert literal not in src, f"档位定义只该在 app/core/presets.py，不该出现在前端：{literal}"


# ---------------------------------------------------------------- 看板要说真话
def test_sidebar_reads_the_current_chain_not_the_last_round() -> None:
    """侧栏「数据源」必须显示**当前配置的链**，不许拿上一轮结果冒充。

    用户当场问过：点「恢复正式档」后那格仍显示 mock。因为它读的是
    `last_round.source`，而切档不产生新的扫描轮次 —— 自然滞后一轮。
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    # 链来自当前配置……
    assert re.search(r"chain\s*=\s*String\(s\.config\?\.provider_chain", src), \
        "侧栏要先从配置里解析出当前链"
    # ……而且「数据源」那格显示的就是它（不是 last_round）
    assert re.search(r"\$\('#chainInfo'\)\.textContent\s*=\s*chain", src), \
        "「数据源」要显示当前链，别拿上一轮结果顶"


def test_sidebar_has_a_separate_slot_for_the_last_round_source() -> None:
    """上一轮实际用到的源得单独一格 —— 它和「当前链」回答的是两个问题。

    混在一格里，用户就无法区分「档位没切成功」和「切成功了但还没重跑」。
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "$('#lastSrcInfo')" in src
    assert 'id="lastSrcInfo"' in html


def test_sidebar_never_fakes_the_source_with_a_hardcoded_fallback() -> None:
    """查不到就说「—」，不许拿一个「像数据源」的字符串兜底。

    登录态过期那一轮 `source` 是空串，`|| 'mock'` 会让侧栏显示「数据源 mock」——
    明明什么都没采到。空串/破折号这类「明摆着是空」的兜底不受此限。
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    fake = re.search(r"source\s*(\|\||\?\?)\s*['\"](browser|mock|thirdparty)", src)
    assert not fake, f"数据源不许用真实源名兜底：{fake.group(0) if fake else ''}"
