"""
数据库基础设施 - 异步 SQLAlchemy 封装

提供所有模块共享的声明式基类(Base)与异步会话工厂(Database)。
每个机器人实例使用独立的 SQLite 数据库文件（对应原软件 data/db/{instance_id}_data.db）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""

    pass


class Database:
    """异步数据库管理器。

    封装异步引擎与会话工厂，并负责建表与资源释放。
    所有业务模块通过此类获取 ``AsyncSession`` 进行持久化操作。
    """

    def __init__(self, db_path: Path | str, echo: bool = False) -> None:
        self.db_path: Path = Path(db_path)
        # 确保父目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite 使用 aiosqlite 驱动；允许跨线程使用（asyncio 场景）
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}",
            echo=echo,
            future=True,
        )
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
        self._initialized: bool = False

    async def init(self) -> None:
        """根据已导入的所有 ORM 模型创建数据表（幂等）。"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._initialized = True
        logger.info(f"数据库初始化完成: {self.db_path}")

    def session(self) -> AsyncSession:
        """创建一个新的异步会话。调用方需使用 ``async with`` 管理生命周期。"""
        return self.session_factory()

    async def close(self) -> None:
        """释放引擎连接池资源。"""
        await self.engine.dispose()
        self._initialized = False
        logger.info(f"数据库已关闭: {self.db_path}")

    async def health_check(self) -> bool:
        """简单的健康检查：能否执行一条查询。"""
        try:
            async with self.session() as session:
                from sqlalchemy import text

                await session.execute(text("SELECT 1"))
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"数据库健康检查失败: {e}")
            return False
