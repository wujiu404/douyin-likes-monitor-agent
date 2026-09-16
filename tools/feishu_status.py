"""飞书多维表格体检工具。

两个用途：
1. `python tools/feishu_status.py` —— 看 7 张业务表各有多少行、《配置表》现在是什么值。
   排查「数据到底有没有镜像过去」时先跑这个。
2. `python tools/feishu_status.py --set threshold=5` —— **只改飞书那一格**，
   模拟「用户直接在多维表格里手改」。用来验证《配置表》作为控制面是否真的生效
   （改完不用重启，下一轮扫描就用新值）。

凭证从 .env（`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_APP_TOKEN`）读，不写死在代码里。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

OPEN_BASE = "https://open.feishu.cn/open-apis"


async def _open() -> tuple[httpx.AsyncClient, dict[str, str], str, dict[str, str]]:
    """返回 (client, headers, base_token, {表名: table_id})。调用方负责 aclose。"""
    s = get_settings()
    if not (s.feishu_app_id and s.feishu_app_secret and s.feishu_app_token):
        raise SystemExit("缺 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN，见 .env.example")

    client = httpx.AsyncClient(timeout=25)
    body = (await client.post(
        f"{OPEN_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": s.feishu_app_id, "app_secret": s.feishu_app_secret},
    )).json()
    if not body.get("tenant_access_token"):
        await client.aclose()
        raise SystemExit(f"取 tenant_access_token 失败：{body.get('code')} {body.get('msg')}")

    headers = {"Authorization": f"Bearer {body['tenant_access_token']}"}
    base = s.feishu_app_token
    items = (await client.get(
        f"{OPEN_BASE}/bitable/v1/apps/{base}/tables", headers=headers, params={"page_size": 100}
    )).json()["data"]["items"]
    return client, headers, base, {t["name"]: t["table_id"] for t in items}


async def _list_records(
    client: httpx.AsyncClient, headers: dict, base: str, table_id: str, page_size: int = 100
) -> list[dict]:
    body = (await client.get(
        f"{OPEN_BASE}/bitable/v1/apps/{base}/tables/{table_id}/records",
        headers=headers, params={"page_size": page_size},
    )).json()
    return (body.get("data") or {}).get("items") or []


async def show() -> None:
    client, headers, base, tables = await _open()
    try:
        print(f"多维表格 app_token = {base}")
        print("（打开链接形如 https://<你的租户>.feishu.cn/base/<app_token>，租户子域看你自己飞书地址栏）\n")
        print("=== 各表行数 ===")
        for name, tid in tables.items():
            r = await client.get(
                f"{OPEN_BASE}/bitable/v1/apps/{base}/tables/{tid}/records",
                headers=headers, params={"page_size": 1},
            )
            print(f"   {name:<12} {(r.json().get('data') or {}).get('total')}")

        if "配置表" in tables:
            items = await _list_records(client, headers, base, tables["配置表"])
            print("\n=== 配置表（控制面，改这里下一轮就生效）===")
            for it in sorted(items, key=lambda x: str((x.get("fields") or {}).get("配置项"))):
                f = it.get("fields") or {}
                print(f"   {str(f.get('配置项')):<22} = {str(f.get('值')):<28} {f.get('说明') or ''}")
    finally:
        await client.aclose()


async def set_value(key: str, value: str) -> None:
    client, headers, base, tables = await _open()
    try:
        cfg = tables["配置表"]
        for it in await _list_records(client, headers, base, cfg):
            if (it.get("fields") or {}).get("配置项") == key:
                await client.put(
                    f"{OPEN_BASE}/bitable/v1/apps/{base}/tables/{cfg}/records/{it['record_id']}",
                    headers=headers, json={"fields": {"值": value}},
                )
                print(f"飞书《配置表》{key} → {value}（只改了飞书，本地不动）")
                return
        raise SystemExit(f"《配置表》里没有 {key}")
    finally:
        await client.aclose()


def main() -> None:
    p = argparse.ArgumentParser(description="飞书多维表格体检 / 模拟手改配置")
    p.add_argument("--set", dest="set_value", metavar="KEY=VALUE", help="只改飞书侧某一格配置")
    args = p.parse_args()
    if args.set_value:
        key, _, value = args.set_value.partition("=")
        asyncio.run(set_value(key, value))
    else:
        asyncio.run(show())


if __name__ == "__main__":
    main()
