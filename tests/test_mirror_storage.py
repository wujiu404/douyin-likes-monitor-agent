"""MirrorStorage 双写语义测试（离线：secondary 用假实现模拟飞书）。

钉住四类行为：
1. 写操作双写：主库成功 + 镜像成功 → 两边都有
2. 镜像失败不扩散：secondary 抛异常，主库照写、方法照常返回
3. 配置控制面：get_config 优先 secondary（飞书改了就生效），secondary 挂了回退主库
4. 配置表三路合并：**两边改都不丢**——飞书改的不会被重启覆盖，本地改的会推到飞书

⚠ 每个用例都必须传 `sync_path`（tmp_path 下），否则会写到项目里的
`data/config_sync.json`，用例之间互相污染。
"""
from __future__ import annotations

import json

import pytest

from app.storage.mirror import MirrorStorage


class FakeSecondary:
    """记调用的假飞书后端；可注入故障。"""

    backend = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail = False  # 打开后所有方法抛异常
        self.config: dict = {}
        self.config_meta: dict = {}

    async def init(self) -> None:
        self.calls.append(("init",))

    async def close(self) -> None:
        pass

    def _maybe_fail(self, op):
        self.calls.append(op)
        if self.fail:
            raise RuntimeError("飞书炸了")

    async def get_config(self):
        self._maybe_fail(("get_config",))
        return dict(self.config)

    async def list_config(self):
        self._maybe_fail(("list_config",))
        return [
            {"key": k, "value": v, "type": "int", "scope": "阈值", "note": "n", "updated_at": ""}
            for k, v in self.config.items()
        ]

    async def set_config(self, key, value):
        self._maybe_fail(("set_config", key, value))
        self.config[key] = value

    async def put_config(self, item):
        # 飞书实现的「整条写」：值和 类型/分类/说明 一起落
        self._maybe_fail(("put_config", item["key"]))
        self.config[item["key"]] = item["value"]
        self.config_meta[item["key"]] = item

    async def list_accounts(self, only_enabled=True):
        return []

    async def upsert_account(self, acc):
        self._maybe_fail(("upsert_account", acc.get("name")))

    async def delete_account(self, sec_uid):
        self._maybe_fail(("delete_account", sec_uid))

    async def batch_create_snapshots(self, rows):
        self._maybe_fail(("snapshots", len(rows)))
        return len(rows)

    async def create_deltas(self, rows):
        self._maybe_fail(("deltas", len(rows)))
        return len(rows)

    async def mark_alerted(self, keys, when):
        self._maybe_fail(("mark_alerted", len(keys)))
        return len(keys)

    async def upsert_comment_hits(self, rows):
        self._maybe_fail(("comments", len(rows)))
        return len(rows)

    async def decide_comment_hits(self, thread_id, decisions):
        self._maybe_fail(("decide", thread_id))
        return len(decisions)

    async def create_round(self, row):
        self._maybe_fail(("create_round", row.get("run_id")))

    async def finish_round(self, run_id, **fields):
        self._maybe_fail(("finish_round", run_id))

    async def try_log_alert(self, run_id, video_id, channel, payload, kind="alert"):
        self._maybe_fail(("alert", video_id))
        return True


class FakePrimary:
    """极简主库：只实现被测方法，独立计数。"""

    backend = "fake-primary"

    def __init__(self) -> None:
        self.snapshots: list[dict] = []
        self.config: dict = {"threshold": "20"}
        self.alerts: list[tuple] = []

    async def init(self) -> None: ...
    async def close(self) -> None: ...

    async def list_config(self):
        return [
            {"key": k, "value": v, "type": "int", "scope": "阈值",
             "note": "点赞增量超过它就告警", "updated_at": "2026-01-01T00:00:00+00:00"}
            for k, v in self.config.items()
        ]

    async def get_config(self):
        return dict(self.config)

    async def set_config(self, key, value):
        self.config[key] = str(value)

    async def list_accounts(self, only_enabled=True):
        return []

    async def batch_create_snapshots(self, rows):
        self.snapshots.extend(rows)
        return len(rows)

    async def create_deltas(self, rows):
        return len(rows)

    async def try_log_alert(self, run_id, video_id, channel, payload, kind="alert"):
        self.alerts.append((run_id, video_id, channel))
        return True

    async def list_snapshots(self, limit=100, video_id=None):
        return self.snapshots[:limit]

    async def list_deltas(self, limit=100, only_alert=False):
        return []

    async def list_comment_hits(self, status=None, limit=100):
        return []

    async def list_alerts(self, limit=50):
        return []

    async def list_rounds(self, limit=50):
        return []

    async def upsert_account(self, acc): ...
    async def update_account(self, old_sec_uid, fields): return True
    async def delete_account(self, sec_uid): ...
    async def mark_alerted(self, keys, when): return len(keys)
    async def upsert_comment_hits(self, rows): return len(rows)
    async def decide_comment_hits(self, thread_id, decisions): return len(decisions)
    async def create_round(self, row): ...
    async def finish_round(self, run_id, **fields): ...
    async def stats(self): return {}


@pytest.fixture
def mirror(tmp_path):
    """返回 (make, primary, secondary)。

    `make()` 每次都指向**同一个**快照文件——模拟「同一台机器上重启服务」；
    若每次换路径，三路合并拿不到快照，测试会走「冲突」分支而失去意义。
    """
    primary = FakePrimary()
    secondary = FakeSecondary()
    sync_path = tmp_path / "config_sync.json"

    def make() -> MirrorStorage:
        return MirrorStorage(primary, secondary, sync_path=sync_path)

    return make, primary, secondary


