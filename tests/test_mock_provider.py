"""mock 数据源与降级链的测试。

mock provider 是 P0 演示的地基，它必须满足两条硬性质：
① 点赞数**随轮次单调递增**（否则增量会出现负数，演示很难看）；
② 每个账号的前两个视频**每轮必然跨阈值**（否则告警链路没东西可演示）。

降级链部分还钉住一条**数据真实性**语义（2026-09-16 补）：
真实源「抛异常」和「成功返回空」必须区别对待——前者允许降到 mock，
后者**禁止** mock 兜底，否则「账号这几天没发作品 / 视频没人评论」会被
静默伪装成一批假数据，看板上完全分不出真假。
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.mock import GROWTH_TIERS, MockProvider
from app.providers.registry import ProviderRegistry


class _FakeRealProvider:
    """真实数据源的替身（synthetic=False），用来免网络地驱动降级链。

    行为可配：返回指定数据、返回空、或抛 ProviderError。
    """

    name = "browser"
    synthetic = False

    def __init__(
        self,
        *,
        videos: list[dict] | None = None,
        comments: list[dict] | None = None,
        boom: Exception | None = None,
        boom_comments: Exception | None = None,
    ) -> None:
        self._videos = videos if videos is not None else []
        self._comments = comments if comments is not None else []
        self._boom = boom
        self._boom_comments = boom_comments

    async def fetch_videos(self, accounts: list[dict], days: int) -> list[dict]:
        if self._boom:
            raise self._boom
        return self._videos

    async def fetch_comments(self, video_id: str, limit: int = 20) -> list[dict]:
        if self._boom_comments:
            raise self._boom_comments
        return self._comments


def _registry_with(fake: _FakeRealProvider) -> ProviderRegistry:
    """链 = browser → mock，browser 换成替身。"""
    reg = ProviderRegistry(
        Settings(provider_chain="browser,mock", browser_user_data_dir="")
    )
    reg._providers["browser"] = fake  # type: ignore[assignment]
    return reg


async def test_likes_monotonic_across_rounds() -> None:
    """最慢的一档每轮也只能涨 1 点多，所以这条断言其实是「增长率不能太小」的守门人。"""
    p = MockProvider()
    accounts = [{"name": "A", "sec_uid": "MS4wLjABAAAA_demo_a"}]

    r1 = {v["video_id"]: v["likes"] for v in await p.fetch_videos(accounts, days=3)}
    r2 = {v["video_id"]: v["likes"] for v in await p.fetch_videos(accounts, days=3)}
    r3 = {v["video_id"]: v["likes"] for v in await p.fetch_videos(accounts, days=3)}

    assert set(r1) == set(r2) == set(r3)
    for vid in r1:
        assert r1[vid] < r2[vid] < r3[vid], f"{vid} 的点赞数没有单调递增"


async def test_same_video_gets_same_curve_after_restart() -> None:
    """重启后同一视频必须落在同一条曲线上——靠 set_round_baseline 续上起点。"""
    accounts = [{"name": "A", "sec_uid": "MS4wLjABAAAA_demo_a"}]

    p1 = MockProvider()
    await p1.fetch_videos(accounts, 3)          # 第 1 轮
    r2_a = {v["video_id"]: v["likes"] for v in await p1.fetch_videos(accounts, 3)}   # 第 2 轮

    p2 = MockProvider()
    p2.set_round_baseline(1)                     # 模拟「库里已有 1 轮」后重启
    r2_b = {v["video_id"]: v["likes"] for v in await p2.fetch_videos(accounts, 3)}

    assert r2_a == r2_b


async def test_first_two_videos_are_hot() -> None:
    """前两个视频的增长率刻意拉高，保证 threshold=20 时每轮都告警。"""
    assert GROWTH_TIERS[0][0] == "爆款"
    assert GROWTH_TIERS[1][0] == "爆款"

    p = MockProvider()
    accounts = [{"name": "A", "sec_uid": "MS4wLjABAAAA_demo_a"}]
    await p.fetch_videos(accounts, 3)
    r2 = {v["video_id"]: v["likes"] for v in await p.fetch_videos(accounts, 3)}
    r3 = {v["video_id"]: v["likes"] for v in await p.fetch_videos(accounts, 3)}

    for vid in ("acc01_v01", "acc01_v02"):
        assert r3[vid] - r2[vid] > 20, f"{vid} 增量 {r3[vid] - r2[vid]} 不足以跨过 threshold=20"
    for vid in ("acc01_v05", "acc01_v06"):
        assert r3[vid] - r2[vid] < 20, f"长尾视频 {vid} 不该告警"


async def test_video_id_is_readable() -> None:
    """回归：早先用 sec_uid 切片会得到 'ount_a_v01' 这种看着像被截断的 id。"""
    p = MockProvider()
    videos = await p.fetch_videos(
        [{"name": "A", "sec_uid": "MS4wLjABAAAA_demo_account_a"},
         {"name": "B", "sec_uid": "MS4wLjABAAAA_demo_account_b"}],
        3,
    )
    ids = [v["video_id"] for v in videos]
    assert "acc01_v01" in ids and "acc02_v06" in ids
    assert len(ids) == len(set(ids))            # 不重复
    assert all(i.startswith("acc") for i in ids)


async def test_comments_are_deterministic_per_video() -> None:
    p = MockProvider()
    a = await p.fetch_comments("acc01_v01")
    b = await p.fetch_comments("acc01_v01")
    c = await p.fetch_comments("acc01_v02")
    assert [x["comment_id"] for x in a] == [x["comment_id"] for x in b]
    assert [x["content"] for x in a] == [x["content"] for x in b]
    assert [x["comment_id"] for x in a] != [x["comment_id"] for x in c]


async def test_degradation_chain_falls_through_to_mock() -> None:
    """默认链 thirdparty → browser → mock：前两档未配置，必然降级到 mock。"""
    from app.config import Settings

    # browser_user_data_dir 显式置空：真实 .env 里配了浏览器采集，
    # 不置空会让这条用例真的去启 Edge
    reg = ProviderRegistry(
        Settings(provider_chain="thirdparty,browser,mock", browser_user_data_dir="")
    )
    assert reg.chain == ["thirdparty", "browser", "mock"]

    videos, trace, name = await reg.fetch_videos([{"name": "A", "sec_uid": "u1"}], days=3)
    assert name == "mock"
    assert trace == ["thirdparty:failed", "browser:failed", "mock:ok"]
    assert len(videos) == 6


async def test_chain_can_be_narrowed_to_mock_only() -> None:
    from app.config import Settings

    reg = ProviderRegistry(Settings(provider_chain="mock"))
    _, trace, name = await reg.fetch_videos([{"name": "A", "sec_uid": "u1"}], days=3)
    assert name == "mock" and trace == ["mock:ok"]


async def test_registry_round_baseline_reaches_provider() -> None:
    """build_deps 会把「已入库轮次数」灌给 registry，避免重启后点赞回落。"""
    from app.config import Settings

    accounts = [{"name": "A", "sec_uid": "u1"}]

    fresh = ProviderRegistry(Settings(provider_chain="mock"))
    v_fresh, _, _ = await fresh.fetch_videos(accounts, days=3)

    resumed = ProviderRegistry(Settings(provider_chain="mock"))
    resumed.set_round_baseline(5)
    v_resumed, _, _ = await resumed.fetch_videos(accounts, days=3)

    fresh_map = {v["video_id"]: v["likes"] for v in v_fresh}
    resumed_map = {v["video_id"]: v["likes"] for v in v_resumed}
    for vid in fresh_map:
        assert resumed_map[vid] > fresh_map[vid], f"{vid} 的续跑起点没有生效"


# --------------------------------------------------- 降级语义（区分两种「没数据」）
ACCOUNTS = [{"name": "A", "sec_uid": "u1"}]


async def test_real_source_empty_does_not_fall_back_to_mock() -> None:
    """真实源**成功返回空** = 明确知道现在没有数据 → 不能用 mock 补假的。

    对应真实场景：账号 3 天内没发新作品（接口正常，21 条作品全在窗口外）。
    以前这里会静默降级到 mock，于是「没发作品」被记成「采集到 12 条视频」。
    """
    reg = _registry_with(_FakeRealProvider(videos=[]))

    videos, trace, name = await reg.fetch_videos(ACCOUNTS, days=3)

    assert videos == [], "真实源说没数据时不许返回模拟视频"
    assert name == "", "没有真实数据源生效，来源必须是空"
    assert trace == ["browser:empty", "mock:skipped:real-source-empty"]


async def test_broken_real_source_is_not_patched_with_mock() -> None:
    """真实源**坏了**（登录态过期 / 被风控）→ 不许用 mock 顶替，本轮如实失败。

    对照组是 `test_degradation_chain_falls_through_to_mock`：「没接真实源」才降级到 mock，
    判据就是 `ProviderNotConfigured`。两者差一个异常类型，看板上差的是
    「12 条 source=mock 的行（用户以为是真数据）」和「一行不写、台账写明原因」。

    2026-09-16 的实例：抖音登录态掉了，browser 对 3 个账号全返回 0 条，
    整轮被静默跳过，快照/增量/告警三张表都没动，用户来问「为什么新增账号扫描后什么都没有」。
    """
    reg = _registry_with(_FakeRealProvider(boom=ProviderError("登录态已过期")))

    with pytest.raises(ProviderError) as err:
        await reg.fetch_videos(ACCOUNTS, days=3)

    assert "登录态已过期" in str(err.value)
    assert "mock:skipped:real-source-unavailable" in str(err.value), "链路要写进错误里"
    assert reg.collect_warnings and "browser" in reg.collect_warnings[0]
    assert reg.collect_failures == 1


async def test_unconfigured_real_source_is_a_quiet_degrade() -> None:
    """「本来就没接」是预期内的降级：照样用 mock 兜底，且不往台账里灌废话。

    没有这条区分的话，正式档每一轮的备注里都会挂一句「thirdparty 未配置」，
    和一直响的警报器没区别。
    """
    from app.providers.base import ProviderNotConfigured

    reg = _registry_with(_FakeRealProvider(boom=ProviderNotConfigured("thirdparty 未配置")))

    videos, trace, name = await reg.fetch_videos(ACCOUNTS, days=3)

    assert name == "mock"
    assert trace == ["browser:failed", "mock:ok"]
    assert len(videos) == 6
    assert reg.collect_warnings == [], "未配置不该进台账"
    assert reg.collect_failures == 0


async def test_real_comments_empty_are_not_filled_with_mock() -> None:
    """视频没人评论是合法结果，不许 mock 造 12 条假评论糊上来。"""
    reg = _registry_with(_FakeRealProvider(comments=[]))

    comments, source = await reg.fetch_comments("7412345678901234567")

    assert comments == []
    assert source == ""


async def test_real_comment_failure_falls_back_to_mock() -> None:
    """真实源干不了这活（例如 mock 的 video_id 拼不出视频页）→ 才换源。"""
    reg = _registry_with(_FakeRealProvider(boom_comments=ProviderError("不是真实视频 ID")))

    comments, source = await reg.fetch_comments("acc01_v01")

    assert source == "mock"
    assert len(comments) == 12  # mock 池子里的 12 条


async def test_collect_marks_skip_when_window_has_no_new_videos() -> None:
    """窗口内没有新作品要在 state 里留下 skip_reason，而不是记成采集错误。

    记成错误会把排查方向带偏（去查 provider），而且对外 200 成功、
    看板上看起来「跑过了」，其实什么都没采集。
    """
    from app.nodes.collect import make_collect_videos

    node = make_collect_videos(_registry_with(_FakeRealProvider(videos=[])))

    out = await node({"run_id": "R1", "accounts": ACCOUNTS, "lookback_days": 3})

    assert out["videos"] == []
    assert "没有新作品" in out["skip_reason"]
    assert out["source"] == ""
    assert "errors" not in out, "这是数据状态不是故障，不该记 error"


async def test_collect_failure_is_recorded_as_error_not_skip() -> None:
    """对照用例：真故障走 error 分支（记 error_count），不要走 skip。"""
    from app.nodes.collect import make_collect_videos

    reg = _registry_with(_FakeRealProvider(boom=ProviderError("boom")))
    reg._providers["mock"] = _FakeRealProvider(boom=ProviderError("mock 也挂了"))
    node = make_collect_videos(reg)

    out = await node({"run_id": "R1", "accounts": ACCOUNTS, "lookback_days": 3})

    assert out["videos"] == []
    assert out.get("skip_reason") is None
    assert out["error_count"] == 1
    assert "采集失败" in out["note"], "失败原因要落进轮次备注"


async def test_collect_writes_account_notes_into_round_note() -> None:
    """账号级结论要落到轮次台账的备注上，用户才看得出「为什么什么都没有」。

    场景：新增了一个账号，扫描后三张表都没动。老代码只打日志，
    台账上写着「成功、跳过」，用户只能来问。现在备注里直接有答案。
    """
    from app.nodes.collect import make_collect_videos

    fake = _FakeRealProvider(
        videos=[
            {
                "video_id": "7412345678901234567",
                "account": "A",
                "title": "t",
                "publish_time": "2026-09-16T00:00:00+00:00",
                "likes": 1,
                "comments": 0,
                "shares": 0,
                "source": "browser",
            }
        ]
    )
    fake.account_notes = ["账号「新号」一条作品都没抓到（sec_uid 可能失效或被风控…）"]
    fake.account_failures = 1
    node = make_collect_videos(_registry_with(fake))

    out = await node({"run_id": "R1", "accounts": ACCOUNTS, "lookback_days": 3})

    assert len(out["videos"]) == 1, "抓到的账号照常入库"
    assert "新号" in out["note"]
    assert out["error_count"] == 1, "账号没抓到算异常，要计数"
    assert out["errors"] == fake.account_notes


async def test_collect_notes_empty_window_without_counting_error() -> None:
    """「账号窗口内没新作品」要写备注，但**不计 error**：那是数据状态不是故障。"""
    from app.nodes.collect import make_collect_videos

    fake = _FakeRealProvider(videos=[])
    fake.account_notes = ["账号「A」回看 3 天内没有新作品（接口正常）"]
    fake.account_failures = 0
    node = make_collect_videos(_registry_with(fake))

    out = await node({"run_id": "R1", "accounts": ACCOUNTS, "lookback_days": 3})

    assert out["videos"] == []
    assert "没有新作品" in out["skip_reason"]
    assert "账号「A」" in out["skip_reason"], "备注要能解释为什么这轮空了"
    assert out["error_count"] == 0, "数据状态不该计数成故障"

