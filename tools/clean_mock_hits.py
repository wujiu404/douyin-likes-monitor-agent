"""清理 mock 历史数据（本地 SQLite + 飞书多维表格）。

两种模式：

① 默认（只清命中/提醒，不动采集数据）
   - `comment_hits` 里的 mock 命中行（看板「评论确认」直接读这张表）
   - `alert_log` 里 `run_id='comment_review'` 的对应幂等记录
     —— 评论提醒的幂等键是 (comment_review, comment_id, channel)，**不含轮次**。
     只删命中行不删这些的话，重跑演示档时评论提醒会被幂等拦掉，一条卡片都收不到。

② `--purge-mock`（演示前重置：把 mock 痕迹全清掉）
   在①的基础上再清 `snapshots` / `deltas` 里 mock 的 video_id（`acc01_v01` 形态）
   以及它们对应的点赞告警记录。**为什么需要**：mock 的 video_id 是确定性的
   （`acc01_v01`…`acc02_v06`），跑过一轮演示档后就永久留在快照表里了。
   下次开演时这些视频不再算「首见」，第一轮扫描就会直接算出一大截累积增量、
   当场跳出告警，和「第 1 轮建基线、第 2 轮才告警」的讲法对不上；
   而且面试官点开《视频快照表》会看到 `acc01_v01` 这种假 ID 混在真实 aweme_id 里。
   清掉之后，演示档第一轮重新回到「全部首见、增量 0、不告警」。

两个后端都要清：本地是权威源（幂等以它为准），飞书是镜像
（镜像里留着已删记录会让对应字段静默少行——飞书侧的幂等索引是启动时建的）。

用法：
    python tools/clean_mock_hits.py                     # dry-run，只报数
    python tools/clean_mock_hits.py --apply             # 只清命中/提醒
    python tools/clean_mock_hits.py --purge-mock        # dry-run（含采集数据）
    python tools/clean_mock_hits.py --purge-mock --apply  # 演示前重置（真删）

⚠ 清完必须**重启服务**：飞书后端的幂等索引只在启动时从飞书重建。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "app.db"
BACKUP_DIR = ROOT / "data" / "backup"

# mock 判据：合成 video_id（acc01_v01）或 mock 评论命名（xxx_c01 / acc01_v01_c12）
MOCK_HIT = "(video_id LIKE 'acc%' OR comment_id GLOB '*_c[0-9][0-9]')"
MOCK_REVIEW_LOG = "run_id='comment_review' AND video_id GLOB '*_c[0-9][0-9]'"
MOCK_ALERT_LOG = "kind='alert' AND video_id LIKE 'acc%'"

# 白名单：真实采集到的那条命中，双保险（判据本身也不会命中它）
KEEP = {"comment_id": {"7685269575458472738"}}

_MOCK_CID = re.compile(r"_c\d{2,}$")


def _text(value: object) -> str:
    """飞书文本字段可能是 str / list[{'text':…}] / {'text':…}，统一成字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(v) for v in value)
    if isinstance(value, dict):
        return _text(value.get("text") or "")
    return str(value or "")


def _is_mock_video(video_id: str) -> bool:
    return (video_id or "").startswith("acc")


def _is_mock_hit(comment_id: str, video_id: str) -> bool:
    """与本地 SQL 判据等价：合成 video_id，或 mock 评论命名 `_cNN` 结尾。"""
    return _is_mock_video(video_id) or bool(_MOCK_CID.search(comment_id or ""))


def backup_sqlite() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst_path = BACKUP_DIR / f"app.db.before-clean-{ts}"
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    with dst:
        src.backup(dst)          # 用 SQLite 官方备份 API，WAL 里的数据也会一起快照
    dst.close()
    src.close()
    return dst_path


def dump_rows(con: sqlite3.Connection, sql: str, out: Path, params: tuple = ()) -> int:
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, params)]
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(rows)


# ---------------------------------------------------------------- 本地 SQLite
def _local_targets(purge: bool) -> list[tuple[str, str, str, tuple]]:
    """返回 [(说明, 表名, WHERE, params), …]。"""
    keep = tuple(KEEP["comment_id"])
    not_keep = f" AND comment_id NOT IN ({','.join('?' * len(keep))})"
    targets = [
        ("评论命中表 comment_hits", "comment_hits", MOCK_HIT + not_keep, keep),
        ("提醒幂等 alert_log(comment_review)", "alert_log", MOCK_REVIEW_LOG, ()),
    ]
    if purge:
        targets += [
            ("视频快照表 snapshots", "snapshots", "video_id LIKE 'acc%'", ()),
            ("增量表 deltas", "deltas", "video_id LIKE 'acc%'", ()),
            ("告警幂等 alert_log(alert)", "alert_log", MOCK_ALERT_LOG, ()),
        ]
    return targets


def local_clean(apply: bool, purge: bool) -> dict:
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    counts: dict[str, int] = {}
    try:
        targets = _local_targets(purge)
        for label, table, where, params in targets:
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
            key = f"{table}:{where.split(' ')[0]}"
            counts[key] = n
            print(f"  待删 {label:<36} {n} 行")

        if not apply:
            return {**counts, "applied": False}

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        for label, table, where, params in targets:
            dump_rows(con, f"SELECT * FROM {table} WHERE {where}",
                      BACKUP_DIR / f"deleted-{table}-{label.split()[0]}-{ts}.json", params)

        deleted = {}
        for label, table, where, params in targets:
            cur = con.execute(f"DELETE FROM {table} WHERE {where}", params)
            deleted[label] = cur.rowcount
            print(f"  ✓ 已删 {label:<36} {cur.rowcount} 行")
        con.commit()
        return {**deleted, "applied": True}
    finally:
        con.close()