# ================================================================ 基本双写语义

async def test_writes_go_to_both(mirror) -> None:
    make, primary, secondary = mirror
    m = make()
    await m.init()

    n = await m.batch_create_snapshots([{"idem_key": "k1", "video_id": "v1"}])
    assert n == 1
    assert len(primary.snapshots) == 1
    assert ("snapshots", 1) in secondary.calls

    logged = await m.try_log_alert("R1", "v1", "local", "x")
    assert logged is True
    assert ("alert", "v1") in secondary.calls


async def test_mirror_failure_does_not_break_primary(mirror) -> None:
    make, primary, secondary = mirror
    secondary.fail = True  # 飞书全挂
    m = make()
    await m.init()

    n = await m.batch_create_snapshots([{"idem_key": "k2", "video_id": "v2"}])
    assert n == 1
    assert len(primary.snapshots) == 1

    logged = await m.try_log_alert("R2", "v2", "local", "x")
    assert logged is True
    assert len(primary.alerts) == 1


async def test_config_reads_prefer_secondary(mirror) -> None:
    make, primary, secondary = mirror
    m = make()
    await m.init()

    secondary.config["threshold"] = "5"
    cfg = await m.get_config()
    assert cfg["threshold"] == "5", "飞书里改的阈值应该直接生效"

    await m.set_config("scan_mode", "interval")
    assert primary.config["scan_mode"] == "interval"
    assert secondary.config["scan_mode"] == "interval"


async def test_config_falls_back_to_primary_when_secondary_down(mirror) -> None:
    make, primary, secondary = mirror
    m = make()
    await m.init()
    secondary.fail = True

    cfg = await m.get_config()
    assert cfg["threshold"] == "20", "飞书不可达时回退主库"


async def test_none_secondary_is_pure_local() -> None:
    primary = FakePrimary()
    m = MirrorStorage(primary, None, sync_path=None)
    await m.init()  # 不抛
    n = await m.batch_create_snapshots([{"idem_key": "k", "video_id": "v"}])
    assert n == 1
    cfg = await m.get_config()
    assert cfg["threshold"] == "20"


# ================================================================ 配置表三路合并

async def test_config_meta_is_carried_to_mirror(mirror) -> None:
    """回填配置时必须带上 类型/分类/说明。

    踩过的坑：早期回填走 set_config(key, value)，飞书《配置表》里
    `threshold` 只剩一个数字，说明列全空——而这张表就是用户改阈值/
    调度时间的控制面板，没说明等于没法用。
    """
    make, primary, secondary = mirror
    m = make()
    await m.init()

    item = secondary.config_meta["threshold"]
    assert item["type"] == "int"
    assert item["scope"] == "阈值"
    assert item["note"] == "点赞增量超过它就告警"
    assert item["value"] == "20"


async def test_feishu_config_edit_survives_restart(mirror) -> None:
    """★ 核心：在飞书《配置表》里改阈值，重启后不能被本地旧值覆盖。

    回归背景：`_backfill` 早期无条件「以主库为准」，于是
    「飞书改 5 → 生效 → 重启 → 又被本地 20 覆盖」，等于只在重启前有效。
    """
    make, primary, secondary = mirror
    await make().init()                      # 第一次启动，写下同步快照
    secondary.config["threshold"] = "5"      # 用户在飞书多维表格里手改

    await make().init()                      # 重启
    assert primary.config["threshold"] == "5", "飞书侧的修改必须同步回本地"


async def test_local_config_edit_pushes_to_feishu(mirror) -> None:
    """看板/API 改的配置，重启后要推给飞书（不被飞书旧值顶回来）。"""
    make, primary, secondary = mirror
    await make().init()
    primary.config["threshold"] = "7"        # 只看板改，飞书没写（比如当时飞书挂了）

    await make().init()
    assert secondary.config["threshold"] == "7", "本地侧的修改必须同步到飞书"


async def test_conflict_without_snapshot_prefers_feishu(mirror) -> None:
    """没有快照（首次接入 / 快照被删）时两边不一致 → 以《配置表》控制面为准。"""
    make, primary, secondary = mirror
    primary.config["threshold"] = "20"
    secondary.config["threshold"] = "5"
    await make().init()
    assert primary.config["threshold"] == "5"


async def test_snapshot_written_and_reusable(mirror, tmp_path) -> None:
    make, primary, secondary = mirror
    m = make()
    await m.init()
    snap_file = m.sync_path
    assert snap_file.exists()
    assert json.loads(snap_file.read_text(encoding="utf-8"))["threshold"] == "20"


async def test_failed_mirror_write_does_not_poison_snapshot(mirror) -> None:
    """飞书写失败时**不能**更新快照，否则重启会把用户刚改的本地值回滚掉。"""
    make, primary, secondary = mirror
    m = make()
    await m.init()

    secondary.fail = True
    await m.set_config("threshold", "9")     # 本地成功、飞书失败
    assert primary.config["threshold"] == "9"

    secondary.fail = False
    await make().init()                      # 重启，飞书恢复
    assert primary.config["threshold"] == "9", "本地那次修改不该被飞书旧值顶掉"
    assert secondary.config["threshold"] == "9", "而且应该补写到飞书"
