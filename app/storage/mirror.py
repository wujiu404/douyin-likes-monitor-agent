"""双写存储：SQLite 主库 + 飞书多维表格镜像。

动机（2026-09-15 用户需求）：爬取数据「同时写到飞书多维表格」，且阈值、
调度时间在两边都可控制。

职责划分（刻意不对称）：
- **读**全走主库（SQLite）——看板响应快，飞书延迟/故障不影响任何功能
- **写**先主库后镜像——主库是权威源；镜像写失败**只告警不中断**，
  下一轮扫描照常跑（飞书是影子，不是依赖）
- **配置表（控制面）特殊**：`get_config` 优先读飞书——《配置表》在飞书里
  直接改「值」，下一轮扫描的阈值/回看/关键词就按新值跑；飞书不可达时
  回退主库。`set_config`（看板/API 改）双写两边，永远一致。

初始化容错：飞书凭证错/网络不通时，镜像降级关闭，服务照常起（只写本地），
日志里说明原因。这样 .env 配错不会把整个服务拖死。

配置表的三路合并（**重点**）：`_backfill` 早期把「主库配置」无条件推给飞书，
结果是「你在飞书《配置表》里把阈值改成 5 → 生效 → 重启 → 又被本地 20 覆盖」，
等于只在重启前有效。现在改成按 `data/config_sync.json` 里记的**上次同步快照**
做三路合并，两边的修改都不会被对方的旧值顶掉：

| 场景 | 判定 | 结果 |
|---|---|---|
| 两边一致 | — | 不动 |
| 本地==快照，飞书变了 | 只有飞书改过 | **飞书胜**（同步回本地）|
| 飞书==快照，本地变了 | 只有本地改过 | **本地胜**（推给飞书）|
| 两边都变 / 没快照 | 冲突 | **飞书胜**（《配置表》是对外声明的控制面）|

快照文件是**派生态**：删掉它只会退化成「冲突时以飞书为准」，不会丢数据，
下次成功同步会重新写出来。

账号表**不做三路合并**，仍然是「本地为准、回填覆盖」。理由：账号决定「扫谁」，
属于必须单一权威的安全属性，不允许两边各说各话；加账号走看板的账号页。

历史回填：`init` 时把主库已有的账号/配置/快照/增量/轮次/告警/评论
全部灌进飞书（全部走幂等写，重复执行无副作用）——你打开多维表格
第一眼就是全量数据，不是从零开始。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 上次成功同步的配置快照。派生态、可删——删了只是退化成「冲突时飞书为准」。
DEFAULT_SYNC_PATH = Path("data/config_sync.json")


def _mirror_failure(op: str, exc: Exception) -> None:
    # 镜像失败是「降级」不是「故障」：告警一次，不抛
    log.warning("飞书镜像写入失败（%s）：%s —— 本轮数据只在本地，不影响扫描", op, exc)


def _norm(value: Any) -> str | None:
    """比较用归一化：int 20 与 str "20" 视为同一个值。"""
    return None if value is None else str(value)


class MirrorStorage:
    backend = "mirror"

    def __init__(self, primary: Any, secondary: Any | None,
                 sync_path: Path | str | None = None) -> None:
        self.primary = primary
        # secondary 可能为 None（构造失败时直接降级为纯本地）
        self.secondary = secondary
        self.sync_path = Path(sync_path) if sync_path is not None else DEFAULT_SYNC_PATH

    # ------------------------------------------------------------ 生命周期

    async def init(self) -> None:
        await self.primary.init()
        if self.secondary is None:
            log.warning("飞书镜像未启用（构造失败），存储只走本地 SQLite")
            return
        try:
            await self.secondary.init()
            await self._backfill()
            log.info("双写存储就绪：本地 SQLite（权威）+ 飞书多维表格（镜像）")
        except Exception as exc:  # noqa: BLE001 - 凭证错/网络不通都不该拖死服务
            log.warning("飞书镜像初始化失败，降级为纯本地存储：%s", exc)
            try:
                await self.secondary.close()
            except Exception:  # noqa: BLE001
                pass
            self.secondary = None

    async def close(self) -> None:
        await self.primary.close()
        if self.secondary is not None:
            await self.secondary.close()

    # ------------------------------------------------------------ 回填

    async def _backfill(self) -> None:
        """把主库已有数据灌进飞书。全部幂等写，可重复执行。"""
        # 先喊一声：这一步要跟飞书逐表核对（几十次 API 调用），实测 60 秒上下。
        # 不打这行的话，启动日志里「飞书存储就绪」和「回填完成」之间会空一大段，
        # 看着像卡死——2026-09-16 就因此误判过一次。
        log.info("历史回填中……（逐表核对本地与飞书，通常 1 分钟内，数据越多越久）")
        # 1) 账号（含停用的）
        for acc in await self.primary.list_accounts(only_enabled=False):
            try:
                await self.secondary.upsert_account(acc)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"账号 {acc.get('name')}", exc)
                return

        # 2) 配置：三路合并，两边的修改都不会被对方旧值顶掉
        await self._reconcile_config()

        # 3) 历史：快照/增量/轮次/告警/评论
        snaps = await self.primary.list_snapshots(limit=2000)
        if snaps:
            await self.secondary.batch_create_snapshots(snaps)
        deltas = await self.primary.list_deltas(limit=2000)
        if deltas:
            await self.secondary.create_deltas(deltas)
        hits = await self.primary.list_comment_hits(status=None, limit=2000)
        if hits:
            await self.secondary.upsert_comment_hits(hits)
            # 已确认过的评论把状态也带过去。按 comment_id 成组，不按 thread_id——
            # 命中记录的去重键只含 comment_id（一条评论一生只落一次表）。
            decided: dict[str, str] = {
                h["comment_id"]: h["status"]
                for h in hits
                if h.get("comment_id") and h.get("status") and h["status"] != "pending"
            }
            if decided:
                try:
                    await self.secondary.decide_comment_hits(decided)
                except Exception as exc:  # noqa: BLE001
                    _mirror_failure("评论状态回填", exc)
        alerts = await self.primary.list_alerts(limit=2000)
        for a in alerts:
            try:
                await self.secondary.try_log_alert(
                    a["run_id"], a["video_id"], a["channel"],
                    a.get("payload", ""), a.get("kind", "alert"),
                )
            except Exception as exc:  # noqa: BLE001
                _mirror_failure("推送日志回填", exc)
        rounds = await self.primary.list_rounds(limit=2000)
        for r in rounds:
            try:
                await self.secondary.create_round(
                    {
                        "run_id": r["run_id"], "trigger_type": r.get("trigger_type", "manual"),
                        "started_at": r.get("started_at") or "",
                        "account_count": r.get("account_count", 0),
                        "video_count": r.get("video_count", 0),
                        "source": r.get("source", ""), "alert_count": r.get("alert_count", 0),
                        "error_count": r.get("error_count", 0),
                        "thread_ids": r.get("thread_ids", ""), "note": r.get("note", ""),
                    }
                )
                if r.get("finished_at"):
                    await self.secondary.finish_round(
                        r["run_id"],
                        finished_at=r["finished_at"],
                        account_count=r.get("account_count", 0),
                        video_count=r.get("video_count", 0),
                        source=r.get("source", ""),
                        alert_count=r.get("alert_count", 0),
                        error_count=r.get("error_count", 0),
                        thread_ids=r.get("thread_ids", ""),
                        note=r.get("note", ""),
                    )
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"轮次 {r['run_id']}", exc)

        log.info(
            "历史回填完成：账号/配置对齐，快照 %d、增量 %d、评论 %d、告警 %d、轮次 %d",
            len(snaps), len(deltas), len(hits), len(alerts), len(rounds),
        )

    # ------------------------------------------------------------ 配置表三路合并

    def _load_snapshot(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.sync_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 - 快照坏了不该拦住启动
            log.warning("配置同步快照读取失败（%s），本次按「冲突时飞书为准」处理", exc)
            return {}

    def _save_snapshot(self, merged: dict[str, Any]) -> None:
        try:
            self.sync_path.parent.mkdir(parents=True, exist_ok=True)
            # 临时文件 + 替换，避免写一半留下坏 JSON
            tmp = self.sync_path.with_suffix(self.sync_path.suffix + ".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.sync_path)
        except Exception as exc:  # noqa: BLE001 - 快照写不进去最多下次退化，不影响业务
            log.warning("配置同步快照写入失败：%s", exc)

    async def _reconcile_config(self) -> None:
        """把本地与飞书的《配置表》合并成一份，并记录同步快照。

        规则见模块 docstring 的表。核心是「谁改过谁说了算」：
        只看「值是否等于上次同步快照」就能判断出是哪一边动过。
        """
        local_items = {i["key"]: i for i in await self.primary.list_config()}
        remote_items = {i["key"]: i for i in await self.secondary.list_config()}
        snap = self._load_snapshot()

        merged: dict[str, Any] = {}
        for key in sorted(set(local_items) | set(remote_items)):
            l_item, r_item = local_items.get(key), remote_items.get(key)
            l_val = l_item["value"] if l_item else None
            r_val = r_item["value"] if r_item else None
            s_val = snap.get(key)

            if _norm(l_val) == _norm(r_val):
                merged[key] = l_val if l_item else r_val      # 一致：不动
                continue

            if r_item is None:                                 # 只在本地有
                await self._push_to_remote(l_item)
                merged[key] = l_val
            elif l_item is None:                               # 只在飞书有
                await self.primary.set_config(key, r_val)
                merged[key] = r_val
            elif _norm(l_val) == _norm(s_val):                 # 只有飞书改过
                await self.primary.set_config(key, r_val)
                merged[key] = r_val
                log.info("配置 %s：飞书侧改成了 %s，同步回本地", key, r_val)
            elif _norm(r_val) == _norm(s_val):                 # 只有本地改过
                await self._push_to_remote(l_item)
                merged[key] = l_val
                log.info("配置 %s：本地侧改成了 %s，同步到飞书", key, l_val)
            else:                                              # 两边都改过 / 没有快照
                await self.primary.set_config(key, r_val)
                merged[key] = r_val
                log.warning("配置 %s 两边不一致（本地 %s / 飞书 %s），以飞书为准", key, l_val, r_val)

        self._save_snapshot(merged)

    async def _push_to_remote(self, item: dict) -> None:
        """推一条配置到飞书。优先 put_config（带 类型/分类/说明）。"""
        put = getattr(self.secondary, "put_config", None)
        if put is not None:
            await put(item)
        else:
            await self.secondary.set_config(item["key"], item["value"])

    # ------------------------------------------------------------ 控制面（配置表）

    async def list_config(self) -> list[dict]:
        # 看板展示优先飞书（用户在飞书里改完立刻能看到），失败回退主库
        if self.secondary is not None:
            try:
                return await self.secondary.list_config()
            except Exception as exc:  # noqa: BLE001
                _mirror_failure("读配置列表", exc)
        return await self.primary.list_config()

    async def get_config(self) -> dict[str, Any]:
        if self.secondary is not None:
            try:
                return await self.secondary.get_config()
            except Exception as exc:  # noqa: BLE001
                _mirror_failure("读配置", exc)
        return await self.primary.get_config()

    async def set_config(self, key: str, value: Any) -> None:
        await self.primary.set_config(key, value)
        synced = True
        if self.secondary is not None:
            try:
                await self.secondary.set_config(key, value)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"写配置 {key}", exc)
                synced = False
        # ⚠ 只有两边都写成功才更新快照。
        # 如果飞书写失败还把新值记进快照，下次启动三路合并会看到
        # 「本地==快照、飞书是旧值」→ 判定成「只有飞书改过」→ 把用户的本地修改回滚掉。
        if synced:
            snap = self._load_snapshot()
            snap[key] = value
            self._save_snapshot(snap)

    # ------------------------------------------------------------ 读：全走主库

    async def list_accounts(self, only_enabled: bool = True) -> list[dict]:
        return await self.primary.list_accounts(only_enabled=only_enabled)

    async def exists_snapshot(self, idem_key: str) -> bool:
        return await self.primary.exists_snapshot(idem_key)

    async def previous_snapshot(self, video_id: str, run_id: str) -> dict | None:
        return await self.primary.previous_snapshot(video_id, run_id)

    async def list_snapshots(self, limit: int = 100, video_id: str | None = None) -> list[dict]:
        return await self.primary.list_snapshots(limit=limit, video_id=video_id)

    async def exists_delta(self, idem_key: str) -> bool:
        return await self.primary.exists_delta(idem_key)

    async def list_deltas(self, limit: int = 100, only_alert: bool = False) -> list[dict]:
        return await self.primary.list_deltas(limit=limit, only_alert=only_alert)

    async def list_comment_hits(self, status: str | None = None, limit: int = 100) -> list[dict]:
        return await self.primary.list_comment_hits(status=status, limit=limit)

    async def list_rounds(self, limit: int = 50) -> list[dict]:
        return await self.primary.list_rounds(limit=limit)

    async def stats(self) -> dict:
        return await self.primary.stats()

    # ------------------------------------------------------------ 写：主库 + 镜像

    async def upsert_account(self, acc: dict) -> None:
        await self.primary.upsert_account(acc)
        if self.secondary is not None:
            try:
                await self.secondary.upsert_account(acc)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"账号 {acc.get('name')}", exc)

    async def update_account(self, old_sec_uid: str, fields: dict) -> bool:
        ok = await self.primary.update_account(old_sec_uid, fields)
        if self.secondary is not None:
            try:
                await self.secondary.update_account(old_sec_uid, fields)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"改账号 {old_sec_uid[:16]}", exc)
        return ok

    async def delete_account(self, sec_uid: str) -> None:
        await self.primary.delete_account(sec_uid)
        if self.secondary is not None:
            try:
                await self.secondary.delete_account(sec_uid)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"删账号 {sec_uid[:16]}", exc)

    async def batch_create_snapshots(self, rows: list[dict]) -> int:
        n = await self.primary.batch_create_snapshots(rows)
        if self.secondary is not None and rows:
            try:
                await self.secondary.batch_create_snapshots(rows)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"快照 {len(rows)} 条", exc)
        return n

    async def create_deltas(self, rows: list[dict]) -> int:
        n = await self.primary.create_deltas(rows)
        if self.secondary is not None and rows:
            try:
                await self.secondary.create_deltas(rows)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"增量 {len(rows)} 条", exc)
        return n

    async def mark_alerted(self, idem_keys: list[str], when: str) -> int:
        n = await self.primary.mark_alerted(idem_keys, when)
        if self.secondary is not None and idem_keys:
            try:
                await self.secondary.mark_alerted(idem_keys, when)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"标记已推送 {len(idem_keys)} 条", exc)
        return n

    async def upsert_comment_hits(self, rows: list[dict]) -> int:
        n = await self.primary.upsert_comment_hits(rows)
        if self.secondary is not None and rows:
            try:
                await self.secondary.upsert_comment_hits(rows)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"评论命中 {len(rows)} 条", exc)
        return n

    async def decide_comment_hits(self, decisions: dict[str, str]) -> int:
        n = await self.primary.decide_comment_hits(decisions)
        if self.secondary is not None and decisions:
            try:
                await self.secondary.decide_comment_hits(decisions)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"评论确认 {len(decisions)} 条", exc)
        return n

    async def create_round(self, row: dict) -> None:
        await self.primary.create_round(row)
        if self.secondary is not None:
            try:
                await self.secondary.create_round(row)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"轮次 {row.get('run_id')}", exc)

    async def finish_round(self, run_id: str, **fields: Any) -> None:
        await self.primary.finish_round(run_id, **fields)
        if self.secondary is not None:
            try:
                await self.secondary.finish_round(run_id, **fields)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"轮次收尾 {run_id}", exc)

    async def try_log_alert(
        self, run_id: str, video_id: str, channel: str, payload: str, kind: str = "alert"
    ) -> bool:
        logged = await self.primary.try_log_alert(run_id, video_id, channel, payload, kind)
        if self.secondary is not None and logged:
            try:
                await self.secondary.try_log_alert(run_id, video_id, channel, payload, kind)
            except Exception as exc:  # noqa: BLE001
                _mirror_failure(f"推送日志 {kind}/{video_id}", exc)
        return logged

    async def list_alerts(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        # 读全走本地（authoritative）
        return await self.primary.list_alerts(limit, kind)


__all__ = ["MirrorStorage"]
