"""调度器：从《配置表》读模式与时点，注册定时任务。

两种节奏都由配置表驱动，改表即生效：

| `scan_mode` | 用哪个配置 | 说明 |
|---|---|---|
| `cron`（默认） | `scan_cron_hours` | 每天固定时点跑，默认 12/18/22 点 |
| `interval` | `scan_interval_minutes` | 每 N 分钟跑一次，演示档用 3 分钟 |

`max_instances=1` + `coalesce=True`：上一轮没跑完就不叠加新轮次——
这和 `scan_service` 里那把进程内锁是同一道保险的两层。

⚠ `interval` 模式**只能短时开**。3 分钟一轮意味着每小时约 160 次外部调用，
长期常开会把外部额度打爆（详见 docs/04-存储与告警渠道选型.md）。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.coerce import as_int
from app.core.errors import ScanSkipped
from app.scan_service import ScanBusy, run_scan

log = logging.getLogger(__name__)

JOB_ID = "monitor-scan"


def _parse_hours(raw: str) -> list[int]:
    hours = []
    for chunk in str(raw).replace("，", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            h = int(chunk)
            if 0 <= h <= 23:
                hours.append(h)
    return hours or [12, 18, 22]


class ScanScheduler:
    def __init__(self, deps) -> None:
        self.deps = deps
        self.scheduler = AsyncIOScheduler(timezone=deps.settings.timezone)
        self.description = "未启动"

    async def reload(self) -> str:
        cfg = await self.deps.storage.get_config()
        self.scheduler.remove_all_jobs()

        mode = str(cfg.get("scan_mode", "cron")).lower()
        if mode == "interval":
            # 空值/非法值 → 默认 3 分钟；显式填 0 由 max(1, …) 夹到 1 分钟（间隔不能是 0）
            minutes = max(1, as_int(cfg.get("scan_interval_minutes"), 3))
            self.scheduler.add_job(
                self._tick,
                "interval",
                minutes=minutes,
                id=JOB_ID,
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            self.description = f"每 {minutes} 分钟"
        else:
            hours = _parse_hours(cfg.get("scan_cron_hours", "12,18,22"))
            self.scheduler.add_job(
                self._tick,
                "cron",
                hour=",".join(str(h) for h in hours),
                id=JOB_ID,
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            self.description = "每天 " + " / ".join(f"{h}:00" for h in hours)

        log.info("调度已重载：%s（模式=%s）", self.description, mode)
        return self.description

    async def _tick(self) -> None:
        try:
            # trigger 记的是**触发方式**（自动/手动），不是「哪种节奏」：interval 档自动跑的
            # 轮次同样记 cron —— 与 state.py 的 Literal["cron","manual"]、《扫描轮次表》
            # 字段口径一致。要判断当时是哪种节奏，看 started_at 与那一轮的档位，别读这列。
            result = await run_scan(self.deps, trigger="cron")
            log.info(
                "定时扫描完成：run_id=%s，视频 %s 条，告警 %s 条",
                result["run_id"], result["video_count"], result["alert_count"],
            )
        except ScanSkipped as exc:
            # 没有启用中的账号之类：正常状态，不用堆栈，别让日志看起来像出事
            log.warning("本轮跳过：%s", exc)
        except ScanBusy:
            log.warning("上一轮扫描还没结束，跳过本次触发（max_instances=1 生效）")
        except Exception:  # noqa: BLE001
            log.exception("定时扫描失败")
        finally:
            # 配置可能在飞书《配置表》里被人直接改过（双写模式下 get_config
            # 优先读飞书）——每轮收尾时重读一次，调度时间在下一轮前生效。
            # 放 finally：扫描失败也要重载，别让一次失败卡住配置更新。
            try:
                await self.reload()
            except Exception:  # noqa: BLE001
                log.warning("扫描后重载调度失败（配置源不可达？）")

    def start(self) -> None:
        self.scheduler.start()

    def snapshot(self) -> dict:
        """给看板/健康检查用的调度状态快照。

        暴露「下次什么时候跑」比只暴露「开着没」有用得多——
        演示时改完配置能立刻确认调度真的重载了。
        """
        job = self.scheduler.get_job(JOB_ID) if self.scheduler.running else None
        return {
            "enabled": True,
            "running": bool(self.scheduler.running),
            "description": self.description,
            "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
        }

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            log.info("调度已停止")
