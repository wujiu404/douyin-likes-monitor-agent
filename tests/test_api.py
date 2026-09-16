"""HTTP 层的集成测试。

用 httpx 的 ASGITransport 直接打 FastAPI app（不起真实端口），
手动进入 lifespan，这样测的是**真的装配流程**——存储、检查点、调度器、
前端挂载全都走一遍。覆盖「手动触发扫描 → 看板数据 → 人工确认」这条主路径。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("CHECKPOINT_PATH", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("NOTIFIER_BACKEND", "local")
    monkeypatch.setenv("PROVIDER_CHAIN", "thirdparty,browser,mock")
    # 同 conftest.settings：别让真实 .env 的浏览器配置漏进测试
    monkeypatch.setenv("BROWSER_USER_DATA_DIR", "")
    monkeypatch.setenv("LLM_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    transport = ASGITransport(app=main.app)
    async with main.app.router.lifespan_context(main.app):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            # 出厂《配置表》的阈值是 300（真实数据的口径）；用例跑 mock，
            # 爆款档每轮 +22~25，钉到 20 才能稳定触发告警。用公开接口改，顺便过一遍控制面。
            await c.put("/api/config", json={"key": "threshold", "value": 20})
            yield c
    get_settings.cache_clear()


# ---------------------------------------------------------------- 基础
async def test_health(client) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_openapi_has_all_routes(client) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    for p in (
        "/api/health", "/api/scan", "/api/scan/status", "/api/login_status", "/api/stats",
        "/api/accounts", "/api/videos", "/api/deltas", "/api/alerts",
        "/api/runs", "/api/reviews", "/api/reviews/{thread_id}", "/api/config",
    ):
        assert p in paths, f"缺少路由 {p}"


async def test_frontend_is_served(client) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "抖音点赞监控" in r.text

    for asset in ("/style.css", "/app.js"):
        a = await client.get(asset)
        assert a.status_code == 200 and a.content


# ---------------------------------------------------------------- 主路径
async def test_scan_then_dashboard(client) -> None:
    r1 = await client.post("/api/scan")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["source"] == "mock"
    assert d1["video_count"] == 12
    assert d1["alert_count"] == 0                       # 首轮全是首见视频
    # 降级链现在由《配置表》驱动（出厂 browser,mock），不再是 .env 里的 PROVIDER_CHAIN
    assert d1["provider_trace"] == ["browser:failed", "mock:ok"]

    r2 = await client.post("/api/scan")
    d2 = r2.json()
    assert d2["alert_count"] == 4
    assert len(d2["comment_threads"]) == 12      # comment_scope=all：每条视频都扫
    assert d2["pending_comments"] > 0

    stats = (await client.get("/api/stats")).json()
    assert stats["rounds"] == 2
    assert stats["accounts"] == 2
    assert stats["snapshots"] == 24
    assert stats["alerts"] == 4
    assert stats["alerts_sent"] == 4
    assert stats["last_round"]["run_id"] == d2["run_id"]


async def test_scan_status_and_busy_guard(client) -> None:
    st = (await client.get("/api/scan/status")).json()
    assert st["scanning"] is False
    # 本 fixture 里 SCHEDULER_ENABLED=false，所以调度是关的
    assert st["scheduler"]["enabled"] is False


async def test_data_endpoints(client) -> None:
    await client.post("/api/scan")
    await client.post("/api/scan")

    # 快照表是**每轮追加**的，所以两轮之后是 24 条（每轮 12 条 × 2）
    videos = (await client.get("/api/videos?limit=50")).json()["items"]
    assert len(videos) == 24
    assert all(v["video_id"].startswith("acc") for v in videos)

    # 单视频查询只回该视频的历史
    one = (await client.get("/api/videos?limit=50&video_id=acc01_v01")).json()["items"]
    assert len(one) == 2

    deltas = (await client.get("/api/deltas?limit=50")).json()["items"]
    assert len(deltas) == 24

    alerts_only = (await client.get("/api/deltas?limit=50&only_alert=true")).json()["items"]
    assert len(alerts_only) == 4
    assert all(d["delta"] > 20 for d in alerts_only)

    log = (await client.get("/api/alerts?limit=50&kind=alert")).json()["items"]
    assert len(log) == 4

    # 两类推送共用一张日志表，靠 kind 分流：「涨了多少」和「有几条等你拍板」是两个问题
    review_log = (await client.get("/api/alerts?limit=50&kind=review")).json()["items"]
    assert review_log, "评论命中应该产生提醒推送记录（选做要求的最后一步）"
    assert all(r["kind"] == "review" for r in review_log)

    runs = (await client.get("/api/runs?limit=50")).json()["items"]
    assert len(runs) == 2
    assert runs[0]["trigger_type"] == "manual"


async def test_review_flow_over_http(client) -> None:
    await client.post("/api/scan")
    await client.post("/api/scan")

    pending = (await client.get("/api/reviews?status=pending&limit=200")).json()["items"]
    assert pending, "第 2 轮之后应该有待确认的评论"

    thread_id = pending[0]["thread_id"]
    detail = (await client.get(f"/api/reviews/{thread_id}")).json()
    assert detail["suspended"] is True, "子图应停在 human_review 等确认"
    items = detail["items"]
    assert items and all(i["status"] == "pending" for i in items)

    decisions = {i["comment_id"]: ("approved" if n < 2 else "ignored") for n, i in enumerate(items)}
    res = (await client.post(f"/api/reviews/{thread_id}", json={"decisions": decisions})).json()
    assert res["ok"] is True
    assert res["resumed"] is True
    assert {i["status"] for i in res["items"]} == {"approved", "ignored"}

    after = (await client.get(f"/api/reviews/{thread_id}")).json()
    assert after["suspended"] is False, "恢复后子图应交出控制权"

    # 其它线程不受影响
    others = (await client.get("/api/reviews?status=pending&limit=200")).json()["items"]
    assert others and all(o["thread_id"] != thread_id for o in others)


async def test_review_after_completion_only_updates_status(client) -> None:
    """子图已跑完时再提交，走「仅更新状态」分支，不报错。"""
    await client.post("/api/scan")
    await client.post("/api/scan")

    pending = (await client.get("/api/reviews?status=pending&limit=200")).json()["items"]
    thread_id = pending[0]["thread_id"]
    detail = (await client.get(f"/api/reviews/{thread_id}")).json()
    decisions = {i["comment_id"]: "approved" for i in detail["items"]}

    first = (await client.post(f"/api/reviews/{thread_id}", json={"decisions": decisions})).json()
    assert first["resumed"] is True

    second = (await client.post(f"/api/reviews/{thread_id}", json={"decisions": decisions})).json()
    assert second["ok"] is True
    assert second["resumed"] is False              # 已经结束了
    assert {i["status"] for i in second["items"]} == {"approved"}


async def test_review_unknown_thread_returns_404(client) -> None:
    r = await client.get("/api/reviews/不存在的线程")
    assert r.status_code == 404


async def test_config_read_and_update(client) -> None:
    cfg = (await client.get("/api/config")).json()
    assert cfg["resolved"]["threshold"] == 20
    assert cfg["resolved"]["scan_mode"] == "cron"
    assert len(cfg["items"]) == 8

    r = await client.put("/api/config", json={"key": "threshold", "value": 10})
    assert r.status_code == 200
    cfg = (await client.get("/api/config")).json()
    assert cfg["resolved"]["threshold"] == 10

    # 阈值降到 4 → 中速视频也开始告警（默认 20 时只有 4 条爆款告警）
    await client.put("/api/config", json={"key": "threshold", "value": 4})
    await client.post("/api/scan")
    d = (await client.post("/api/scan")).json()
    assert d["alert_count"] > 4, f"阈值 4 下告警应多于 4 条，实际 {d['alert_count']}"


async def test_accounts_crud(client) -> None:
    items = (await client.get("/api/accounts?only_enabled=false")).json()["items"]
    assert len(items) == 2

    r = await client.post(
        "/api/accounts",
        json={"name": "新监控号", "sec_uid": "MS4wLjABAAAA_new", "homepage": "", "enabled": True},
    )
    assert r.status_code == 200
    assert len((await client.get("/api/accounts?only_enabled=false")).json()["items"]) == 3

    r = await client.delete("/api/accounts/MS4wLjABAAAA_new")
    assert r.status_code == 200
    assert len((await client.get("/api/accounts?only_enabled=false")).json()["items"]) == 2


async def test_all_accounts_disabled_degrades_gracefully(client) -> None:
    """一条账号都没有不是「采集失败」，而是一个可解释的跳过。

    早先空账号列表会被抛成 RuntimeError，最后记成「降级链全部失败」，
    把人误导到 provider 上去查——其实该去账号页加账号。已修：
    图正常走到 END，台账 error_count=0，对外 409（可解释的状态），不是 500。
    """
    for acc in (await client.get("/api/accounts?only_enabled=false")).json()["items"]:
        await client.delete(f"/api/accounts/{acc['sec_uid']}")
    assert len((await client.get("/api/accounts")).json()["items"]) == 0

    r = await client.post("/api/scan")
    assert r.status_code == 409, r.text
    assert "没有启用" in r.json()["detail"]

    runs = (await client.get("/api/runs")).json()["items"]
    assert len(runs) == 1, "跳过的轮次也要留下台账"
    assert runs[0]["video_count"] == 0
    assert runs[0]["error_count"] == 0, "跳过不是错误，不该记进 error_count"
    assert runs[0]["note"].startswith("跳过")


# ---------------------------------------------------------------- 登录态体检
async def test_login_status_reports_unconfigured_without_exploding(client) -> None:
    """browser 没接的时候不能 500，要给可解释的 JSON：configured=false / logged_in=null。

    「这一档没接」是预期内的降级，不是故障——接口不该把它报成错误。
    """
    r = await client.get("/api/login_status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["logged_in"] is None
    assert body["detail"], "得有一句人能看懂的原因"


async def test_login_status_reflects_provider_answer(client, monkeypatch) -> None:
    """登录/过期两种结论都要如实透出，过期时 detail 必须带修复命令。"""
    from app.providers.browser import BrowserProvider

    async def logged_in(self):
        return True

    monkeypatch.setattr(BrowserProvider, "check_login", logged_in)
    body = (await client.get("/api/login_status")).json()
    assert body == {
        "configured": True,
        "logged_in": True,
        "detail": "登录态正常（profile 里有 sessionid）",
    }

    async def expired(self):
        return False

    monkeypatch.setattr(BrowserProvider, "check_login", expired)
    body = (await client.get("/api/login_status")).json()
    assert body["logged_in"] is False
    assert "登录态已过期" in body["detail"]
    assert "login_douyin.py" in body["detail"], "detail 要带上修复命令，别让人再去翻文档"


async def test_login_status_does_not_block_while_scanning(client, monkeypatch) -> None:
    """扫描中不许去抢 provider 的锁：一轮真实采集 100 秒上下，只读体检不能挂在那儿等。"""
    import app.api.routes_scan as routes_scan

    monkeypatch.setattr(routes_scan, "is_scanning", lambda: True)
    r = await client.get("/api/login_status")
    assert r.status_code == 200
    body = r.json()
    assert body["logged_in"] is None
    assert "正在扫描" in body["detail"]


async def test_login_status_swallows_probe_error(client, monkeypatch) -> None:
    """体检自己炸了也不能把只读接口变成 500：logged_in=null + 原因。"""
    from app.providers.browser import BrowserProvider

    async def boom(self):
        raise RuntimeError("profile 被占用")

    monkeypatch.setattr(BrowserProvider, "check_login", boom)
    r = await client.get("/api/login_status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["logged_in"] is None
    assert "profile 被占用" in body["detail"]


# ---------------------------------------------------------------- 看板侧栏
async def test_stats_carries_current_chain_for_the_sidebar(client) -> None:
    """侧栏「数据源」显示的是**当前配置的链**，所以 stats 必须带上它。

    这一格以前读 `last_round.source`：切档不产生新扫描轮次，于是切回正式档后
    它仍显示上一轮的 mock，看着像档位没切成功。
    """
    body = (await client.get("/api/stats")).json()
    assert body["config"]["provider_chain"], "侧栏得能拿到当前链，否则只能退回读上一轮结果"

    await client.put("/api/config", json={"key": "provider_chain", "value": "mock"})
    assert (await client.get("/api/stats")).json()["config"]["provider_chain"] == "mock"
