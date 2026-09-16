"""业务存储 · 飞书多维表格实现。

设计要点（与 SQLite 实现刻意不同的三处）：

1. **表结构自动初始化**。首次启动会按需建表、建字段——你不用手动在飞书里
   点一遍。已有的表和字段不会被改动，重复调用是幂等的。

2. **幂等键本地镜像**。多维表格没有便宜的「EXISTS」查询，而 `exists_*` 在
   checkpointer 重放时会被高频调用。所以启动时把各表的幂等键拉进内存索引，
   之后在本地判重，写完再回填。代价是**写入飞书与更新本地索引不在同一事务内**：
   若「飞书写成功但索引没更新」时进程崩溃，重启后重新拉一次索引即可自愈
   （索引是**从飞书重建**的，不是权威源）。

3. **只做多维表格，不做消息推送**。推送归 `app/notifiers/`——职责不重叠，
   避免出现两个地方都能 `send_card` 的历史问题。

API 参考：https://open.feishu.cn/open-apis/bitable/v1/...
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import Settings
from app.core.coerce import cast_config
from app.core.feishu_api import OPEN_BASE, TenantToken
from app.core.retry import retry_with_backoff

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 字段类型：1=多行文本 2=数字 3=单选 7=复选框
T_TEXT, T_NUM, T_SELECT, T_CHECK = 1, 2, 3, 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cast(value: Any, type_: str) -> Any:
    """把《配置表》里的「值」转成目标类型（实现见 `app/core/coerce.py`，与 SQLite 共用）。"""
    return cast_config(value, type_, _as_text)


def _as_text(value: Any) -> str:
    """多维表格的文本字段可能返回字符串、富文本片段数组或数字。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in value)
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value)


