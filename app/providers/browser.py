"""浏览器自动化 provider（P1，已实现）。

用 Playwright 驱动**已登录的 Chrome 用户目录**读取公开页面数据。

核心思路（绕开签名逆向）：
不去硬调抖音的开放接口（X-Bogus / a_bogus 签名纯逆向的坑），而是让真实
Chrome 打开页面，**监听页面自己发出的 XHR 响应**，从响应体里抽数据——
签名由浏览器自己算，我们只做"读"。

⚠ 合规红线（写在代码里而不是只写在文档里）：
- 只读公开数据
- **不自动点赞、不自动评论、不自动关注**
- 单账号内必须串行 + 账号间停顿（browser_account_delay）

并发模型：
- 整个 provider 共享**一个**持久化 Chrome 上下文 + 一把 asyncio.Lock。
  原因有二：① Chrome 同一用户目录不允许并行启动两个实例；
  ② 评论子图是 fire-and-forget 的，多个子图会并发调 fetch_comments，
  必须串行化，否则会在同一个 Chrome 上打架。
- 上下文懒启动、进程退出时统一关闭（见 main.py lifespan → deps 关停）。

未配置用户目录时抛 ProviderError，降级链自动跳到下一档（mock）。

⚠ 采集前必查登录态（`check_login` / `LOGIN_COOKIE_NAMES`）：登录态掉了之后
抖音对未登录访客仍会回一份「作品列表」，只是**不含最新作品**（实测最新一条
停在 9 天前）。按回看窗口过滤后正好 0 条，看起来和「账号这几天没发作品」
一模一样，整轮会被静默跳过——2026-09-16 的真事。所以这里把「未登录」
当成明确故障上报，而不是当成空数据。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.core.video_id import is_real_video_id
from app.providers.base import ProviderError, ProviderNotConfigured

log = logging.getLogger(__name__)

# 页面会自己请求的接口（我们只监听，不主动调）：
AWEME_POST_API = "/aweme/v1/web/aweme/post/"
COMMENT_LIST_API = "/aweme/v1/web/comment/list/"
USER_PAGE = "https://www.douyin.com/user/{sec_uid}"
VIDEO_PAGE = "https://www.douyin.com/video/{video_id}"

_SECONDS_PER_DAY = 86400

# 抖音网页版的标准 sec_uid 形态（以 MS4wLjABAAAA 开头的 base64 变体串）
SEC_UID_RE = re.compile(r"MS4wLjABAAAA[\w-]+")

# 登录凭据 cookie：只有**登录后**抖音才下发 sessionid。
# ⚠ 别拿 ttwid / odin_tt / UIFID 判登录——匿名访客也有这些，
#   2026-09-16 就是因为只看「有没有抓到 aweme_list」而踩了坑：
#   登录态掉了之后抖音照样对访客回一份**不含最新作品**的作品列表
#   （实测最新一条停在 9 天前，账号真实的新作品一条不给），
#   按回看窗口过滤后正好 0 条，于是被误读成「账号这几天没发新作品」，
#   整轮静默跳过，快照/增量/告警三张表一行都不写。
LOGIN_COOKIE_NAMES = ("sessionid", "sessionid_ss")

# 未登录时的统一话术：日志、轮次台账、API 响应都用它，避免各说各话
LOGIN_EXPIRED_HINT = (
    "browser 登录态已过期（profile 里没有 sessionid）：抖音对未登录访客只回"
    "不含最新作品的历史列表，采集结果不可信。重跑 "
    ".venv/Scripts/python.exe tools/login_douyin.py 重新扫码登录，再手动扫描一次"
)


def has_login_cookie(cookies: list[dict] | None) -> bool:
    """cookie 列表里有没有登录凭据。

    独立成纯函数是为了能单测（`tests/test_browser_login.py`），
    也为了让「判据只有一条」这件事一眼可见——判据分散就又会各说各话。
    """
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        if cookie.get("name") in LOGIN_COOKIE_NAMES and (cookie.get("value") or ""):
            return True
    return False


# ================================================================ 纯函数解析
# 独立于 Playwright，单独可单测（tests/test_browser_parse.py）。
# 结构对齐抖音 web 端 XHR 响应的 aweme_list / comment_list 载荷。


def parse_aweme(item: dict, account_name: str) -> dict | None:
    """单条视频 → 项目约定的 VIDEO_FIELDS + music 元信息。

    解析不了的条目返回 None（而不是抛异常）：一条脏数据不该毁掉整轮采集。
    """
    try:
        video_id = str(item["aweme_id"])
        stats = item.get("statistics") or {}
        create_time = int(item.get("create_time") or 0)
        if not video_id or not create_time:
            return None

        music = item.get("music") or {}
        author = (item.get("author") or {}).get("nickname") or account_name
        return {
            "video_id": video_id,
            "account": author,
            "title": str(item.get("desc") or "").strip()[:120],
            "publish_time": datetime.fromtimestamp(
                create_time, tz=timezone.utc
            ).isoformat(timespec="seconds"),
            "likes": int(stats.get("digg_count") or 0),
            "comments": int(stats.get("comment_count") or 0),
            "shares": int(stats.get("share_count") or 0),
            "music": {
                "title": str(music.get("title") or "").strip(),
                "artist": str(music.get("author") or "").strip(),
            },
            "source": "browser",
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_comment(item: dict) -> dict | None:
    """单条评论 → comment_id / content / comment_time。"""
    try:
        comment_id = str(item["cid"])
        content = str(item.get("text") or "").strip()
        if not comment_id or not content:
            return None
        create_time = int(item.get("create_time") or 0)
        return {
            "comment_id": comment_id,
            "content": content[:500],
            "comment_time": (
                datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat(timespec="seconds")
                if create_time
                else ""
            ),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_aweme_list(payload: dict, account_name: str, days: int) -> tuple[list[dict], bool]:
    """一个 post 接口响应 → (视频列表, 是否已越过回看窗口)。

    返回的第二个值为 True 表示本批里出现了比回看窗口更老的视频，
    调用方可以停止滚动了。
    """
    items = payload.get("aweme_list") or []
    cutoff = datetime.now(timezone.utc).timestamp() - days * _SECONDS_PER_DAY
    out: list[dict] = []
    passed = False
    for item in items:
        video = parse_aweme(item, account_name)
        if video is None:
            continue
        if float(item.get("create_time") or 0) < cutoff:
            passed = True
            continue  # 窗口外的旧视频不进结果
        out.append(video)
    return out, passed


# ================================================================ provider


class _XhrSniffer:
    """挂在 page 上，把命中目标接口的 JSON 响应攒进一个列表。"""

    def __init__(self, api_keyword: str) -> None:
        self.api_keyword = api_keyword
        self.captured: list[dict] = []

    async def __call__(self, response) -> None:  # pragma: no cover - 依赖真实网络
        try:
            if self.api_keyword not in response.url:
                return
            if "json" not in (response.headers.get("content-type") or ""):
                return
            payload = await response.json()
            if isinstance(payload, dict):
                self.captured.append(payload)
        except Exception:  # noqa: BLE001 - 网络抖动、非 JSON，都只跳过这一条
            pass


class BrowserProvider:
    name = "browser"
    synthetic = False  # 真实数据源：成功返回空 = 真的没数据，不允许 mock 兜底

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.user_data_dir = getattr(settings, "browser_user_data_dir", "") or ""
        self.headless = bool(getattr(settings, "browser_headless", True))
        self.channel = getattr(settings, "browser_channel", "chrome") or "chrome"
        self.account_delay = float(getattr(settings, "browser_account_delay", 8.0) or 8.0)
        self.max_scrolls = int(getattr(settings, "browser_max_scrolls", 10) or 10)
        # 同一用户目录只能有一个 Chrome 实例；评论子图并发也靠它串行化
        self._lock = asyncio.Lock()
        self._pw = None
        self._ctx = None
        # 上一轮采集的账号级结论，供上层写进《扫描轮次表》的备注。
        # `account_notes` 是「看得见的提示」，`account_failures` 是「真异常计数」。
        # ⚠ 之所以敢用实例属性而不是返回值：采集是串行的
        #   （APScheduler max_instances=1 + ScanBusy 一把锁），见 accounts.py 的同款说明。
        self.account_notes: list[str] = []
        self.account_failures: int = 0

    # ------------------------------------------------------------ 生命周期

    def _configured(self) -> bool:
        return bool(self.user_data_dir)

    def _require(self):
        if not self._configured():
            raise ProviderNotConfigured(
                "browser 未配置（.env 里 BROWSER_USER_DATA_DIR 为空）。"
                "首次接入：填好目录 → BROWSER_HEADLESS=false 启动 → 弹窗里扫码登录抖音 → 改回 true"
            )
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError as exc:
            raise ProviderNotConfigured(
                "browser 需要 playwright：.venv\\Scripts\\pip install playwright"
            ) from exc

    async def _ensure_context(self):
        """懒启动共享的持久化 Chrome 上下文（调用方必须已持有 self._lock）。"""
        if self._ctx is not None:
            return self._ctx
        from playwright.async_api import async_playwright

        log.info(
            "启动 Chrome（channel=%s, headless=%s, 目录=%s）",
            self.channel, self.headless, self.user_data_dir,
        )
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            channel=self.channel,
            headless=self.headless,
            # UA 不改——伪装 UA 反而更容易对不上浏览器指纹
            viewport={"width": 1280, "height": 800},
        )
        return self._ctx

    async def aclose(self) -> None:
        """进程退出时调用（main.py lifespan 里接线）。幂等。"""
        async with self._lock:
            if self._ctx is not None:
                try:
                    await self._ctx.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ctx = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._pw = None

    # ------------------------------------------------------------ 登录态体检

    async def check_login(self) -> bool:
        """当前 profile 里还有没有抖音登录凭据。

        单独暴露出来给 `tools/check_douyin_login.py` 用：演示前 10 秒体检一次，
        比等一整轮扫描跑完再猜「为什么 0 条」便宜得多。未配置时抛 ProviderError
        （调用方自己决定要不要吞），这样「没配」和「配了但掉线」不会混为一谈。
        """
        self._require()
        async with self._lock:
            ctx = await self._ensure_context()
            return has_login_cookie(await ctx.cookies())

    # ------------------------------------------------------------ 采集

    async def fetch_videos(self, accounts: list[dict], days: int) -> list[dict]:
        self._require()
        from playwright.async_api import Error as PwError

        videos: list[dict] = []
        self.account_notes = []
        self.account_failures = 0
        # 逐账号的结论按账号归类，**不能只留一个全局布尔**：
        # `saw_api = saw_api or saw` 会让「新加的账号一条都没抓到」被
        # 「老账号接口正常」掩盖成「本轮窗口内没有新作品」——
        # 2026-09-16 就是这么把一个失败账号伪装成一次正常空扫描的。
        saw_api_accounts: list[str] = []
        dead_accounts: list[str] = []

        async with self._lock:
            ctx = await self._ensure_context()
            # 采集前先确认登录态。未登录不是「没数据」，是「数据不可信」：
            # 抖音给访客的那份列表不含最新作品，照它算增量会得出
            # 「账号这几天没发作品」的错误结论。这里直接失败，让上层把原因
            # 写进轮次台账，而不是安静地返回空。
            if not has_login_cookie(await ctx.cookies()):
                log.error("%s", LOGIN_EXPIRED_HINT)
                raise ProviderError(LOGIN_EXPIRED_HINT)

            page = await ctx.new_page()
            try:
                for i, acc in enumerate(accounts):
                    if i:  # 账号间停顿（风控），第一个不停
                        await asyncio.sleep(self.account_delay)
                    got, saw = await self._collect_one_account(page, acc, days)
                    name = acc.get("name") or acc.get("sec_uid") or "?"
                    videos.extend(got)
                    if got:
                        continue
                    if saw:
                        # 接口正常回过列表，但窗口内确实没有作品 —— 合法空结果
                        saw_api_accounts.append(name)
                    else:
                        dead_accounts.append(name)
            except PwError as exc:
                raise ProviderError(f"browser 采集失败：{exc}") from exc
            finally:
                await page.close()

        for name in saw_api_accounts:
            self.account_notes.append(f"账号「{name}」回看 {days} 天内没有新作品（接口正常）")
        for name in dead_accounts:
            self.account_notes.append(
                f"账号「{name}」一条作品都没抓到（sec_uid 可能失效或被风控，"
                "可跑 tools/resolve_sec_uid.py --write 把短链解析成标准 sec_uid）"
            )
            self.account_failures += 1

        if not videos:
            if saw_api_accounts:
                log.warning(
                    "账号在回看窗口（%d 天）内没有新作品——接口正常，按空结果返回，不降级到模拟数据",
                    days,
                )
                return []
            # 所有账号都没抓到接口：登录态检查已经过了，那就是 sec_uid 无效 /
            # 全部被风控 —— 这是故障，不能伪装成「没有新作品」。
            raise ProviderError(
                f"browser 对 {len(dead_accounts)} 个账号都没抓到作品列表："
                f"{', '.join(dead_accounts)}（sec_uid 是否有效？是否被风控？去浏览器里人工看一眼）"
            )
        return videos

    async def resolve_sec_uid(self, account: dict) -> str | None:
        """把非标准 sec_uid（短链 / 纯数字 ID）解析成标准 MS4wLjABAAAA… 形态。

        单独暴露出来给 `tools/resolve_sec_uid.py` 用：**解析一次写回账号表**，
        省得每一轮扫描都白等这几秒重定向、日志一直刷「非标准形态」的告警。
        解析不出来返回 None（调用方保留原值，采集链路仍会按老路兜底）。
        """
        self._require()
        sec_uid = (account.get("sec_uid") or "").strip()
        if SEC_UID_RE.fullmatch(sec_uid):
            return sec_uid
        homepage = (account.get("homepage") or "").strip()
        if not (homepage.startswith("http") and "douyin.com" in homepage):
            return None
        async with self._lock:
            ctx = await self._ensure_context()
            page = await ctx.new_page()
            try:
                await page.goto(homepage, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2_000)
                m = SEC_UID_RE.search(page.url)
                return m.group() if m else None
            except Exception as exc:  # noqa: BLE001 - 解析失败不是致命错误
                log.warning("解析 sec_uid 失败（%s）：%s", account.get("name"), exc)
                return None
            finally:
                await page.close()

    async def _resolve_user_url(self, page, account: dict) -> str:
        """把账号解析成标准主页 URL。

        sec_uid 有三种形态，只有第一种网页版原生认识：
        ① MS4wLjABAAAA…（标准 sec_uid）→ 直接拼 URL
        ② v.douyin.com 短链 / 任何 douyin.com/user/ 链接（homepage 字段）→
           打开它跟重定向，从最终地址里抠出 sec_uid
        ③ 纯数字短 ID（App 里复制的）→ 网页接口不认（实测 status_code=2），
           若有 homepage 走 ②，否则按 ③ 兜底（大概率 0 条，日志会提示）
        """
        sec_uid = account.get("sec_uid", "")
        if SEC_UID_RE.fullmatch(sec_uid):
            return USER_PAGE.format(sec_uid=sec_uid)

        homepage = (account.get("homepage") or "").strip()
        if homepage.startswith("http") and "douyin.com" in homepage:
            log.info(
                "账号 %s 的 sec_uid 不是标准形态（%s…），通过主页链接解析",
                account.get("name"), sec_uid[:12],
            )
            try:
                await page.goto(homepage, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2_000)
                m = SEC_UID_RE.search(page.url)
                if m:
                    log.info("解析出真实 sec_uid：%s…", m.group()[:20])
                    return USER_PAGE.format(sec_uid=m.group())
            except Exception as exc:  # noqa: BLE001 - 解析失败退回原始 sec_uid 试一次
                log.warning("主页链接解析失败（%s），退回 sec_uid 直连", exc)

        if not SEC_UID_RE.fullmatch(sec_uid):
            log.warning(
                "账号 %s 的 sec_uid=%r 不是网页版标准形态，若 0 条请改用主页短链（v.douyin.com/xxx）",
                account.get("name"), sec_uid[:20],
            )
        return USER_PAGE.format(sec_uid=sec_uid)

    async def _collect_one_account(self, page, account: dict, days: int) -> tuple[list[dict], bool]:
        """返回 (窗口内的视频, 接口是否正常回过 aweme_list)。

        第二个值**只代表这一个账号**，由 `fetch_videos` 按账号归类：
          saw=True 且 0 条 → 接口正常、窗口内没新作品（合法空结果）
          saw=False 且 0 条 → 这个账号是故障（sec_uid 失效 / 被风控）
        ⚠ 不要把多个账号的结果 `or` 成一个全局布尔，那正是
        「新加的账号一条没抓到、却报成『窗口内没有新作品』」的由来。
        """
        sec_uid = account.get("sec_uid", "")
        name = account.get("name") or sec_uid
        url = await self._resolve_user_url(page, account)

        sniffer = _XhrSniffer(AWEME_POST_API)
        page.on("response", sniffer)

        log.info("采集账号 %s …", name)
        saw_api = False
        try:
            if page.url != url:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3_000)  # 等首批 XHR 自然发生

            collected: list[dict] = []
            seen_ids: set[str] = set()
            for _ in range(self.max_scrolls):
                for payload in sniffer.captured:
                    if "aweme_list" in payload:
                        saw_api = True
                    batch, _ = parse_aweme_list(payload, name, days)
                    for v in batch:
                        if v["video_id"] not in seen_ids:
                            seen_ids.add(v["video_id"])
                            collected.append(v)
                if _author_exhausted(sniffer):
                    break
                sniffer.captured.clear()
                await page.mouse.wheel(0, 4_000)
                await page.wait_for_timeout(2_000)  # 给懒加载留时间

            # 滚动结束后再收一次尾批
            for payload in sniffer.captured:
                if "aweme_list" in payload:
                    saw_api = True
                batch, _ = parse_aweme_list(payload, name, days)
                for v in batch:
                    if v["video_id"] not in seen_ids:
                        seen_ids.add(v["video_id"])
                        collected.append(v)

            if not collected:
                if saw_api:
                    log.info("账号 %s 回看窗口内没有新作品（接口正常）", name)
                else:
                    log.warning(
                        "账号 %s 没抓到任何视频：sec_uid 可能无效，或页面被风控拦了（去浏览器里人工看一眼）",
                        name,
                    )
            return collected, saw_api
        finally:
            page.remove_listener("response", sniffer)

    async def fetch_comments(self, video_id: str, limit: int = 20) -> list[dict]:
        self._require()
        from playwright.async_api import Error as PwError

        # 非真实 aweme_id（mock 的 acc01_v01）根本没这个视频页 —— 抛异常表示
        # 「我干不了这活」，上层才会考虑换别的源。
        if not is_real_video_id(video_id):
            raise ProviderError(f"browser 无法采集评论：{video_id!r} 不是真实视频 ID")

        async with self._lock:
            ctx = await self._ensure_context()
            page = await ctx.new_page()
            sniffer = _XhrSniffer(COMMENT_LIST_API)
            page.on("response", sniffer)
            try:
                await page.goto(
                    VIDEO_PAGE.format(video_id=video_id),
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                # 滚两屏把评论区加载出来（通常首屏就有 20 条）
                for _ in range(2):
                    await page.mouse.wheel(0, 2_000)
                    await page.wait_for_timeout(2_000)

                comments: list[dict] = []
                seen: set[str] = set()
                for payload in sniffer.captured:
                    for item in payload.get("comments") or []:
                        c = parse_comment(item)
                        if c and c["comment_id"] not in seen:
                            seen.add(c["comment_id"])
                            comments.append(c)
                comments.sort(key=lambda c: c["comment_time"], reverse=True)
                # ⚠ 抓不到就是**真的没有评论**（小账号的视频常常 0~3 条），
                # 返回空列表而不是抛异常：空结果意味着「这个视频没人评论」，
                # 上层据此收工，不允许再用 mock 补一批假评论上来。
                log.info("视频 %s 抓到 %d 条评论", video_id, len(comments))
                return comments[:limit]
            except PwError as exc:
                raise ProviderError(f"browser 评论采集失败：{exc}") from exc
            finally:
                page.remove_listener("response", sniffer)
                await page.close()


def _author_exhausted(sniffer: _XhrSniffer) -> bool:
    """已抓到的响应里是否出现 has_more=0（作者视频翻完了）。"""
    return any(payload.get("has_more") == 0 for payload in sniffer.captured)
