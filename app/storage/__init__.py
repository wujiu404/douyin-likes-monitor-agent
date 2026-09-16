"""业务存储层。

- 默认 `sqlite`：本地零依赖、迁移友好、不受外部额度限制
- `feishu`：多维表格为唯一存储（正式演示用，可视化效果最好）
- `sqlite+feishu`：**双写**——SQLite 权威 + 飞书多维表格实时镜像，
  配置表两边改都生效（详见 `mirror.py` 模块注释）

上层节点只依赖 `StorageProvider` 协议，不感知具体后端。
"""
from __future__ import annotations

from app.config import Settings
from app.storage.base import StorageProvider


def build_storage(settings: Settings) -> StorageProvider:
    backend = (settings.storage_backend or "sqlite").lower()

    if backend in ("sqlite+feishu", "mirror"):
        # 双写：构造失败（凭证缺失）时自动降级纯本地，不让服务起不来
        from app.storage.mirror import MirrorStorage
        from app.storage.sqlite import SqliteStorage

        primary = SqliteStorage(settings.db_file)
        try:
            from app.storage.feishu import FeishuStorage

            secondary = FeishuStorage(settings, seed_defaults=False)
        except Exception as exc:  # noqa: BLE001 - 凭证缺失时降级
            import logging

            logging.getLogger(__name__).warning("飞书镜像不可用（%s），只走本地存储", exc)
            secondary = None
        return MirrorStorage(primary, secondary)

    if backend == "feishu":
        from app.storage.feishu import FeishuStorage

        return FeishuStorage(settings)

    from app.storage.sqlite import SqliteStorage

    return SqliteStorage(settings.db_file)


__all__ = ["StorageProvider", "build_storage"]
