"""抖音登录态体检（无头、约 10 秒）。

用法：
    .venv/Scripts/python.exe tools/check_douyin_login.py

为什么要单独一个工具：登录态掉了之后，扫描本身**不会报错**——
抖音对未登录访客照样回一份作品列表，只是不含最新作品；按回看窗口过滤后
正好 0 条，看起来就是「账号这几天没发作品」。于是整轮被跳过，
《视频快照表》《点赞增量表》《告警表》一行都不写，人只能盯着看板发呆。
（2026-09-16 的真事，详见 `app/providers/browser.py` 的模块说明。）

所以在「加了账号要验证」「演示前彩排」这两个时刻，先跑这个工具：
10 秒给出结论，比等一整轮扫描（三个账号 ~110 秒）再猜便宜得多。

**两条路径，先问服务**（2026-09-16 补）：
① 服务在跑 → 打 `GET /api/login_status`，让服务用**它自己的**浏览器读 cookie。
   必须这样：profile 被服务的**热上下文**占着（`_ensure_context` 有意缓存），
   这里再拉一个 Edge 必然失败（实测 TargetClosedError），而「演示前体检」
   恰恰就是服务开着的时候。
② 服务没跑 → 自己无头拉一个 Edge 读 cookie（此时 profile 是空的，没问题）。

退出码：0 = 登录态正常；1 = 登录态已过期；2 = 判断不了（没配好 / 扫描占着 / 拉不起浏览器）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.providers.browser import LOGIN_EXPIRED_HINT  # noqa: E402


async def ask_service(settings) -> dict | None:
    """服务在跑就问它一次；没在跑（或答非所问）返回 None，交给本地路径。

    `trust_env=False` 是必须的：本机有 `http_proxy`，httpx 默认会读它，
    于是访问 127.0.0.1 也会被塞给代理 —— 和 curl 必须加 `--noproxy '*'` 是同一件事。
    """
    import httpx

    url = f"http://{settings.app_host}:{settings.app_port}/api/login_status"
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(url)
    except Exception:  # noqa: BLE001 - 服务没跑是常态，不是错误
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - 不是我们的接口（端口被别的程序占了）
        return None


def report(payload: dict, via_service: bool) -> int:
    """把 `/api/login_status` 的答复翻译成人和 CI 都能用的话 + 退出码。"""
    logged_in = payload.get("logged_in")
    detail = payload.get("detail") or ""
    where = "经服务内的浏览器" if via_service else "本地无头 Edge"
    print(f"  结论来自：{where}")

    if logged_in is True:
        print("✓ 登录态正常（profile 里有 sessionid）")
        print("  下一步：手动扫描，数据会正常入库")
        return 0
    if logged_in is False:
        print("✗ 登录态已过期：profile 里没有 sessionid，只有匿名设备 cookie。")
        print("  这时抖音只会给访客一份**不含最新作品**的列表，扫描结果不可信")
        print("  （表现：轮次台账写「跳过：回看 3 天内没有新作品」，三张表都不动）。")
        print(f"  修复：{LOGIN_EXPIRED_HINT}")
        return 1
    print(f"? 这次判断不了登录态：{detail}")
    if payload.get("configured") is False:
        print("  （browser 数据源本来就没接——这是预期内的降级，不是故障）")
    return 2


async def main() -> int:
    settings = get_settings()

    print(f"→ 体检抖音登录态（用户目录：{settings.browser_user_data_dir or '(未配置)'}）")

    served = await ask_service(settings)
    if served is not None:
        # 服务在跑：别去抢 profile，直接用它的结论
        print("  检测到服务在跑 → 请问服务（不再单独拉浏览器，避免抢 profile）")
        return report(served, via_service=True)

    # 服务没跑，才自己拉浏览器
    print("  服务没在跑 → 无头启动 Edge 读一次 cookie，10 秒内出结果……")

    if not (settings.browser_user_data_dir or ""):
        print("✗ .env 里 BROWSER_USER_DATA_DIR 为空——browser 数据源没接，先配好它")
        return 2

    from app.providers.base import ProviderNotConfigured
    from app.providers.browser import BrowserProvider

    provider = BrowserProvider(settings)
    try:
        ok = await provider.check_login()
    except ProviderNotConfigured as exc:
        print(f"✗ 环境没配好：{exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - 启动失败也要给出人能照做的结论
        # Playwright 的异常 __str__ 带几十行 `<launching> …` 命令行 + call log，
        # 整个打出来会把下面两句「该怎么做」冲没了 —— 只留第一行。
        first_line = (str(exc) or "").splitlines()[0] if str(exc) else exc.__class__.__name__
        print(f"✗ 拉起浏览器失败：{first_line}")
        print("  常见原因：profile 被别的进程占着（跑着的服务、或遗留的 Edge 进程）。")
        print("  先停服务；若已停，杀掉遗留进程：")
        print('    Get-CimInstance Win32_Process -Filter "Name=\'msedge.exe\'" |')
        print("      Where-Object { $_.CommandLine -like '*douyin_agent*' } |")
        print("      ForEach-Object { taskkill /PID $_.ProcessId /T /F }")
        return 2
    finally:
        await provider.aclose()

    if ok:
        print("✓ 登录态正常（profile 里有 sessionid）")
        print("  下一步：启动服务 → 手动扫描，数据会正常入库")
        return 0

    print("✗ 登录态已过期：profile 里没有 sessionid，只有匿名设备 cookie。")
    print("  这时抖音只会给访客一份**不含最新作品**的列表，扫描结果不可信")
    print("  （表现：轮次台账写「跳过：回看 3 天内没有新作品」，三张表都不动）。")
    print(f"  修复：{LOGIN_EXPIRED_HINT}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
