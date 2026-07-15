"""
多实例管理器 - 管理多个机器人实例

每个实例拥有:
- 独立的配置 (InstanceConfig)
- 独立的数据库文件 ({instance_id}_data.db)
- 独立的消息管道 (MessagePipeline)
- 独立的状态

支持:
- 创建 / 启动 / 停止实例
- 查询实例状态
- 配置热加载 (reload_config)
- 从主库恢复已注册实例 (load_instances)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select

from config.instance_config import InstanceConfig
from config.settings import settings
from core.message_pipeline import MessagePipeline, SendCallback
from database.db_manager import DatabaseManager
from database.migrations import (
    create_instance_tables,
    seed_default_data,
    upsert_instance_record,
)
from database.models import Instance, InstanceStatus, TaskLog, TaskStatus


@dataclass
class InstanceRuntime:
    """运行时实例信息"""
    instance_id: str
    config: InstanceConfig
    pipeline: MessagePipeline
    status: str = InstanceStatus.STOPPED.value
    started_at: Optional[datetime] = None
    send_callback: Optional[SendCallback] = field(default=None, repr=False)

    @property
    def is_running(self) -> bool:
        return self.status == InstanceStatus.RUNNING.value


class InstanceManager:
    """多实例管理器"""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager: DatabaseManager = db_manager
        self._instances: dict[str, InstanceRuntime] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        logger.info("实例管理器已初始化")

    # ------------------------------------------------------------------
    # 创建 / 注册
    # ------------------------------------------------------------------
    async def create_instance(self, config: InstanceConfig) -> InstanceRuntime:
        """创建新实例 (初始化实例库 + 默认数据 + 持久化记录)"""
        async with self._lock:
            instance_id = config.instance_id
            if not instance_id:
                raise ValueError("instance_id 不能为空")
            if instance_id in self._instances:
                raise ValueError(f"实例已存在: {instance_id}")

            # 同步配置中的数据库路径
            config.db_path = settings.db_dir / DatabaseManager.instance_db_name(instance_id)

            # 初始化实例库与默认数据
            await create_instance_tables(self.db_manager, instance_id)
            await seed_default_data(self.db_manager, instance_id)

            # 持久化实例记录到主库
            await upsert_instance_record(
                self.db_manager,
                instance_id=instance_id,
                display_name=config.display_name or instance_id,
                wxid=config.wxid,
                status=InstanceStatus.STOPPED.value,
                config_json=config.model_dump_json(),
            )

            # 创建消息管道
            pipeline = MessagePipeline(config)
            runtime = InstanceRuntime(
                instance_id=instance_id,
                config=config,
                pipeline=pipeline,
            )
            self._instances[instance_id] = runtime
            logger.info(f"实例已创建: {instance_id} ({config.display_name})")
            return runtime

    async def load_instances(self) -> list[str]:
        """从主库恢复已注册实例 (status 保持为 stopped)"""
        loaded: list[str] = []
        async with self.db_manager.get_session(settings.main_db_name) as session:
            result = await session.execute(select(Instance))
            records = result.scalars().all()

        for record in records:
            instance_id = record.instance_id
            if instance_id in self._instances:
                continue
            config = self._build_config(record)
            config.db_path = settings.db_dir / DatabaseManager.instance_db_name(instance_id)
            # 确保实例库表存在
            await create_instance_tables(self.db_manager, instance_id)
            pipeline = MessagePipeline(config)
            runtime = InstanceRuntime(
                instance_id=instance_id,
                config=config,
                pipeline=pipeline,
                status=InstanceStatus.STOPPED.value,
            )
            self._instances[instance_id] = runtime
            loaded.append(instance_id)
            logger.info(f"已恢复实例: {instance_id} (status={record.status})")
        return loaded

    @staticmethod
    def _build_config(record: Instance) -> InstanceConfig:
        """从实例记录构建配置"""
        if record.config_json:
            try:
                return InstanceConfig.model_validate_json(record.config_json)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"解析实例配置失败, 使用默认配置: {record.instance_id} -> {e}")
        return InstanceConfig(
            instance_id=record.instance_id,
            display_name=record.display_name or record.instance_id,
            wxid=record.wxid or "",
        )

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    async def start_instance(self, instance_id: str) -> bool:
        """启动实例"""
        runtime = self._get_runtime(instance_id)
        if runtime is None:
            logger.warning(f"实例不存在: {instance_id}")
            return False
        if runtime.is_running:
            logger.warning(f"实例已在运行: {instance_id}")
            return True
        await runtime.pipeline.start()
        runtime.status = InstanceStatus.RUNNING.value
        runtime.started_at = datetime.now()
        await upsert_instance_record(
            self.db_manager,
            instance_id=instance_id,
            display_name=runtime.config.display_name,
            wxid=runtime.config.wxid,
            status=InstanceStatus.RUNNING.value,
            config_json=runtime.config.model_dump_json(),
        )
        await self.log_task(instance_id, "start", TaskStatus.SUCCESS, "实例已启动")
        logger.info(f"实例已启动: {instance_id}")
        return True

    async def stop_instance(self, instance_id: str) -> bool:
        """停止实例"""
        runtime = self._get_runtime(instance_id)
        if runtime is None:
            logger.warning(f"实例不存在: {instance_id}")
            return False
        if not runtime.is_running:
            logger.warning(f"实例未在运行: {instance_id}")
            return True
        await runtime.pipeline.stop()
        runtime.status = InstanceStatus.STOPPED.value
        runtime.started_at = None
        await upsert_instance_record(
            self.db_manager,
            instance_id=instance_id,
            display_name=runtime.config.display_name,
            wxid=runtime.config.wxid,
            status=InstanceStatus.STOPPED.value,
            config_json=runtime.config.model_dump_json(),
        )
        await self.log_task(instance_id, "stop", TaskStatus.SUCCESS, "实例已停止")
        logger.info(f"实例已停止: {instance_id}")
        return True

    async def stop_all(self) -> None:
        """停止所有运行中的实例"""
        async with self._lock:
            running = [iid for iid, rt in self._instances.items() if rt.is_running]
        for instance_id in running:
            try:
                await self.stop_instance(instance_id)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"停止实例异常: {instance_id} -> {e}")

    # ------------------------------------------------------------------
    # 热加载配置
    # ------------------------------------------------------------------
    async def reload_config(
        self,
        instance_id: str,
        new_config: Optional[InstanceConfig] = None,
    ) -> bool:
        """热加载实例配置 (不重启管道, 原地替换配置)"""
        runtime = self._get_runtime(instance_id)
        if runtime is None:
            logger.warning(f"实例不存在: {instance_id}")
            return False
        if new_config is None:
            new_config = runtime.config
        new_config.instance_id = instance_id
        new_config.db_path = settings.db_dir / DatabaseManager.instance_db_name(instance_id)
        # 原地替换管道配置 (热加载, 无需重启)
        runtime.config = new_config
        runtime.pipeline.config = new_config
        # 持久化
        await upsert_instance_record(
            self.db_manager,
            instance_id=instance_id,
            display_name=new_config.display_name,
            wxid=new_config.wxid,
            status=runtime.status,
            config_json=new_config.model_dump_json(),
        )
        await self.log_task(instance_id, "reload_config", TaskStatus.SUCCESS, "配置已热加载")
        logger.info(f"实例配置已热加载: {instance_id}")
        return True

    # ------------------------------------------------------------------
    # 发送回调注入
    # ------------------------------------------------------------------
    def set_send_callback(self, instance_id: str, callback: SendCallback) -> bool:
        """为实例注入发送回调 (由微信网络层提供)"""
        runtime = self._get_runtime(instance_id)
        if runtime is None:
            return False
        runtime.send_callback = callback
        runtime.pipeline.set_send_callback(callback)
        return True

    def get_pipeline(self, instance_id: str) -> Optional[MessagePipeline]:
        """获取实例的消息管道"""
        runtime = self._get_runtime(instance_id)
        return runtime.pipeline if runtime else None

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get_instance_status(self, instance_id: str) -> Optional[dict]:
        """获取单个实例状态"""
        runtime = self._get_runtime(instance_id)
        if runtime is None:
            return None
        return {
            "instance_id": runtime.instance_id,
            "display_name": runtime.config.display_name,
            "wxid": runtime.config.wxid,
            "status": runtime.status,
            "started_at": runtime.started_at.isoformat() if runtime.started_at else None,
            "pipeline": runtime.pipeline.get_status(),
            "has_send_callback": runtime.send_callback is not None,
        }

    def list_instances(self) -> list[dict]:
        """列出所有实例状态"""
        return [
            self.get_instance_status(iid)  # type: ignore[misc]
            for iid in self._instances
        ]

    def get_all_pipelines(self) -> dict[str, MessagePipeline]:
        """获取所有实例管道 (供引擎调度使用)"""
        return {iid: rt.pipeline for iid, rt in self._instances.items()}

    # ------------------------------------------------------------------
    # 任务日志
    # ------------------------------------------------------------------
    async def log_task(
        self,
        instance_id: str,
        task_type: str,
        status: str,
        result: str = "",
    ) -> None:
        """记录任务日志到实例库"""
        db_name = DatabaseManager.instance_db_name(instance_id)
        try:
            async with self.db_manager.get_session(db_name) as session:
                session.add(
                    TaskLog(
                        instance_id=instance_id,
                        task_type=task_type,
                        status=status,
                        result=result,
                    )
                )
                await session.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"记录任务日志失败: {instance_id} -> {e}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _get_runtime(self, instance_id: str) -> Optional[InstanceRuntime]:
        return self._instances.get(instance_id)

    @property
    def instance_count(self) -> int:
        return len(self._instances)

    @property
    def running_count(self) -> int:
        return sum(1 for rt in self._instances.values() if rt.is_running)


__all__ = ["InstanceManager", "InstanceRuntime"]
