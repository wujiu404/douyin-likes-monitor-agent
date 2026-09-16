"""browser provider 的登录态判据（2026-09-16 事故的回归测试）。

事故经过：抖音登录态掉了，profile 里只剩匿名设备 cookie（ttwid / odin_tt / UIFID…）。
未登录访客拿到的作品列表**不含最新作品**（实测最新一条停在 9 天前），
按回看窗口过滤后正好 0 条 —— 和「账号这几天没发作品」长得一模一样。
于是那几轮扫描全被判成「窗口内没有新作品」，整轮跳过，
《视频快照表》《点赞增量表》《告警表》一行都没写，用户来问「为什么什么都没有」。

所以这里钉住两件事：
① 只有 sessionid 系列 cookie 才算登录凭据，匿名设备 cookie 一律不算；
② 未登录要报明确的错（带操作指引），不能安静返回空。
"""
from __future__ import annotations

from app.providers.browser import (
    LOGIN_COOKIE_NAMES,
    LOGIN_EXPIRED_HINT,
    BrowserProvider,
    has_login_cookie,
)

# 未登录时 profile 里真实存在的那一堆匿名 cookie（照抄实测）
ANONYMOUS_COOKIES = [
    {"name": "ttwid", "value": "xxx"},
    {"name": "odin_tt", "value": "xxx"},
    {"name": "UIFID", "value": "xxx"},
    {"name": "passport_csrf_token", "value": "xxx"},
    {"name": "s_v_web_id", "value": "xxx"},
]


def test_anonymous_device_cookies_are_not_login() -> None:
    """匿名设备 cookie 到处都是，拿它们判登录就会得出「已登录」的错觉。"""
    assert has_login_cookie(ANONYMOUS_COOKIES) is False


def test_sessionid_means_logged_in() -> None:
    assert has_login_cookie([{"name": "sessionid", "value": "abc"}]) is True
    assert has_login_cookie([{"name": "sessionid_ss", "value": "abc"}]) is True
    assert LOGIN_COOKIE_NAMES == ("sessionid", "sessionid_ss")


def test_empty_cookie_value_is_not_login() -> None:
    """被清空（值为空）的 sessionid 不算登录——抖音失效时会把它留成空壳。"""
    assert has_login_cookie([{"name": "sessionid", "value": ""}]) is False
    assert has_login_cookie([{"name": "sessionid", "value": None}]) is False


def test_has_login_cookie_tolerates_junk() -> None:
    assert has_login_cookie(None) is False
    assert has_login_cookie([]) is False
    assert has_login_cookie([{"no_name": 1}, "not-a-dict"]) is False


def test_login_hint_tells_you_what_to_do() -> None:
    """报错话术必须能直接照做：带上重扫登录态的那条命令。"""
    assert "sessionid" in LOGIN_EXPIRED_HINT
    assert "tools/login_douyin.py" in LOGIN_EXPIRED_HINT


async def test_check_login_reports_not_configured(tmp_path) -> None:
    """没配用户目录时抛「未配置」而不是「未登录」——两件事的处理路径完全不同。"""
    import pytest

    from app.config import Settings
    from app.providers.base import ProviderNotConfigured

    provider = BrowserProvider(Settings(browser_user_data_dir=""))
    with pytest.raises(ProviderNotConfigured):
        await provider.check_login()