def _as_num(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, list):
        value = _as_text(value)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        value = _as_text(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是")


# ---------------------------------------------------------------- 表结构定义
# (中文表名字段名, 类型)；第一列一律是幂等键/主键，用于自建索引。
ACCOUNTS_FIELDS = [
    ("账号名", T_TEXT), ("sec_uid", T_TEXT), ("主页链接", T_TEXT),
    ("启用", T_CHECK), ("备注", T_TEXT), ("创建时间", T_TEXT),
]
SNAPSHOT_FIELDS = [
    ("幂等键", T_TEXT), ("run_id", T_TEXT), ("扫描时间", T_TEXT), ("账号", T_TEXT),
    ("video_id", T_TEXT), ("标题", T_TEXT), ("发布时间", T_TEXT),
    ("点赞", T_NUM), ("评论", T_NUM), ("分享", T_NUM), ("数据源", T_TEXT),
]
DELTA_FIELDS = [
    ("幂等键", T_TEXT), ("run_id", T_TEXT), ("video_id", T_TEXT), ("账号", T_TEXT),
    ("标题", T_TEXT), ("上一轮点赞", T_NUM), ("本轮点赞", T_NUM), ("增量", T_NUM),
    ("是否告警", T_CHECK), ("已推送", T_CHECK), ("推送时间", T_TEXT),
]
COMMENT_FIELDS = [
    ("幂等键", T_TEXT), ("thread_id", T_TEXT), ("run_id", T_TEXT), ("video_id", T_TEXT),
    ("账号", T_TEXT), ("comment_id", T_TEXT), ("评论内容", T_TEXT), ("评论时间", T_TEXT),
    ("命中关键词", T_TEXT), ("歌曲名", T_TEXT), ("歌手", T_TEXT), ("拟回复", T_TEXT),
    ("状态", T_TEXT), ("创建时间", T_TEXT), ("确认时间", T_TEXT),
]
ROUND_FIELDS = [
    ("run_id", T_TEXT), ("触发方式", T_TEXT), ("开始时间", T_TEXT), ("结束时间", T_TEXT),
    ("账号数", T_NUM), ("视频数", T_NUM), ("数据源", T_TEXT),
    ("告警数", T_NUM), ("错误数", T_NUM), ("评论线程", T_TEXT), ("备注", T_TEXT),
]
CONFIG_FIELDS = [
    ("配置项", T_TEXT), ("值", T_TEXT), ("类型", T_TEXT), ("分类", T_TEXT),
    ("说明", T_TEXT), ("更新时间", T_TEXT),
]
ALERT_FIELDS = [
    ("run_id", T_TEXT), ("video_id", T_TEXT), ("渠道", T_TEXT),
    ("类型", T_TEXT), ("内容", T_TEXT), ("发送时间", T_TEXT),
]

DEFAULT_CONFIG: list[tuple[str, str, str, str, str]] = [
    ("scan_mode", "cron", "str", "调度", "cron=按固定时点 | interval=按固定间隔"),
    ("scan_cron_hours", "12,18,22", "str", "调度", "cron 模式下的扫描时点"),
    ("scan_interval_minutes", "3", "int", "调度", "interval 模式下的扫描间隔（分钟）"),
    ("threshold", "300", "int", "阈值", "点赞增量超过它就告警；演示档可改成 10 或 20"),
    ("lookback_days", "3", "int", "采集", "只采集最近 N 天发布的视频"),
    ("comment_keywords", "什么歌,歌曲名,歌名,BGM,好听", "str", "采集", "评论命中关键词，逗号分隔"),
    ("provider_chain", "browser,mock", "str", "采集",
     "数据源降级链，按顺序尝试；演示档填 mock 即为确定性模拟数据"),
    ("comment_scope", "all", "str", "采集",
     "评论扫哪些视频：all=本轮采集的全部 | alerted=仅有告警的"),
]

DEFAULT_ACCOUNTS: list[tuple[str, str, str, str]] = [
    ("测试账号 A", "MS4wLjABAAAA_demo_account_a", "https://www.douyin.com/user/MS4wLjABAAAA_demo_account_a", "演示用"),
    ("测试账号 B", "MS4wLjABAAAA_demo_account_b", "https://www.douyin.com/user/MS4wLjABAAAA_demo_account_b", "演示用"),
]


# ================================================================ HTTP 客户端
class BitableClient:
    """多维表格 HTTP 封装：租户令牌、分页、限流重试、表结构自愈。

    刻意**只**封装多维表格，不含任何消息推送方法。
    """

    def __init__(self, app_id: str, app_secret: str, app_token: str, timeout: float = 15.0) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token_cache = TenantToken(app_id, app_secret)
        self._table_cache: dict[str, str] = {}

    async def close(self) -> None:
        await self._client.aclose()

    # ---------------- 鉴权 ----------------
    async def _ensure_token(self) -> str:
        # 令牌缓存逻辑与「应用消息」渠道共用（app/core/feishu_api.py）
        return await self._token_cache.get(self._client, label="飞书多维表格")

    async def request(self, method: str, path: str, **kwargs: Any) -> dict:
        """发一次开放平台请求，自动带 token、自动重试限流（99991400 / 429）。"""
        async def _call() -> dict:
            token = await self._ensure_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
            resp = await self._client.request(method, f"{OPEN_BASE}{path}", headers=headers, **kwargs)
            if resp.status_code == 429:
                raise RuntimeError("飞书限流（HTTP 429）")
            resp.raise_for_status()
            body = resp.json()
            code = body.get("code", 0)
            if code != 0:
                # 99991400 = 频率超限；其余为业务错误，重试无意义
                if code == 99991400:
                    raise RuntimeError(f"飞书限流：{body.get('msg')}")
                raise RuntimeError(f"飞书接口错误 code={code} msg={body.get('msg')} path={path}")
            return body

        return await retry_with_backoff(_call, label=f"飞书 {method} {path}", max_attempts=4)

    # ---------------- 表解析与自建 ----------------
    async def resolve_table(self, name_or_id: str, fields: list[tuple[str, int]]) -> str:
        """把配置里的「表名或表 ID」解析成 table_id；表不存在就按定义创建。"""
        key = name_or_id.strip()
        if key in self._table_cache:
            return self._table_cache[key]

        table_id = ""
        if key.startswith("tbl"):
            table_id = key
        else:
            body = await self.request("GET", f"/bitable/v1/apps/{self.app_token}/tables", params={"page_size": 100})
            for item in body.get("data", {}).get("items", []):
                if item.get("name") == key:
                    table_id = item.get("table_id", "")
                    break

        if not table_id:
            log.info("飞书多维表格：未找到「%s」，按内置结构创建", key)
            body = await self.request(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables",
                json={
                    "table": {
                        "name": key,
                        "default_view_name": "表格",
                        "fields": [{"field_name": f, "type": t} for f, t in fields],
                    }
                },
            )
            table_id = body["data"]["table_id"]
            self._table_cache[key] = table_id
            return table_id

        await self._ensure_fields(table_id, fields)
        self._table_cache[key] = table_id
        return table_id

    async def _ensure_fields(self, table_id: str, fields: list[tuple[str, int]]) -> None:
        body = await self.request(
            "GET", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields", params={"page_size": 100}
        )
        existing = {item.get("field_name") for item in body.get("data", {}).get("items", [])}
        for fname, ftype in fields:
            if fname in existing:
                continue
            log.info("飞书多维表格：补建字段「%s」", fname)
            await self.request(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields",
                json={"field_name": fname, "type": ftype},
            )

    # ---------------- 记录读写 ----------------
    async def list_records(self, table_id: str, page_size: int = 500) -> tuple[list[dict], int]:
        """返回 (records, total)。records 按写入顺序（record_id 序）。"""
        items: list[dict] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": min(page_size, 500)}
            if page_token:
                params["page_token"] = page_token
            body = await self.request(
                "GET", f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records", params=params
            )
            data = body.get("data", {})
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items, int(data.get("total", len(items)))
            page_token = data.get("page_token", "")

    async def count(self, table_id: str) -> int:
        body = await self.request(
            "GET",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records",
            params={"page_size": 1},
        )
        return int(body.get("data", {}).get("total", 0))

    async def batch_create(self, table_id: str, rows: list[dict]) -> list[dict]:
        """批量建记录，返回**创建结果**（含 `record_id`）。

        ⚠ 返回值必须是记录本身而不是条数：调用方要拿 `record_id` 去回填幂等索引，
        否则后续 `mark_alerted` 之类的「按幂等键更新」就找不到目标记录。
        踩过：这里早先 `return created`（int），调用方只能往索引里塞空串。
        """
        if not rows:
            return []
        created: list[dict] = []
        for i in range(0, len(rows), 500):
            chunk = rows[i : i + 500]
            body = await self.request(
                "POST",
                f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_create",
                json={"records": [{"fields": r} for r in chunk]},
            )
            created.extend((body.get("data") or {}).get("records") or [])
        return created

    async def update_record(self, table_id: str, record_id: str, fields: dict) -> None:
        await self.request(
            "PUT",
            f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/{record_id}",
            json={"fields": fields},
        )

    async def search(self, table_id: str, field: str, values: list[str]) -> list[dict]:
        """按字段等值检索（多值用 or 连接）。值列表为空时返回空。"""
        if not values:
            return []
        out: list[dict] = []
        for i in range(0, len(values), 50):  # 单次条件数别太多
            chunk = values[i : i + 50]
            conditions = [{"field_name": field, "operator": "is", "value": [v]} for v in chunk]
            payload: dict[str, Any] = {"page_size": 500}
            payload["filter"] = {
                "conjunction": "or" if len(conditions) > 1 else "and",
                "conditions": conditions,
            }
            page_token = ""
            while True:
                if page_token:
                    payload["page_token"] = page_token
                body = await self.request(
                    "POST",
                    f"/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/search",
                    json=payload,
                )
                data = body.get("data", {})
                out.extend(data.get("items", []))
                if not data.get("has_more"):
                    break
                page_token = data.get("page_token", "")
                payload.pop("page_token", None)
        return out


# ================================================================ 存储实现
class FeishuStorage:
    """实现了 StorageProvider 协议（业务数据落在多维表格）。

    与 SQLite 版语义一致：所有写操作先查幂等键，重复即跳过。
    """

    backend = "feishu"

    def __init__(self, settings: Settings, seed_defaults: bool = True) -> None:
        if not (settings.feishu_app_id and settings.feishu_app_secret and settings.feishu_app_token):
            raise RuntimeError(
                "STORAGE_BACKEND=feishu 需要在 .env 配置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_APP_TOKEN"
            )
        self.settings = settings
        self.client = BitableClient(
            settings.feishu_app_id, settings.feishu_app_secret, settings.feishu_app_token
        )
        # mirror 模式传 False：不往飞书里种演示账号/默认配置，
        # 全部以主库（SQLite）回填的现值为准。
        self._seed_defaults = seed_defaults
        self.tables: dict[str, str] = {}
        # 幂等键 → record_id（从飞书重建，见模块 docstring 第 2 点）
        self._snap_index: dict[str, str] = {}
        self._delta_index: dict[str, str] = {}
        self._comment_index: dict[str, str] = {}
        self._alert_index: set[str] = set()
        self._round_index: set[str] = set()

    # ------------------------------------------------------------ 生命周期
    async def init(self) -> None:
        s = self.settings
        self.tables = {
            "accounts": await self.client.resolve_table(s.feishu_table_accounts, ACCOUNTS_FIELDS),
            "snapshots": await self.client.resolve_table(s.feishu_table_snapshots, SNAPSHOT_FIELDS),
            "deltas": await self.client.resolve_table(s.feishu_table_deltas, DELTA_FIELDS),
            "comment_hits": await self.client.resolve_table(s.feishu_table_comments, COMMENT_FIELDS),
            "scan_rounds": await self.client.resolve_table(s.feishu_table_rounds, ROUND_FIELDS),
            "config": await self.client.resolve_table(s.feishu_table_config, CONFIG_FIELDS),
            "alert_log": await self.client.resolve_table("告警推送日志", ALERT_FIELDS),
        }
        await self._seed()
        await self.reindex()
        log.info("业务存储就绪：飞书多维表格 app_token=%s…", s.feishu_app_token[:12])

    async def close(self) -> None:
        await self.client.close()

    async def reindex(self) -> None:
        """从飞书**重建**本地幂等索引。崩溃恢复 / 手工改表后调用它即可自愈。"""
        self._snap_index.clear()
        self._delta_index.clear()
        self._comment_index.clear()
        self._alert_index.clear()
        self._round_index.clear()

        rows, _ = await self.client.list_records(self.tables["snapshots"])
        for r in rows:
            key = _as_text(r["fields"].get("幂等键"))
            if key:
                self._snap_index[key] = r["record_id"]

        rows, _ = await self.client.list_records(self.tables["deltas"])
        for r in rows:
            key = _as_text(r["fields"].get("幂等键"))
            if key:
                self._delta_index[key] = r["record_id"]

        rows, _ = await self.client.list_records(self.tables["comment_hits"])
        for r in rows:
            key = _as_text(r["fields"].get("幂等键"))
            if key:
                self._comment_index[key] = r["record_id"]

        rows, _ = await self.client.list_records(self.tables["alert_log"])
        for r in rows:
            f = r["fields"]
            self._alert_index.add(f"{_as_text(f.get('run_id'))}|{_as_text(f.get('video_id'))}|{_as_text(f.get('渠道'))}")

        rows, _ = await self.client.list_records(self.tables["scan_rounds"])
        for r in rows:
            rid = _as_text(r["fields"].get("run_id"))
            if rid:
                self._round_index.add(rid)

        log.info(
            "幂等索引重建完成：快照 %d / 增量 %d / 评论 %d / 告警 %d / 轮次 %d",
            len(self._snap_index), len(self._delta_index), len(self._comment_index),
            len(self._alert_index), len(self._round_index),
        )

    def _index_created(self, index: dict[str, str], created: list[dict]) -> None:
        """把刚建好的记录按「幂等键 → record_id」回填进内存索引。

        ⚠ 必须回填**真实 record_id**。这里早先只往索引里塞空串（当作「已存在」的
        廉价标记），结果是 `mark_alerted` 拿着空串去 PUT，把《增量与告警表》的
        「已推送」永远标不上——而且只影响**本进程内新建的行**，
        重启后 reindex() 填了真 id 就正常，所以特别难发现。
        """
        for item in created:
            key = _as_text((item.get("fields") or {}).get("幂等键"))
            rid = item.get("record_id")
            if key and rid:
                index[key] = rid

    async def _resolve_record_id(self, index: dict[str, str], table: str, idem_key: str) -> str | None:
        """幂等键 → record_id。索引里只有「存在」没有 id 时兜底查一次并缓存。"""
        rid = index.get(idem_key)
        if rid:
            return rid
        if idem_key not in index:
            return None                      # 压根没建过，不用更新
        items = await self.client.search(table, "幂等键", [idem_key])
        for it in items:
            if _as_text(it["fields"].get("幂等键")) == idem_key:
                index[idem_key] = it["record_id"]
                return it["record_id"]
        return None

    async def _seed(self) -> None:
        if not self._seed_defaults:
            return
        rows, _ = await self.client.list_records(self.tables["config"])
        existing = {_as_text(r["fields"].get("配置项")) for r in rows}
        missing = [
            {"配置项": k, "值": v, "类型": t, "分类": sc, "说明": nt, "更新时间": _now()}
            for k, v, t, sc, nt in DEFAULT_CONFIG
            if k not in existing
        ]
        if missing:
            await self.client.batch_create(self.tables["config"], missing)
            log.info("配置表补入 %d 条默认项", len(missing))

        rows, _ = await self.client.list_records(self.tables["accounts"])
        existing = {_as_text(r["fields"].get("sec_uid")) for r in rows}
        missing = [
            {"账号名": n, "sec_uid": u, "主页链接": h, "启用": True, "备注": nt, "创建时间": _now()}
            for n, u, h, nt in DEFAULT_ACCOUNTS
            if u not in existing
        ]
        if missing:
            await self.client.batch_create(self.tables["accounts"], missing)

    # ------------------------------------------------------------ 配置表
    async def list_config(self) -> list[dict]:
        rows, _ = await self.client.list_records(self.tables["config"])
        out = [
            {
                "key": _as_text(r["fields"].get("配置项")),
                "value": _as_text(r["fields"].get("值")),
                "type": _as_text(r["fields"].get("类型")) or "str",
                "scope": _as_text(r["fields"].get("分类")) or "通用",
                "note": _as_text(r["fields"].get("说明")),
                "updated_at": _as_text(r["fields"].get("更新时间")),
            }
            for r in rows
        ]
        out.sort(key=lambda x: (x["scope"], x["key"]))
        return out

    async def get_config(self) -> dict[str, Any]:
        return {r["key"]: _cast(r["value"], r["type"]) for r in await self.list_config()}

    async def set_config(self, key: str, value: Any) -> None:
        await self._set_config_value(key, value)
        # 配置表的读写都存在本地缓存会有一致性风险，这里直接走远端每次刷新

    async def put_config(self, item: dict) -> None:
        """整条写入配置项（含 类型/分类/说明 元信息）。

        与 `set_config` 的分工：`set_config` 只在用户改「值」时调用，
        必须**保留**已有的说明；`put_config` 用于初始化/回填，把元信息
        一起带过去——否则飞书里的《配置表》只有一列光秃秃的值，
        用户根本不知道 `threshold` 是什么。
        """
        rows, _ = await self.client.list_records(self.tables["config"])
        key = str(item["key"])
        fields = {
            "配置项": key,
            "值": "true" if isinstance(item.get("value"), bool) else str(item.get("value", "")),
            "类型": str(item.get("type") or "str"),
            "分类": str(item.get("scope") or "通用"),
            "说明": str(item.get("note") or ""),
            "更新时间": item.get("updated_at") or _now(),
        }
        for r in rows:
            if _as_text(r["fields"].get("配置项")) == key:
                await self.client.update_record(self.tables["config"], r["record_id"], fields)
                return
        await self.client.batch_create(self.tables["config"], [fields])

    async def _set_config_value(self, key: str, value: Any) -> None:
        rows, _ = await self.client.list_records(self.tables["config"])
        stored = "true" if isinstance(value, bool) else str(value)
        for r in rows:
            if _as_text(r["fields"].get("配置项")) == key:
                await self.client.update_record(
                    self.tables["config"], r["record_id"], {"值": stored, "更新时间": _now()}
                )
                return
        await self.client.batch_create(
            self.tables["config"],
            [{"配置项": key, "值": stored, "类型": "str", "分类": "通用", "说明": "", "更新时间": _now()}],
        )

    # ------------------------------------------------------------ 表 1 账号
    async def list_accounts(self, only_enabled: bool = True) -> list[dict]:
        rows, _ = await self.client.list_records(self.tables["accounts"])
        out: list[dict] = []
        for r in rows:
            f = r["fields"]
            acc = {
                "id": r["record_id"],
                "name": _as_text(f.get("账号名")),
                "sec_uid": _as_text(f.get("sec_uid")),
                "homepage": _as_text(f.get("主页链接")),
                "enabled": 1 if _as_bool(f.get("启用")) else 0,
                "note": _as_text(f.get("备注")),
                "created_at": _as_text(f.get("创建时间")),
            }
            if only_enabled and not acc["enabled"]:
                continue
            out.append(acc)
        return out

    async def upsert_account(self, acc: dict) -> None:
        rows, _ = await self.client.list_records(self.tables["accounts"])
        fields = {
            "账号名": acc.get("name", ""),
            "sec_uid": acc["sec_uid"],
            "主页链接": acc.get("homepage", ""),
            "启用": bool(acc.get("enabled", True)),
            "备注": acc.get("note", ""),
        }
        for r in rows:
            if _as_text(r["fields"].get("sec_uid")) == acc["sec_uid"]:
                await self.client.update_record(self.tables["accounts"], r["record_id"], fields)
                return
        await self.client.batch_create(self.tables["accounts"], [{**fields, "创建时间": _now()}])

    # 字段名映射：本地键 → 多维表格列名（只更新传进来的那几列，其余保持原值）
    _ACCOUNT_COLS = {
        "name": "账号名",
        "sec_uid": "sec_uid",
        "homepage": "主页链接",
        "enabled": "启用",
        "note": "备注",
    }

    async def update_account(self, old_sec_uid: str, fields: dict) -> bool:
        """就地改账号字段（含 sec_uid 本身），避免 upsert 匹配不上而插出重复账号。"""
        patch = {
            self._ACCOUNT_COLS[k]: (bool(v) if k == "enabled" else v)
            for k, v in fields.items()
            if k in self._ACCOUNT_COLS
        }
        if not patch:
            return False
        rows, _ = await self.client.list_records(self.tables["accounts"])
        for r in rows:
            if _as_text(r["fields"].get("sec_uid")) == old_sec_uid:
                await self.client.update_record(self.tables["accounts"], r["record_id"], patch)
                return True
        return False

    async def delete_account(self, sec_uid: str) -> None:
        rows, _ = await self.client.list_records(self.tables["accounts"])
        for r in rows:
            if _as_text(r["fields"].get("sec_uid")) == sec_uid:
                await self.client.request(
                    "DELETE",
                    f"/bitable/v1/apps/{self.client.app_token}/tables/{self.tables['accounts']}"
                    f"/records/{r['record_id']}",
                )
                return

    # ------------------------------------------------------------ 表 2 快照
    async def exists_snapshot(self, idem_key: str) -> bool:
        return idem_key in self._snap_index

    async def batch_create_snapshots(self, rows: list[dict]) -> int:
        fresh = [r for r in rows if r["idem_key"] not in self._snap_index]
        if not fresh:
            return 0
        payload = [
            {
                "幂等键": r["idem_key"], "run_id": r["run_id"], "扫描时间": r["scanned_at"],
                "账号": r["account"], "video_id": r["video_id"], "标题": r.get("title", ""),
                "发布时间": r.get("publish_time", ""), "点赞": int(r.get("likes", 0)),
                "评论": int(r.get("comments", 0)), "分享": int(r.get("shares", 0)),
                "数据源": r.get("source", "mock"),
            }
            for r in fresh
        ]
        created = await self.client.batch_create(self.tables["snapshots"], payload)
        self._index_created(self._snap_index, created)
        return len(created)

    async def previous_snapshot(self, video_id: str, run_id: str) -> dict | None:
        items = await self.client.search(self.tables["snapshots"], "video_id", [video_id])
        best: dict | None = None
        for r in items:  # 列表按写入顺序，倒着找第一条非本轮的
            f = r["fields"]
            if _as_text(f.get("run_id")) == run_id:
                continue
            best = {
                "id": r["record_id"],
                "idem_key": _as_text(f.get("幂等键")),
                "run_id": _as_text(f.get("run_id")),
                "scanned_at": _as_text(f.get("扫描时间")),
                "account": _as_text(f.get("账号")),
                "video_id": _as_text(f.get("video_id")),
                "title": _as_text(f.get("标题")),
                "publish_time": _as_text(f.get("发布时间")),
                "likes": _as_num(f.get("点赞")),
                "comments": _as_num(f.get("评论")),
                "shares": _as_num(f.get("分享")),
                "source": _as_text(f.get("数据源")),
            }
        return best

    async def list_snapshots(self, limit: int = 100, video_id: str | None = None) -> list[dict]:
        if video_id:
            items = await self.client.search(self.tables["snapshots"], "video_id", [video_id])
        else:
            items, _ = await self.client.list_records(self.tables["snapshots"])
        out = []
        for r in items:
            f = r["fields"]
            out.append(
                {
                    "id": r["record_id"],
                    "idem_key": _as_text(f.get("幂等键")),
                    "run_id": _as_text(f.get("run_id")),
                    "scanned_at": _as_text(f.get("扫描时间")),
                    "account": _as_text(f.get("账号")),
                    "video_id": _as_text(f.get("video_id")),
                    "title": _as_text(f.get("标题")),
                    "publish_time": _as_text(f.get("发布时间")),
                    "likes": _as_num(f.get("点赞")),
                    "comments": _as_num(f.get("评论")),
                    "shares": _as_num(f.get("分享")),
                    "source": _as_text(f.get("数据源")),
                }
            )
        out.reverse()
        return out[:limit]

    # ------------------------------------------------------------ 表 3 增量
    async def exists_delta(self, idem_key: str) -> bool:
        return idem_key in self._delta_index

    async def create_deltas(self, rows: list[dict]) -> int:
        fresh = [r for r in rows if r["idem_key"] not in self._delta_index]
        if not fresh:
            return 0
        payload = [
            {
                "幂等键": r["idem_key"], "run_id": r["run_id"], "video_id": r["video_id"],
                "账号": r.get("account", ""), "标题": r.get("title", ""),
                "上一轮点赞": int(r.get("prev_likes", 0)), "本轮点赞": int(r.get("curr_likes", 0)),
                "增量": int(r.get("delta", 0)), "是否告警": bool(r.get("is_alert")),
                "已推送": False, "推送时间": "",
            }
            for r in fresh
        ]
        created = await self.client.batch_create(self.tables["deltas"], payload)
        self._index_created(self._delta_index, created)
        return len(created)

    async def list_deltas(self, limit: int = 100, only_alert: bool = False) -> list[dict]:
        items, _ = await self.client.list_records(self.tables["deltas"])
        out = []
        for r in items:
            f = r["fields"]
            row = {
                "id": r["record_id"],
                "idem_key": _as_text(f.get("幂等键")),
                "run_id": _as_text(f.get("run_id")),
                "video_id": _as_text(f.get("video_id")),
                "account": _as_text(f.get("账号")),
                "title": _as_text(f.get("标题")),
                "prev_likes": _as_num(f.get("上一轮点赞")),
                "curr_likes": _as_num(f.get("本轮点赞")),
                "delta": _as_num(f.get("增量")),
                "is_alert": 1 if _as_bool(f.get("是否告警")) else 0,
                "alerted": 1 if _as_bool(f.get("已推送")) else 0,
                "alert_time": _as_text(f.get("推送时间")),
            }
            if only_alert and not row["is_alert"]:
                continue
            out.append(row)
        out.reverse()
        return out[:limit]

    async def mark_alerted(self, idem_keys: list[str], when: str) -> int:
        changed = 0
        for key in idem_keys:
            rid = await self._resolve_record_id(self._delta_index, self.tables["deltas"], key)
            if not rid:
                continue
            await self.client.update_record(
                self.tables["deltas"], rid, {"已推送": True, "推送时间": when}
            )
            changed += 1
        return changed

    # ------------------------------------------------------------ 表 4 评论命中
    async def upsert_comment_hits(self, rows: list[dict]) -> int:
        fresh = [r for r in rows if r["idem_key"] not in self._comment_index]
        if not fresh:
            return 0
        payload = [
            {
                "幂等键": r["idem_key"], "thread_id": r["thread_id"], "run_id": r.get("run_id", ""),
                "video_id": r["video_id"], "账号": r.get("account", ""),
                "comment_id": r.get("comment_id", ""), "评论内容": r.get("content", ""),
                "评论时间": r.get("comment_time", ""), "命中关键词": r.get("keywords", ""),
                "歌曲名": r.get("song_title", ""), "歌手": r.get("song_artist", ""),
                "拟回复": r.get("draft", ""), "状态": "pending",
                "创建时间": r.get("created_at") or _now(), "确认时间": "",
            }
            for r in fresh
        ]
        created = await self.client.batch_create(self.tables["comment_hits"], payload)
        self._index_created(self._comment_index, created)
        return len(created)

    async def list_comment_hits(self, status: str | None = None, limit: int = 100) -> list[dict]:
        items, _ = await self.client.list_records(self.tables["comment_hits"])
        out = []
        for r in items:
            f = r["fields"]
            row = {
                "id": r["record_id"],
                "idem_key": _as_text(f.get("幂等键")),
                "thread_id": _as_text(f.get("thread_id")),
                "run_id": _as_text(f.get("run_id")),
                "video_id": _as_text(f.get("video_id")),
                "account": _as_text(f.get("账号")),
                "comment_id": _as_text(f.get("comment_id")),
                "content": _as_text(f.get("评论内容")),
                "comment_time": _as_text(f.get("评论时间")),
                "keywords": _as_text(f.get("命中关键词")),
                "song_title": _as_text(f.get("歌曲名")),
                "song_artist": _as_text(f.get("歌手")),
                "draft": _as_text(f.get("拟回复")),
                "status": _as_text(f.get("状态")) or "pending",
                "created_at": _as_text(f.get("创建时间")),
                "decided_at": _as_text(f.get("确认时间")),
            }
            if status and row["status"] != status:
                continue
            out.append(row)
        out.reverse()
        return out[:limit]

    async def decide_comment_hits(self, decisions: dict[str, str]) -> int:
        """按 comment_id 定位（命中记录去重键只含 comment_id，跨轮次复用同一条）。"""
        if not decisions:
            return 0
        items = await self.client.search(
            self.tables["comment_hits"], "comment_id", list(decisions.keys())
        )
        now = _now()
        changed = 0
        for r in items:
            cid = _as_text(r["fields"].get("comment_id"))
            status = decisions.get(cid)
            if not status:
                continue
            await self.client.update_record(
                self.tables["comment_hits"], r["record_id"], {"状态": status, "确认时间": now}
            )
            changed += 1
        return changed

    # ------------------------------------------------------------ 表 5 轮次
    async def create_round(self, row: dict) -> None:
        if row["run_id"] in self._round_index:
            return
        await self.client.batch_create(
            self.tables["scan_rounds"],
            [
                {
                    "run_id": row["run_id"], "触发方式": row.get("trigger_type", "manual"),
                    "开始时间": row["started_at"], "结束时间": "",
                    "账号数": int(row.get("account_count", 0)), "视频数": int(row.get("video_count", 0)),
                    "数据源": row.get("source", ""), "告警数": int(row.get("alert_count", 0)),
                    "错误数": int(row.get("error_count", 0)), "评论线程": row.get("thread_ids", ""),
                    "备注": row.get("note", ""),
                }
            ],
        )
        self._round_index.add(row["run_id"])

    async def finish_round(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        field_map = {
            "finished_at": "结束时间", "account_count": "账号数", "video_count": "视频数",
            "source": "数据源", "alert_count": "告警数", "error_count": "错误数",
            "thread_ids": "评论线程", "note": "备注", "trigger_type": "触发方式",
        }
        payload = {field_map[k]: v for k, v in fields.items() if k in field_map}
        payload.setdefault("结束时间", _now())
        items = await self.client.search(self.tables["scan_rounds"], "run_id", [run_id])
        for r in items:
            await self.client.update_record(self.tables["scan_rounds"], r["record_id"], payload)
            return

    async def list_rounds(self, limit: int = 50) -> list[dict]:
        items, _ = await self.client.list_records(self.tables["scan_rounds"])
        out = []
        for r in items:
            f = r["fields"]
            out.append(
                {
                    "run_id": _as_text(f.get("run_id")),
                    "trigger_type": _as_text(f.get("触发方式")),
                    "started_at": _as_text(f.get("开始时间")),
                    "finished_at": _as_text(f.get("结束时间")),
                    "account_count": _as_num(f.get("账号数")),
                    "video_count": _as_num(f.get("视频数")),
                    "source": _as_text(f.get("数据源")),
                    "alert_count": _as_num(f.get("告警数")),
                    "error_count": _as_num(f.get("错误数")),
                    "thread_ids": _as_text(f.get("评论线程")),
                    "note": _as_text(f.get("备注")),
                }
            )
        # 按**写入顺序倒序**返回（多维表格的 list 就是插入序，即时间序）。
        # 刻意不按「开始时间」字符串排序：它只精确到秒，手动连点两次会落在同一秒，
        # 那种排序会让「最近一轮」变得不确定。
        out.reverse()
        return out[:limit]

    # ------------------------------------------------------------ 告警日志
    async def try_log_alert(
        self, run_id: str, video_id: str, channel: str, payload: str, kind: str = "alert"
    ) -> bool:
        key = f"{run_id}|{video_id}|{channel}"
        if key in self._alert_index:
            return False
        await self.client.batch_create(
            self.tables["alert_log"],
            [{
                "run_id": run_id, "video_id": video_id, "渠道": channel,
                "类型": kind, "内容": payload, "发送时间": _now(),
            }],
        )
        self._alert_index.add(key)
        return True

    async def list_alerts(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        items, _ = await self.client.list_records(self.tables["alert_log"])
        out = []
        for r in items:
            row = {
                "id": r["record_id"],
                "run_id": _as_text(r["fields"].get("run_id")),
                "video_id": _as_text(r["fields"].get("video_id")),
                "channel": _as_text(r["fields"].get("渠道")),
                "kind": _as_text(r["fields"].get("类型")) or "alert",
                "payload": _as_text(r["fields"].get("内容")),
                "sent_at": _as_text(r["fields"].get("发送时间")),
            }
            if kind and row["kind"] != kind:
                continue
            out.append(row)
        out.reverse()
        return out[:limit]

    # ------------------------------------------------------------ 概览
    async def stats(self) -> dict:
        last_rounds = await self.list_rounds(limit=1)
        pending = await self.list_comment_hits(status="pending", limit=200)
        hits = await self.list_comment_hits(limit=200)
        deltas = await self.list_deltas(limit=500)
        alerts = await self.list_alerts(limit=1000)
        return {
            "rounds": await self.client.count(self.tables["scan_rounds"]),
            "accounts": len(await self.list_accounts()),
            "snapshots": await self.client.count(self.tables["snapshots"]),
            "deltas": await self.client.count(self.tables["deltas"]),
            "alerts": sum(1 for d in deltas if d["is_alert"]),
            # 「已推送」只数点赞告警，评论提醒单独一个口径（见 sqlite 同名字段注释）
            "alerts_sent": sum(1 for a in alerts if a.get("kind", "alert") == "alert"),
            "reviews_notified": sum(1 for a in alerts if a.get("kind") == "review"),
            "pending_comments": len(pending),
            "comment_hits": len(hits),
            "last_round": last_rounds[0] if last_rounds else None,
            "config": await self.get_config(),
        }


__all__ = ["FeishuStorage", "BitableClient"]
