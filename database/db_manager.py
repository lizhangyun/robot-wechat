"""
数据库管理器 - 基于 SQLAlchemy 2.0 async + aiosqlite

设计说明:
- 主库 (默认 data.db): 存储共享数据, 如 Instance 实例表
- 实例库 ({instance_id}_data.db): 每个机器人实例拥有独立数据库文件
- 每个数据库文件对应一个独立的 AsyncEngine 与 async_sessionmaker, 按需懒加载并缓存
- 通过 SQLAlchemy 事件钩子将 SQL 语句通过 loguru 输出
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from loguru import logger

from config.settings import settings
from database.models import (
    Base,
    INSTANCE_TABLES,
    MAIN_TABLES,
)


class DatabaseManager:
    """异步数据库管理器 (支持多实例)"""

    def __init__(
        self,
        db_dir: Optional[Path] = None,
        *,
        echo: bool = False,
        log_sql: bool = False,
    ) -> None:
        self.db_dir: Path = Path(db_dir) if db_dir else settings.db_dir
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._echo: bool = echo
        self._log_sql: bool = log_sql
        self._engines: dict[str, AsyncEngine] = {}
        self._session_factories: dict[str, async_sessionmaker[AsyncSession]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        logger.info(f"数据库管理器已初始化: db_dir={self.db_dir}")

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @staticmethod
    def instance_db_name(instance_id: str) -> str:
        """实例数据库文件名: {instance_id}_data.db"""
        return f"{instance_id}_data.db"

    def _db_path(self, db_name: str) -> Path:
        return self.db_dir / db_name

    def _db_url(self, db_name: str) -> str:
        # 使用 4 个斜杠表示绝对路径
        path = self._db_path(db_name).resolve()
        return f"sqlite+aiosqlite:///{path}"

    def _register_sql_logger(self, engine: AsyncEngine) -> None:
        """注册 SQL 日志钩子 (经 loguru 输出)"""
        if not self._log_sql:
            return

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):  # type: ignore[no-untyped-def]
            logger.debug(
                f"[SQL] {statement.strip()[:200]} | params={parameters}"
            )

    # ------------------------------------------------------------------
    # 引擎 / 会话管理
    # ------------------------------------------------------------------
    def get_engine(self, db_name: str) -> AsyncEngine:
        """获取 (或创建并缓存) 指定数据库的异步引擎"""
        if db_name in self._engines:
            return self._engines[db_name]
        engine = create_async_engine(
            self._db_url(db_name),
            echo=self._echo,
            future=True,
            # SQLite 不需要连接池
            pool_pre_ping=True,
        )
        self._register_sql_logger(engine)
        factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        self._engines[db_name] = engine
        self._session_factories[db_name] = factory
        logger.debug(f"已创建数据库引擎: {db_name}")
        return engine

    def get_session(self, db_name: str) -> AsyncSession:
        """获取异步会话 (建议配合 async with 使用)"""
        if db_name not in self._session_factories:
            self.get_engine(db_name)
        return self._session_factories[db_name]()

    async def create_tables(
        self, db_name: str, tables: Optional[list] = None
    ) -> None:
        """在指定数据库中创建表 (幂等)

        Args:
            db_name: 数据库文件名
            tables: 要创建的表对象列表, None 表示创建 Base 上所有表
        """
        engine = self.get_engine(db_name)
        table_objects = tables if tables is not None else None

        def _create(sync_conn) -> None:
            if table_objects is not None:
                Base.metadata.create_all(sync_conn, tables=table_objects)
            else:
                Base.metadata.create_all(sync_conn)

        async with engine.begin() as conn:
            await conn.run_sync(_create)
        logger.info(f"数据库表已就绪: {db_name} (tables={len(table_objects) if table_objects else 'all'})")

    # ------------------------------------------------------------------
    # 主库 / 实例库初始化
    # ------------------------------------------------------------------
    async def init_main_db(self) -> None:
        """初始化主库 (共享数据, 如 Instance 表)"""
        await self.create_tables(settings.main_db_name, MAIN_TABLES)

    async def init_instance_db(self, instance_id: str) -> None:
        """初始化实例库 ({instance_id}_data.db)"""
        db_name = self.instance_db_name(instance_id)
        await self.create_tables(db_name, INSTANCE_TABLES)

    async def init_db(self, instance_id: Optional[str] = None) -> None:
        """初始化主库 (以及可选的实例库)

        Args:
            instance_id: 若提供则同时初始化该实例库
        """
        async with self._lock:
            await self.init_main_db()
            if instance_id:
                await self.init_instance_db(instance_id)

    # ------------------------------------------------------------------
    # 健康检查 / 关闭
    # ------------------------------------------------------------------
    async def health_check(self) -> dict:
        """检查数据库连通性"""
        result: dict[str, bool] = {}
        for db_name, engine in self._engines.items():
            try:
                async with engine.connect() as conn:
                    from sqlalchemy import text
                    await conn.execute(text("SELECT 1"))
                result[db_name] = True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"数据库健康检查失败: {db_name} -> {e}")
                result[db_name] = False
        return {"databases": result, "ok": all(result.values()) if result else True}

    async def close(self, db_name: str) -> None:
        """关闭指定数据库引擎"""
        engine = self._engines.pop(db_name, None)
        self._session_factories.pop(db_name, None)
        if engine is not None:
            await engine.dispose()
            logger.info(f"已关闭数据库引擎: {db_name}")

    async def close_all(self) -> None:
        """关闭所有数据库引擎"""
        names = list(self._engines.keys())
        for name in names:
            await self.close(name)
        logger.info("所有数据库引擎已关闭")

    def get_status(self) -> dict:
        """获取数据库管理器状态"""
        return {
            "db_dir": str(self.db_dir),
            "databases": list(self._engines.keys()),
            "log_sql": self._log_sql,
        }

    # 上下文管理支持: async with DatabaseManager() as dbm
    async def __aenter__(self) -> "DatabaseManager":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close_all()


__all__ = ["DatabaseManager"]
