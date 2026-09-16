"""全局配置。

这里**只放基础设施参数**（路径、端口、后端选择）——改了要重启。
业务参数（阈值、扫描间隔、关键词）一律走数据库里的《配置表》，改表即生效。

这条边界是刻意的：配置表是**控制面**，它同时驱动调度（触发层）与阈值判定（编排层），
不属于任何一层存储。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 服务 ----------
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    debug: bool = True

    # ---------- 业务数据存储 ----------
    storage_backend: str = "sqlite"          # sqlite | feishu
    sqlite_path: str = "data/app.db"

    # ---------- Graph 执行状态（与业务存储是两条独立支线）----------
    checkpoint_backend: str = "sqlite"
    checkpoint_path: str = "data/checkpoints.db"

    # ---------- 数据源降级链 ----------
    provider_chain: str = "thirdparty,browser,mock"
    lookback_days: int = 3

    # ---------- browser provider（Playwright + 本机 Chrome）----------
    # 指向一个"已登录过抖音网页版"的 Chrome 用户数据目录。
    # 首次使用：BROWSER_HEADLESS=false 起一次 → 扫码登录 → 关闭 → 改回 true。
    # 留空 = browser provider 不可用，降级链自动跳到下一档。
    browser_user_data_dir: str = ""
    browser_headless: bool = True
    # 用本机已装的浏览器而不是 playwright 自带的 Chromium（省 ~170MB 下载，
    # 且真实浏览器指纹更不容易被识别）。可选值：chrome / msedge。
    # 本机检测：有 Chrome 用 chrome，否则 Edge 用 msedge。
    browser_channel: str = "msedge"
    # 账号与账号之间的停顿秒数（风控：低频、可解释）。
    browser_account_delay: float = 8.0
    # 每个账号最多往下滚多少屏（防失控：一屏约 10+ 条视频）。
    browser_max_scrolls: int = 10

    # ---------- 告警 ----------
    # local | feishu_im（直发你的飞书私聊）| feishu_card（群 webhook）
    # 可组合：`local+feishu_im` = 本地看板记一笔 + 同时推飞书
    notifier_backend: str = "local"
    feishu_webhook: str = ""
    # 告警私聊对象。留空时自动取「应用创建者」的 open_id（通常就是你本人）。
    # 想发给别人/群：填对应 id，并把下面 type 改成 user_id / email / chat_id。
    feishu_notify_receive_id: str = ""
    feishu_notify_receive_id_type: str = "open_id"

    # ---------- 调度 ----------
    scheduler_enabled: bool = True
    timezone: str = "Asia/Shanghai"

    # ---------- 评论子图 ----------
    enable_comment_graph: bool = True
    # 扫描收尾时给在飞的评论子图留的收尾窗口（秒）。子图跑到 interrupt 挂起即算收尾，
    # 正常只要几十毫秒，所以这个值只在子系统卡死时才会被用满——它是**响应时间的上界**，
    # 别设大。设 0 可关闭等待（纯 fire-and-forget）。
    comment_drain_timeout: float = 2.0
    llm_enabled: bool = False
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # ---------- 飞书（storage_backend=feishu 时必填）----------
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_app_token: str = ""
    feishu_table_accounts: str = "监控账号表"
    feishu_table_snapshots: str = "视频快照表"
    feishu_table_deltas: str = "增量与告警表"
    feishu_table_comments: str = "评论命中表"
    feishu_table_config: str = "配置表"
    feishu_table_rounds: str = "扫描轮次表"

    # ---------- 派生属性 ----------
    @property
    def provider_chain_list(self) -> list[str]:
        return [p.strip() for p in self.provider_chain.split(",") if p.strip()]

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def db_file(self) -> Path:
        return self._resolve(self.sqlite_path)

    @property
    def checkpoint_file(self) -> Path:
        return self._resolve(self.checkpoint_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