# ---------------------------------------------------------------- 飞书多维表格
async def _feishu_purge_table(st, key: str, predicate, tag: str, apply: bool) -> int:
    """按 predicate(fields) 筛掉记录并批量删除。返回（将）删除的行数。"""
    table = st.tables[key]
    records, total = await st.client.list_records(table, page_size=500)
    victims = [r for r in records if predicate(r.get("fields", {}))]
    print(f"  {tag:<28} 共 {total} 行 → mock {len(victims)} 行")
    if not apply or not victims:
        return len(victims)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dump = BACKUP_DIR / f"deleted-feishu-{key}-{ts}.json"
    dump.write_text(json.dumps(victims, ensure_ascii=False, indent=2), encoding="utf-8")

    ids = [r.get("record_id") for r in victims if r.get("record_id")]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        await st.client.request(
            "POST",
            f"/bitable/v1/apps/{st.client.app_token}/tables/{table}/records/batch_delete",
            json={"records": chunk},
        )
    print(f"    ✓ 已删 {len(ids)} 行（原始行 → data/backup/{dump.name}）")
    return len(ids)


async def feishu_clean(apply: bool, purge: bool) -> dict:
    from app.config import get_settings
    from app.storage.feishu import FeishuStorage

    settings = get_settings()
    st = FeishuStorage(settings, seed_defaults=False)
    await st.init()
    result: dict[str, int] = {}
    try:
        def hit_pred(f: dict) -> bool:
            cid = _text(f.get("comment_id"))
            vid = _text(f.get("video_id"))
            return cid not in KEEP["comment_id"] and _is_mock_hit(cid, vid)

        def review_log_pred(f: dict) -> bool:
            rid = _text(f.get("run_id"))
            vid = _text(f.get("video_id"))
            return rid == "comment_review" and _is_mock_hit(vid, vid)

        result["comment_hits"] = await _feishu_purge_table(
            st, "comment_hits", hit_pred, "评论命中表", apply)
        result["alert_log/review"] = await _feishu_purge_table(
            st, "alert_log", review_log_pred, "推送日志表(评论幂等)", apply)

        if purge:
            def video_pred(f: dict) -> bool:
                return _is_mock_video(_text(f.get("video_id")))

            def alert_log_pred(f: dict) -> bool:
                return (_text(f.get("类型")) == "alert"
                        and _is_mock_video(_text(f.get("video_id"))))

            result["snapshots"] = await _feishu_purge_table(
                st, "snapshots", video_pred, "视频快照表", apply)
            result["deltas"] = await _feishu_purge_table(
                st, "deltas", video_pred, "增量与告警表", apply)
            result["alert_log/alert"] = await _feishu_purge_table(
                st, "alert_log", alert_log_pred, "推送日志表(点赞告警)", apply)
        return {**result, "applied": apply}
    finally:
        await st.close()


# ---------------------------------------------------------------- 校验
def verify() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    print("  comment_hits 剩余：", con.execute("SELECT COUNT(*) FROM comment_hits").fetchone()[0])
    for r in con.execute("SELECT comment_id, content, status FROM comment_hits"):
        print(f"    {r}")
    print("  snapshots 剩余：", con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
          "/ 其中 mock：",
          con.execute("SELECT COUNT(*) FROM snapshots WHERE video_id LIKE 'acc%'").fetchone()[0])
    print("  deltas    剩余：", con.execute("SELECT COUNT(*) FROM deltas").fetchone()[0],
          "/ 其中 mock：",
          con.execute("SELECT COUNT(*) FROM deltas WHERE video_id LIKE 'acc%'").fetchone()[0])
    print("  alert_log 剩余：", con.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0])
    print("    comment_review 剩余：",
          con.execute("SELECT COUNT(*) FROM alert_log WHERE run_id='comment_review'").fetchone()[0])
    print("    点赞告警 mock 剩余：",
          con.execute(f"SELECT COUNT(*) FROM alert_log WHERE {MOCK_ALERT_LOG}").fetchone()[0])
    con.close()


async def main(apply: bool, purge: bool) -> None:
    title = "清理 mock 命中/提醒" + ("＋采集数据（演示前重置）" if purge else "")
    print("=" * 68)
    print(f"{title}　{'（APPLY：真删）' if apply else '（dry-run：只报数）'}")
    print("=" * 68)

    if apply:
        dst = backup_sqlite()
        print(f"  ① 已备份本地库 → data/backup/{dst.name}  ({dst.stat().st_size / 1024:.0f} KB)")

    print("\n② 本地 SQLite")
    local = local_clean(apply, purge)

    print("\n③ 飞书多维表格")
    fei = await feishu_clean(apply, purge)

    if not apply:
        print("\n（加 --apply 真删）")
        return

    print("\n" + "=" * 68)
    print("清理后校验（本地）")
    print("=" * 68)
    verify()
    print(f"\n结果：{json.dumps({**local, **fei}, ensure_ascii=False)}")
    print("\n⚠ 记得重启服务：飞书的幂等索引只在启动时从飞书重建，不重启会静默跳过镜像写入。")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="真删（默认 dry-run）")
    p.add_argument("--purge-mock", action="store_true",
                   help="连采集数据一起清（快照/增量/点赞告警），让演示档第一轮回到「首见」")
    args = p.parse_args()
    asyncio.run(main(args.apply, args.purge_mock))
