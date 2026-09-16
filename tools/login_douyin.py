"""一次性登录抖音网页版（browser provider 配套）。

用法：
    .venv/Scripts/python.exe tools/login_douyin.py

行为：
1. 用 .env 里的 BROWSER_USER_DATA_DIR 启动一个**可见的** Edge 窗口
2. 打开抖音首页，弹出登录二维码
3. 你用手机抖音 App 扫码登录（5 分钟超时，每 3 秒检测一次）
4. 检测到已登录 → 自动把 .env 的 BROWSER_HEADLESS 改成 true → 提示重启项目

登录态存在用户数据目录里，以后所有无头采集都复用它，无需再扫。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

LOGIN_HINTS = (
    # 已登录的可靠特征：window 里能取到登录用户信息（任一命中即算成功）
    "document.querySelector('[class*=loginUserInfo], [data-e2e=login-user-info]') !== null",
)


async def is_logged_in(page) -> bool:
    """判定登录态：优先看 cookie（sessionid 只在登录后出现），兜底看页面元素。"""
    try:
        cookies = {c["name"] for c in await page.context.cookies()}
        if "sessionid" in cookies or "sessionid_ss" in cookies:
            return True
    except Exception:  # noqa: BLE001
        pass
    for js in LOGIN_HINTS:
        try:
            if await page.evaluate(js):
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


async def main() -> int:
    settings = get_settings()
    if not settings.browser_user_data_dir:
        print("✗ .env 里 BROWSER_USER_DATA_DIR 为空，先配置它")
        return 2

    from playwright.async_api import async_playwright

    print(f"→ 启动 Edge（用户目录：{settings.browser_user_data_dir}）")
    print("=" * 56)
    print("  窗口马上会弹出并打开抖音首页")
    print("  请用 手机抖音 App → 扫页面上的二维码 登录")
    print("  登录成功后本脚本会自动收尾（最长等 5 分钟）")
    print("=" * 56)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=settings.browser_user_data_dir,
            channel=settings.browser_channel or "msedge",
            headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60_000)

        # 先看是不是本来就已登录（二次运行本脚本时）
        await page.wait_for_timeout(3_000)
        if await is_logged_in(page):
            print("✓ 已经是登录状态（之前的登录态还在），无需再扫")
        else:
            # 未登录：尽量把登录面板点出来（抖音首页右上角「登录」按钮）
            try:
                for sel in ("button:has-text('登录')", "[class*=loginContainer]"):
                    btn = page.locator(sel).first
                    if await btn.count():
                        await btn.click(timeout=3_000)
                        break
            except Exception:  # noqa: BLE001
                pass

            deadline = asyncio.get_event_loop().time() + 300
            ok = False
            while asyncio.get_event_loop().time() < deadline:
                await page.wait_for_timeout(3_000)
                if await is_logged_in(page):
                    ok = True
                    break
                print("  … 等待扫码（每 3 秒检测一次）", flush=True)
            if not ok:
                print("✗ 5 分钟内没检测到登录。窗口先不关，手动登录后重跑本脚本即可。")
                await ctx.close()
                return 1
            print("✓ 检测到登录成功！")

        # 确认一下用户昵称（尽力而为，失败不影响）
        try:
            name = await page.evaluate(
                "() => { const el = document.querySelector('[class*=loginUserInfo] img[alt], [data-e2e=login-user-info] img[alt]'); return el ? el.getAttribute('alt') : ''; }"
            )
            if name:
                print(f"  登录账号：{name}")
        except Exception:  # noqa: BLE001
            pass

        await page.wait_for_timeout(2_000)  # 让 cookie 落盘
        await ctx.close()

    # 把 .env 改成无头模式
    env_file = ROOT / ".env"
    text = env_file.read_text(encoding="utf-8")
    new_text = text.replace("BROWSER_HEADLESS=false", "BROWSER_HEADLESS=true")
    if new_text != text:
        env_file.write_text(new_text, encoding="utf-8")
        print("✓ 已把 .env 的 BROWSER_HEADLESS 改为 true（以后无头采集）")
    print("\n下一步：重启项目，看板里添加真实账号（sec_uid），点「手动扫描」验证。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
