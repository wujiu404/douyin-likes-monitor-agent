"""把「非标准形态」的 sec_uid（v.douyin.com 短链 / App 复制的纯数字 ID）解析成
标准 MS4wLjABAAAA… 形态，并**回写**《监控账号表》（本地 + 飞书镜像）。

用法：
    .venv/Scripts/python.exe tools/resolve_sec_uid.py            # 只报告
    .venv/Scripts/python.exe tools/resolve_sec_uid.py --write    # 确认后回写

为什么需要它：网页版只认标准 sec_uid，短链能让 browser provider 每轮多花几秒去跟重定向，
且日志里会一直刷「sec_uid 不是标准形态」的告警。提前解析一次写回，后续轮次直接命中。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.providers.browser import SEC_UID_RE, USER_PAGE  # noqa: E402


async def main(write: bool) -> None:
    from app.providers.browser import BrowserProvider
    from app.storage.sqlite import SqliteStorage

    settings = get_settings()
    storage = SqliteStorage(settings.db_file)
    await storage.init()
    accounts = await storage.list_accounts(only_enabled=True)

    todo = [a for a in accounts if not SEC_UID_RE.fullmatch(a["sec_uid"] or "")]
    if not todo:
        print("所有启用账号的 sec_uid 都已是标准形态，无需解析。")
        await storage.close()
        return

    provider = BrowserProvider(settings)
    resolved: dict[int, str] = {}
    try:
        for acc in todo:
            print(f"[解析] {acc['name']} | {acc['sec_uid'][:24]} | {acc['homepage']}")
            sec_uid = await provider.resolve_sec_uid(acc)
            if not sec_uid or not SEC_UID_RE.fullmatch(sec_uid):
                print("   ✗ 没解析出来，请检查 homepage 链接是否还有效")
                continue
            resolved[acc["id"]] = sec_uid
            print(f"   ✓ {sec_uid}")
    finally:
        await provider.aclose()

    if write and resolved:
        for acc_id, sec_uid in resolved.items():
            old = next(a["sec_uid"] for a in todo if a["id"] == acc_id)
            await storage.update_account(old, {
                "sec_uid": sec_uid,
                "homepage": USER_PAGE.format(sec_uid=sec_uid),
            })
            print(f"[回写] account#{acc_id} {old} → {sec_uid}")
    elif resolved:
        print("\n（未加 --write，只报告不回写）")

    await storage.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="解析并回写标准 sec_uid")
    p.add_argument("--write", action="store_true", help="确认无误后回写账号表")
    asyncio.run(main(p.parse_args().write))
