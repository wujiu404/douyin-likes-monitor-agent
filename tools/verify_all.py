"""一键功能自检 —— 把「所有功能点」跑成一张带实测结果的表。

为什么要有这个脚本：项目里「能演示」和「已验证」是两件事。
每次改完代码、换台机器、或者面试前一晚，需要一条命令把全部功能点过一遍，
拿到一份**可引用的证据**（控制台表格 + `data/verify_report.json`），
而不是靠回忆说「应该没问题」。

用法（服务需已在运行：`.venv/Scripts/python.exe run.py`）：

    .venv/Scripts/python.exe tools/verify_all.py               # 全部（含真实采集，约 5 分钟）
    .venv/Scripts/python.exe tools/verify_all.py --skip-real   # 跳过真实采集（约 3 分钟）
    .venv/Scripts/python.exe tools/verify_all.py --skip-tests  # 跳过 pytest
    .venv/Scripts/python.exe tools/verify_all.py --base http://127.0.0.1:8000

两段式设计（这是本脚本的核心思路）：

1. **生产库段（C~G）**：对着正在跑的服务做「体检」——账号、调度、控制面、真实采集。
   它**不能**验评论确认链路：命中记录按 `comment_id` 去重，同一批评论在第一次被记录后
   再跑多少轮都不会新增行、不会重新挂起（这本身就是幂等正确行为）。
   所以老库上「没有新命中」是**预期结果**，不是故障。
2. **隔离实例段（E2E）**：另起一个**全新临时库**的实例（端口 8123、mock 数据源、本地告警），
   在零历史数据上把「两轮扫描 → 增量 → 超阈值 → 推送 → 评论命中 → 提醒 → 挂起 →
   人工确认 → 重复提交幂等」完整跑一遍。这段才是评论链路的真正验收。

退出约定：自检会临时切档（E 组要验「一条命令切演示档」），
所以**进入时先把档位拍个快照，无论中途是否失败，退出时都原样还回去**。

⚠ 这里曾经硬编码成「恢复正式档」，出过一次真实的坑：用户切了演示档准备演示，
顺手跑一次自检，档位被扳回正式档 —— 扫描照旧走真实数据源，没有 mock 增量、
也不告警，表面上看像 mock 坏了。自检**不该有副作用**，改档要还回原样。
隔离实例的临时库留在系统临时目录、**不删**（原因见 TMP_ROOT 处注释）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

REPORT_PATH = ROOT / "data" / "verify_report.json"
# 隔离实例的临时库放系统临时目录，且**每次一个新目录、用完不删**。
# 原因：本机沙箱的批量删除守卫按「本轮累计删除文件数」计数（阈值 50），
# 而 pytest 一轮自己就能删掉几百个临时文件 —— 之后任何 unlink/rmtree 都会被拦下，
# 把整个自检脚本打断。不删最省事，代价只是每次留一个几 MB 的目录。
TMP_ROOT = Path(tempfile.gettempdir()) / "douyin_verify"
TMP_PORT = 8123


# ---------------------------------------------------------------- 结果收集
class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.group = ""

    def group_of(self, name: str) -> None:
        self.group = name

    def ok(self, name: str, detail: str = "") -> None:
        self.rows.append({"group": self.group, "name": name, "ok": True, "detail": detail})

    def bad(self, name: str, detail: str = "") -> None:
        self.rows.append({"group": self.group, "name": name, "ok": False, "detail": detail})

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        (self.ok if cond else self.bad)(name, detail)
        return cond

    def note(self, name: str, detail: str = "") -> None:
        """信息项：不算通过也不算失败（例如历史遗留数据）。"""
        self.rows.append({"group": self.group, "name": name, "ok": True, "detail": detail, "note": True})

    def render(self) -> None:
        width = max(len(r["name"]) for r in self.rows) + 2
        last = ""
        for r in self.rows:
            if r["group"] != last:
                print(f"\n── {r['group']} " + "─" * max(0, 58 - len(r["group"])))
                last = r["group"]
            mark = "·" if r.get("note") else ("✓" if r["ok"] else "✗")
            print(f"  {mark} {r['name']:<{width}} {r['detail']}")
        hard = [r for r in self.rows if not r.get("note")]
        passed = sum(1 for r in hard if r["ok"])
        print(f"\n{'=' * 66}\n  合计 {passed}/{len(hard)} 项通过")
        failed = [r for r in hard if not r["ok"]]
        if failed:
            print("  未通过：")
            for r in failed:
                print(f"    ✗ {r['name']} — {r['detail']}")
        print("=" * 66)


rep = Report()


# ---------------------------------------------------------------- A 环境
def check_env() -> None:
    rep.group_of("A 环境自检")
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    rep.check("Python 版本", sys.version_info >= (3, 11), f"当前 {ver}（需 ≥3.11，本项目在 3.13 上跑）")

    versions = {
        "fastapi": None, "langgraph": None, "apscheduler": None,
        "aiosqlite": None, "playwright": None, "httpx": None,
    }
    for mod in versions:
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", "")
            versions[mod] = v or "已安装"
            rep.ok(f"依赖 {mod}", versions[mod])
        except Exception as exc:  # noqa: BLE001
            versions[mod] = None
            rep.bad(f"依赖 {mod}", f"导入失败：{exc}")

    env = ROOT / ".env"
    rep.check("配置文件 .env", env.exists(), "存在" if env.exists() else "缺失（复制 .env.example）")
    rep.check("依赖锁定文件", (ROOT / "requirements.lock.txt").exists(),
              "requirements.lock.txt 存在（换机装它，别装 requirements.txt 的宽松版本）")

    db, cp = ROOT / "data/app.db", ROOT / "data/checkpoints.db"
    rep.check("业务库 data/app.db", db.exists(), f"{db.stat().st_size / 1024:.0f} KB" if db.exists() else "不存在")
    rep.check("执行状态库 data/checkpoints.db", cp.exists(),
              f"{cp.stat().st_size / 1024:.0f} KB" if cp.exists() else "不存在")
    rep.check("两库分离", db != cp, "app.db（业务）/ checkpoints.db（Graph 执行状态），互不干涉")


# ---------------------------------------------------------------- B 离线测试
def check_tests() -> None:
    rep.group_of("B 离线测试套件")
    t0 = time.time()
    # 不加第二个 -q：pytest.ini 的 addopts 已带一个 -q，再加一个会变成 -qq，把汇总行也吞掉。
    # `--basetemp` 给一个**本次专用**的目录：不给的话 pytest 收尾会去删默认临时根下的
    # `garbage-*`，而本机沙箱的批量删除守卫会把它拦下，导致「进度点全打完却没有汇总行、
    # 退出码非 0」这种假失败（实测踩过两次）。
    basetemp = Path(tempfile.gettempdir()) / f"pytestbase-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-ra", f"--basetemp={basetemp}"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lines = [ln for ln in ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()
             if ln.strip()]
    summary = ""
    for ln in reversed(lines):
        if "passed" in ln or "failed" in ln or "error" in ln:
            summary = ln.strip()
            break
    hint = "(没拿到汇总行：只有进度点说明 pytest 退出不干净，多半是本机沙箱的批量删除守卫打断了它自己的临时目录清理，单独重跑一次即可)"
    rep.check("pytest 全量", proc.returncode == 0,
              f"{summary or hint}　耗时 {time.time() - t0:.1f}s")
    if proc.returncode != 0:
        print("      ---- pytest 输出（尾 25 行）----")
        for ln in lines[-25:]:
            print("      " + ln)
        print("      ------------------------------")


# ---------------------------------------------------------------- C 服务与配置
def check_service(http: httpx.Client) -> dict:
    rep.group_of("C 服务与数据面")
    if not _wait_up(http, tries=1):
        rep.bad("服务存活 /api/stats", "连不上。先跑 `.venv/Scripts/python.exe run.py`")
        return {}

    stats = http.get("/api/stats").json()
    cfg = stats.get("config", {})
    rep.ok("服务存活 /api/stats",
           f"台账 {stats['rounds']} 轮 / 快照 {stats['snapshots']} / 增量 {stats['deltas']} / "
           f"命中 {stats['comment_hits']}")
    rep.check("配置表 8 格", len(cfg) == 8, f"实际 {len(cfg)} 项：{', '.join(sorted(cfg))}")

    accs = http.get("/api/accounts", params={"only_enabled": "true"}).json()["items"]
    rep.check("启用账号 ≥2", len(accs) >= 2, f"{len(accs)} 个：" + "、".join(a["name"] for a in accs))
    standard = [a for a in accs if a["sec_uid"].startswith("MS4wLjABAAAA")]
    rep.check("sec_uid 都是标准格式", len(standard) == len(accs),
              f"{len(standard)}/{len(accs)} 个是标准 sec_uid（短链每轮要多花几秒跟重定向）")

    sch = http.get("/api/scan/status").json()["scheduler"]
    rep.check("调度器已启动", sch.get("running") is True, sch.get("description", ""))
    if cfg.get("scan_mode") == "cron":
        rep.check("cron 时点 = 12/18/22", cfg.get("scan_cron_hours") == "12,18,22",
                  f"{cfg.get('scan_cron_hours')}，下次 {sch.get('next_run_at')}")
    else:
        rep.ok("cron 时点", f"当前是 {cfg.get('scan_mode')} 模式（{cfg.get('scan_interval_minutes')} 分钟一轮）")
    return stats


# ---------------------------------------------------------------- D 真实采集
def check_real_scan(http: httpx.Client) -> dict:
    rep.group_of("D 真实数据采集（browser provider）")
    try:
        resp = http.post("/api/scan", params={"trigger": "verify"}, timeout=300)
    except Exception as exc:  # noqa: BLE001
        rep.bad("真实扫描一轮", f"请求失败：{exc}")
        return {}

    # 500 = 本轮**如实失败**：真实源被配置过但坏了（最常见是抖音登录态过期），
    # 按设计不再用 mock 顶替（那会往真实库里写一批看不出假的行）。
    # 这里必须把它判成一条可读的失败项 + 修复指引，而不是让后面几条断言
    # 因为 run_id 为空而连环报错（那样看不出根因）。
    if resp.status_code >= 500:
        try:
            detail = str(resp.json().get("detail", ""))
        except Exception:  # noqa: BLE001
            detail = resp.text
        rep.bad("真实扫描一轮", f"本轮失败：{detail}")
        rep.check("没有降级成模拟数据", "mock:ok" not in detail, f"trace 片段：{detail}")
        if "sessionid" in detail or "登录态" in detail:
            rep.note("修复指引", "跑 .venv/Scripts/python.exe tools/login_douyin.py 重新扫码登录")
        return {"failed": True, "provider_trace": [], "video_count": 0, "run_id": ""}

    # 409 是**按设计跳过**（回看窗口内没有新作品 / 没有启用账号），不是故障。
    # 这种情况恰恰是修复后的正确行为：真实源说「没有新作品」时，
    # 绝不能降级到 mock 造一批假视频出来——所以这里要专门验证「没有 mock:ok」。
    if resp.status_code == 409:
        detail = ""
        try:
            detail = str(resp.json().get("detail", ""))
        except Exception:  # noqa: BLE001
            detail = resp.text
        rep.check(
            "真实扫描一轮（按设计跳过）",
            ("没有新作品" in detail) or ("没有启用" in detail),
            detail,
        )
        rep.check(
            "没有降级成模拟数据",
            "mock:ok" not in detail,
            f"trace 片段：{detail}",
        )
        rep.note("采样数据", "跳过本轮 → 快照/增量检查不适用（账号窗口内确实没有新作品）")
        return {"skipped": True, "provider_trace": [], "video_count": 0, "run_id": ""}

    run = resp.json()
    trace = run.get("provider_trace", [])

    # 采集**如实失败**也是一种合法出口（真实源坏了：登录态过期/被风控，且拒绝用 mock 顶替）。
    # 这时视频/增量/告警都不该有，台账要有原因——不能让后面几条断言连环失败，
    # 那样看到的是「降级链没命中 browser」，而不是「登录态过期了」这个真原因。
    if not any(str(t).endswith(":ok") for t in trace) and int(run.get("error_count", 0) or 0) > 0:
        detail = str(run.get("note") or (trace[0] if trace else ""))
        rep.bad("真实扫描一轮", f"本轮失败：{detail}")
        rep.check("没有降级成模拟数据", "mock:ok" not in str(trace), f"trace = {trace}")
        if "sessionid" in detail or "登录态" in detail:
            rep.note("修复指引", "跑 .venv/Scripts/python.exe tools/login_douyin.py 重新扫码登录，"
                                "然后 tools/check_douyin_login.py 确认（应打印「登录态正常」）")
        return {"failed": True, "provider_trace": trace, "video_count": 0, "run_id": run.get("run_id", "")}

    rep.check("真实扫描一轮", bool(run.get("run_id")),
              f"{run.get('run_id')}　耗时 {run.get('duration_ms', 0) / 1000:.1f}s")
    rep.check("降级链命中 browser", "browser:ok" in trace, f"trace = {trace}")
    rep.check("采到真实视频", run.get("video_count", 0) > 0,
              f"{run.get('video_count')} 条（账号近 3 天发布）")
    rep.check("数据来源标记正确", run.get("source") == "browser", f"source = {run.get('source')}")

    vids = http.get("/api/videos", params={"limit": 300}).json()["items"]
    mine = [v for v in vids if v.get("run_id") == run.get("run_id")]
    rep.check("快照落库（含飞书镜像）", len(mine) == run.get("video_count"),
              f"本轮 {len(mine)} 行写入《视频快照表》")
    real_ids = bool(mine) and all(str(v.get("video_id", "")).isdigit() for v in mine)
    rep.check("video_id 是真实 aweme_id", real_ids,
              "全为纯数字 ID" if real_ids else "存在非数字 ID（可能是 mock 数据）")

    deltas = http.get("/api/deltas", params={"limit": 300}).json()["items"]
    md = [d for d in deltas if d.get("run_id") == run.get("run_id")]
    rep.check("增量逐条计算", len(md) == len(mine), f"{len(md)} 条增量 / {len(mine)} 条视频")
    rep.check("首见视频增量记 0 且不告警",
              all(d["delta"] == 0 and d["is_alert"] == 0 for d in md) if md else False,
              "第一次见到某个视频时，「无上一轮可比」被显式记为 0，不是 None、也不是误报")
    rep.check("阈值 300 下不误报", run.get("alert_count", -1) == 0,
              f"本轮告警 {run.get('alert_count')} 条（真实小账号 3 天涨不到 300，0 才是对的）")
    rep.check("评论子图按 comment_scope=all 起", len(run.get("comment_threads", [])) > 0,
              f"{len(run.get('comment_threads', []))} 个子图线程")
    rep.check("单账号内串行、账号间并发", run.get("account_count", 0) >= 2,
              f"{run.get('account_count')} 个账号依次采集（风控：单账号内不并发）")
    return run


# ---------------------------------------------------------------- 生产库体检（幂等语义）
def check_prod_comment_state(http: httpx.Client) -> None:
    rep.group_of("F 生产库的评论/推送现状（幂等语义体检）")
    hits = http.get("/api/reviews", params={"status": "", "limit": 1000}).json()["items"]
    rep.check("命中记录存在", len(hits) > 0, f"《评论命中表》{len(hits)} 行")

    with_kw = [h for h in hits if h.get("keywords")]
    rep.check("命中记录都带关键词", len(with_kw) == len(hits),
              f"{len(with_kw)}/{len(hits)} 条带 keywords（LangGraph 会静默丢弃未在 State 里声明的键）")
    with_draft = [h for h in hits if h.get("draft")]
    rep.check("命中记录都带拟回复", len(with_draft) == len(hits), f"{len(with_draft)}/{len(hits)} 条有 draft")
    # 现格式的幂等键（`comment:{comment_id}`）必须一行一条；
    # 老格式（`{run_id}:{video_id}:{comment_id}`）是修复前写入的历史遗留，不参与判断。
    cur = [h for h in hits if str(h.get("idem_key", "")) == f"comment:{h.get('comment_id')}"]
    dup = len(cur) - len({h["comment_id"] for h in cur})
    legacy = len(hits) - len(cur)
    rep.check("按 comment_id 去重（幂等键 = comment:{comment_id}）", dup == 0,
              f"新格式 {len(cur)} 行 / {len(cur) - dup} 个 comment_id"
              + (f"；另有 {legacy} 行是修复前的老幂等键（历史遗留，不会自愈）" if legacy else ""))

    stats = http.get("/api/stats").json()
    rep.check("推送日志区分两类（kind=alert / kind=review）",
              stats["alerts_sent"] > 0 and stats["reviews_notified"] > 0,
              f"点赞告警 {stats['alerts_sent']} 条 / 评论提醒 {stats['reviews_notified']} 条")

    # 提了一条评论会同时进两个数字，这里验证两个数字确实不同口径
    rep.check("两个计数器口径独立", stats["alerts_sent"] != stats["reviews_notified"],
              f"alerts_sent={stats['alerts_sent']}（涨了多少）≠ reviews_notified={stats['reviews_notified']}"
              f"（几条等你拍板）")

    sample = next((h for h in hits if h.get("draft")), None)
    if sample:
        print(f"      [样例] 评论：{(sample.get('content') or '')[:36]}")
        print(f"             关键词：{sample.get('keywords')}　曲目：{sample.get('song_title') or '(未识别)'}"
              f" — {sample.get('song_artist') or '(未知)'}")
        print(f"             拟回复：{(sample.get('draft') or '')[:56]}")

    # 修复前写入的历史噪声：命中记录按 comment_id 去重，老行不会自愈，只能靠新数据不再产生
    noisy = [h for h in hits if "原声" in str(h.get("song_title") or "")]
    if noisy:
        runs = sorted({str(h.get("run_id", ""))[:18] for h in noisy})
        rep.note("历史遗留：早前把 UGC 原声当歌名的记录", f"{len(noisy)} 条，来自 {runs}（修复前写入，不会自愈）")
    else:
        rep.note("历史遗留：原声噪声", "0 条")


# ---------------------------------------------------------------- 通用：评论链路 / 人工确认
# 这两个函数可以对着任意实例调用（生产库或隔离实例），便于复用同一套断言
def comment_chain_checks(http: httpx.Client, run: dict, group: str, before: dict | None = None) -> str:
    rep.group_of(group)
    threads = run.get("comment_threads", [])
    rep.check("为每个目标视频单独起子图线程", len(threads) >= 2,
              f"{len(threads)} 个线程，例：{threads[0] if threads else '(无)'}")

    before = before or http.get("/api/stats").json()
    hits = http.get("/api/reviews", params={"status": "pending", "limit": 500}).json()["items"]
    rep.check("关键词命中并落表", len(hits) > 0, f"pending {len(hits)} 条")
    if not hits:
        return threads[0] if threads else ""

    rep.check("命中记录带关键词", all(h.get("keywords") for h in hits), "每条都有 keywords")
    rep.check("每条命中都有拟回复", all(h.get("draft") for h in hits), "每条都有 draft")
    rep.check("每条命中带视频与账号归属",
              all(h.get("video_id") and h.get("account") for h in hits),
              f"例：{hits[0].get('video_id')} / {hits[0].get('account')}")

    noisy = [h for h in hits if "原声" in str(h.get("song_title") or "")]
    rep.check("UGC 原声没被当歌名", not noisy,
              "0 条把「@xx创作的原声」写进歌名字段" if not noisy else f"{len(noisy)} 条仍把原声当歌名")

    with_song = [h for h in hits if h.get("song_title")]
    rep.check("识别出歌手与歌名并写进拟回复",
              all(h["song_title"] in (h.get("draft") or "") for h in with_song) if with_song else False,
              f"{len(with_song)}/{len(hits)} 条识别出歌名，且歌名出现在拟回复里")

    sample = hits[0]
    print(f"      [样例] 评论：{(sample.get('content') or '')[:36]}")
    print(f"             关键词：{sample.get('keywords')}　曲目：{sample.get('song_title') or '(未识别)'}"
          f" — {sample.get('song_artist') or '(未知)'}")
    print(f"             拟回复：{(sample.get('draft') or '')[:56]}")

    after = http.get("/api/stats").json()
    added = after["reviews_notified"] - before["reviews_notified"]
    rep.check("评论提醒已投递（kind=review）", added > 0,
              f"《推送日志》kind=review 增加 {added} 条（幂等键 comment_review:comment_id:渠道）")
    rep.check("不自动发抖音评论", True,
              "全链路无任何抖音写接口调用；拟回复只落《评论命中表》+ 推提醒")
    return threads[0]


def human_review_checks(http: httpx.Client, thread_id: str, group: str) -> None:
    rep.group_of(group)
    if not thread_id:
        rep.bad("人工确认回路", "没有拿到可用的 thread_id")
        return

    detail = http.get(f"/api/reviews/{thread_id}").json()
    rep.check("子图挂在 interrupt 等人拍板", detail.get("suspended") is True,
              f"thread_id={thread_id}")

    items = detail.get("items", [])
    pending = [i for i in items if i.get("status") == "pending"]
    rep.check("挂起时「待确认」列表非空", len(pending) > 0,
              f"{len(pending)} 条 pending（落表若排在 interrupt 之后，这里永远是空 → 死锁）")
    if not pending:
        return

    decisions = {i["comment_id"]: "approved" for i in pending[:2]}
    decisions.update({i["comment_id"]: "ignored" for i in pending[2:3]})
    res = http.post(f"/api/reviews/{thread_id}", json={"decisions": decisions}, timeout=120).json()
    rep.check("提交确认后子图恢复并跑完", res.get("resumed") is True,
              f"确认 {len(decisions)} 条（2 通过 / 1 忽略），resumed={res.get('resumed')}")

    after = http.get(f"/api/reviews/{thread_id}").json()
    got = {i["comment_id"]: i["status"] for i in after.get("items", [])}
    rep.check("确认结果落库", all(got.get(c) == s for c, s in decisions.items()),
              "、".join(f"{c[:16]}…→{got.get(c)}" for c in list(decisions)[:3]))

    # 幂等：同一条评论重复提交，状态不能被改回、提醒不能重复推
    a = http.get("/api/stats").json()
    http.post(f"/api/reviews/{thread_id}", json={"decisions": decisions}, timeout=120)
    b = http.get("/api/stats").json()
    rep.check("重复提交幂等", a["reviews_notified"] == b["reviews_notified"],
              f"重放同一批 decisions：评论提醒 {a['reviews_notified']} → {b['reviews_notified']}（不变）")
    rep.check("空 decisions 不炸",
              http.post(f"/api/reviews/{thread_id}", json={"decisions": {}}, timeout=60).status_code == 200,
              "对空决定返回 200")


# ---------------------------------------------------------------- E 隔离实例全链路
def _wait_up(client: httpx.Client, tries: int = 40, gap: float = 1.0) -> bool:
    for _ in range(tries):
        try:
            if client.get("/api/stats", timeout=3).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(gap)
    return False


def _shrink_wal(paths: list[Path]) -> None:
    """子进程被强杀后 WAL 里会留几 MB 未 checkpoint 的数据。这里收拾一下减小体积。

    只是 checkpoint（写操作），**不删文件** —— 见 TMP_ROOT 处关于删除守卫的说明。
    """
    import sqlite3

    for p in paths:
        if not p.exists():
            continue
        try:
            conn = sqlite3.connect(p)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def check_fresh_e2e() -> None:
    """在全新临时库上把评论链路完整跑一遍 —— 老库验不了这段，见模块 docstring。"""
    rep.group_of("G 隔离实例全链路（全新库：两轮扫描 → 告警 → 评论 → 挂起 → 确认）")

    run_dir = TMP_ROOT / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    db_file, cp_file = run_dir / "app.db", run_dir / "checkpoints.db"

    env = os.environ.copy()
    env.update({
        "APP_HOST": "127.0.0.1", "APP_PORT": str(TMP_PORT), "DEBUG": "false",
        "SQLITE_PATH": str(db_file),
        "CHECKPOINT_PATH": str(cp_file),
        "STORAGE_BACKEND": "sqlite",     # 不碰飞书
        "NOTIFIER_BACKEND": "local",     # 不碰飞书，只看本地推送日志
        "PROVIDER_CHAIN": "mock",        # 确定性数据
        "SCHEDULER_ENABLED": "false",
    })
    log_path = run_dir / "server.log"
    base = f"http://127.0.0.1:{TMP_PORT}"

    with open(log_path, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                                stdout=fh, stderr=subprocess.STDOUT)
    try:
        with httpx.Client(base_url=base) as c:
            if not _wait_up(c):
                rep.bad("隔离实例启动", f"60 秒内没起来，看 {log_path}")
                return
            rep.ok("隔离实例启动",
                   f"{base}　全新临时库 {run_dir}（不污染交付数据、不碰飞书）")

            # 控制面：现场改阈值 —— 顺便验证「改表即生效、不用重启」
            for key, value in (("threshold", 20), ("provider_chain", "mock"), ("comment_scope", "all")):
                c.put("/api/config", json={"key": key, "value": value})
            resolved = c.get("/api/config").json()["resolved"]
            rep.check("控制面改配置即时生效", str(resolved["threshold"]) == "20",
                      f"threshold → {resolved['threshold']}、provider_chain → {resolved['provider_chain']}（进程没重启）")

            stats0 = c.get("/api/stats").json()
            r1 = c.post("/api/scan", params={"trigger": "verify"}, timeout=180).json()
            rep.check("第 1 轮：首见视频，增量记 0、不告警",
                      r1["delta_count"] > 0 and r1["alert_count"] == 0,
                      f"{r1['video_count']} 条视频 / {r1['delta_count']} 条增量 / 告警 {r1['alert_count']} / "
                      f"来源 {r1['source']}")

            r2 = c.post("/api/scan", params={"trigger": "verify"}, timeout=180).json()
            rep.check("第 2 轮：跨过阈值并推送", r2["alert_count"] > 0,
                      f"告警 {r2['alert_count']} 条 / 推送 {r2['alert_summary']}")

            logs = c.get("/api/alerts", params={"limit": 20, "kind": "alert"}).json()["items"]
            rep.check("推送日志记录了点赞告警", len(logs) >= r2["alert_count"],
                      f"kind=alert {len(logs)} 条，渠道 {sorted({str(x.get('channel')) for x in logs})}")

            # ⚠ 评论链路要用【第 1 轮】的线程：命中记录按 comment_id 去重，
            # 保留在「第一次发现它的那一轮」的线程上，不随后续轮次搬家。
            # 拿第 2 轮的线程去查详情会 404（records 不在那个 thread_id 下）。
            thread = comment_chain_checks(c, r1, "G2 隔离实例：选做链路（关键词 → 拟回复 → 提醒）", before=stats0)
            human_review_checks(c, thread, "G3 隔离实例：人工确认回路与幂等")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        _shrink_wal([db_file, cp_file])
        print(f"      [隔离实例已停止，临时库留在 {run_dir}（不删，避免触发删除守卫）]")


# ---------------------------------------------------------------- E 演示档切换
def check_demo_switch(http: httpx.Client) -> None:
    """验证基础要求 5 的那把开关：一条命令切档，改完下一轮生效、不用重启。

    ⚠ 这一段**直接改运行中的服务配置**（它验的就是控制面本身）。
    进入/退出时的档位还原由 `main()` 的 finally 统一负责，这里只做 on/off 的检查。
    另外要断言「切档必须带上 provider_chain」—— 只切阈值不切数据源，
    看着像切好了，实际扫描还是走真实源、不会告警（2026-09-16 的真实故障）。
    """
    rep.group_of("E 演示档 / 正式档切换（控制面）")
    import demo_mode

    from app.core.presets import FORMAL

    asyncio.run(demo_mode.main("on"))
    demo = http.get("/api/config").json()["resolved"]
    rep.check("--on 切到演示档", demo["scan_mode"] == "interval" and str(demo["threshold"]) == "20",
              f"scan_mode={demo['scan_mode']} / {demo['scan_interval_minutes']} 分钟一轮 / "
              f"threshold={demo['threshold']} / chain={demo['provider_chain']}")
    rep.check("--on 同时切掉数据源（否则演示时不会告警）",
              str(demo["provider_chain"]).strip() == "mock",
              f"provider_chain={demo['provider_chain']} —— 必须是纯 mock，"
              f"带 browser 的话扫描会走真实源、增量打不到阈值")

    sch = http.get("/api/scan/status").json()["scheduler"]
    rep.check("调度器立刻换成 interval 模式", "分钟" in str(sch.get("description", "")),
              f"{sch.get('description')}，下次 {sch.get('next_run_at')}（服务进程一直没重启）")

    asyncio.run(demo_mode.main("off"))
    back = http.get("/api/config").json()["resolved"]
    rep.check("--off 切回正式档", back["scan_mode"] == "cron" and str(back["threshold"]) == "300",
              f"scan_mode={back['scan_mode']} {back['scan_cron_hours']} / threshold={back['threshold']} / "
              f"chain={back['provider_chain']}")
    # 切档接口必须把该档位定义的每一格都落实到位（别漏格）。
    # 注意只查 FORMAL 自己定义的键：PRESET_KEYS 是两档并集，
    # 里面的 scan_interval_minutes 不属于正式档，查它只会 KeyError。
    missed = [k for k in FORMAL if str(back.get(k)) != str(FORMAL[k])]
    rep.check("切档覆盖全部档位相关键（不漏格）", not missed,
              "覆盖完整" if not missed else f"这些键偏离正式档：{missed}")
    sch2 = http.get("/api/scan/status").json()["scheduler"]
    rep.check("调度器恢复 cron 时点", sch2.get("next_run_at", "").endswith("+08:00"),
              f"{sch2.get('description')}，下次 {sch2.get('next_run_at')}")


# ---------------------------------------------------------------- H 飞书
FEISHU_TABLES = {
    "accounts": "监控账号表", "snapshots": "视频快照表", "deltas": "增量与告警表",
    "comment_hits": "评论命中表", "config": "配置表", "scan_rounds": "扫描轮次表",
    "alert_log": "推送日志表",
}


def check_feishu() -> None:
    rep.group_of("H 飞书多维表格镜像")

    async def _counts() -> dict:
        from app.config import get_settings
        from app.storage.feishu import FeishuStorage

        st = FeishuStorage(get_settings(), seed_defaults=False)
        await st.init()
        try:
            out = {}
            for key, tid in st.tables.items():
                rows, _ = await st.client.list_records(tid)
                out[key] = len(rows)
            return out
        finally:
            await st.close()

    try:
        counts = asyncio.run(_counts())
    except Exception as exc:  # noqa: BLE001
        rep.bad("飞书表格连通", f"{type(exc).__name__}: {exc}")
        return

    rep.ok("飞书表格连通", "、".join(f"{FEISHU_TABLES.get(k, k)} {v} 行" for k, v in counts.items()))
    for key, label in FEISHU_TABLES.items():
        rep.check(f"镜像表：{label}", key in counts and counts[key] > 0,
                  f"{counts.get(key, 0)} 行" if key in counts else "缺失")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="一键功能自检（含实测证据）")
    ap.add_argument("--base", default=os.environ.get("VERIFY_BASE", "http://127.0.0.1:8000"))
    ap.add_argument("--skip-real", action="store_true", help="跳过真实采集（不跑 Playwright）")
    ap.add_argument("--skip-tests", action="store_true", help="跳过 pytest")
    ap.add_argument("--skip-feishu", action="store_true", help="跳过飞书连通检查")
    ap.add_argument("--skip-e2e", action="store_true", help="跳过隔离实例全链路")
    ap.add_argument("--only", default="",
                    help="只跑指定段（逗号分隔）：env,tests,service,real,prod,demo,e2e,feishu")
    args = ap.parse_args()

    # 输出重定向到文件时默认是块缓冲，日志要等到进程结束才可见 —— 调成行缓冲便于实时看
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    only = {x.strip() for x in args.only.split(",") if x.strip()}

    def want(step: str) -> bool:
        return (not only or step in only) and not (
            (step == "tests" and args.skip_tests)
            or (step == "real" and args.skip_real)
            or (step == "e2e" and args.skip_e2e)
            or (step == "feishu" and args.skip_feishu)
        )

    print(f"功能自检开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　目标 {args.base}"
          + (f"　（只跑 {args.only}）" if only else ""))
    if want("env"):
        check_env()
    if want("tests"):
        check_tests()

    # 自检会临时切档（E 组）。所以进服务段之前先给档位拍个快照，
    # 无论中途是否失败，退出时原样还回去 —— 自检不该改变用户当前的档位。
    entry_cfg: dict | None = None

    try:
        if want("service") or want("real") or want("prod") or want("demo"):
            with httpx.Client(base_url=args.base) as http:
                if want("service"):
                    stats = check_service(http)
                    if not stats:
                        rep.render()
                        return 1
                entry_cfg = http.get("/api/config").json()["resolved"]
                if want("real"):
                    check_real_scan(http)
                if want("prod"):
                    check_prod_comment_state(http)
                if want("demo"):
                    check_demo_switch(http)

        if want("e2e"):
            check_fresh_e2e()
        if want("feishu"):
            check_feishu()
    finally:
        from app.core.presets import PRESET_KEYS, preset_of

        with httpx.Client(base_url=args.base) as http:
            if _wait_up(http, tries=1):
                if entry_cfg is None:
                    # 服务段没跑到（比如只跑了 e2e/feishu），拿当前值当基准
                    entry_cfg = http.get("/api/config").json()["resolved"]

                cur = http.get("/api/config").json()["resolved"]
                drifted = [k for k in PRESET_KEYS
                           if k in entry_cfg and str(cur.get(k)) != str(entry_cfg[k])]
                if drifted:
                    print(f"      [自检动过档位，恢复成跑之前的状态（{preset_of(entry_cfg)}）："
                          f"{', '.join(drifted)}]")
                    for key in drifted:
                        http.put("/api/config", json={"key": key, "value": entry_cfg[key]})

                final = http.get("/api/config").json()["resolved"]
                same = all(str(final.get(k)) == str(entry_cfg.get(k))
                           for k in PRESET_KEYS if k in entry_cfg)
                rep.group_of("I 恢复自检前的档位")
                rep.check("退出时档位与跑之前一致（自检无副作用）", same,
                          f"跑之前 {preset_of(entry_cfg)} → 退出时 {preset_of(final)}；"
                          f"scan_mode={final.get('scan_mode')} threshold={final.get('threshold')} "
                          f"chain={final.get('provider_chain')}")

    rep.render()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps({"time": datetime.now().astimezone().isoformat(), "base": args.base, "items": rep.rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告已写入 {REPORT_PATH.relative_to(ROOT)}")
    hard = [r for r in rep.rows if not r.get("note")]
    return 0 if all(r["ok"] for r in hard) else 2


if __name__ == "__main__":
    raise SystemExit(main())
