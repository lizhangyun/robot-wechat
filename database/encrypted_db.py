"""
AES-256 加密数据库 - 对应原软件 wxsqlite3 AES-256 加密方案

原软件通过 wxsqlite3 扩展实现 SQLite AES-256 加密, 函数链:
    wxsqlite3_config -> wxsqlite3_config_cipher (配置为 AES-256) -> sqlite3_key()

加密的数据库: users.db, appdata.db, app/c680X/data.db
明文数据库: data.db (联系人 / 群聊列表)

密钥来源: config.ini 中 [jizhang] 段的 keyword 字段, 运行时 AES 解密后传入 sqlite3_key()

本模块封装 pysqlcipher3 实现 AES-256 加密:
    - 打开数据库时执行 PRAGMA key 与 PRAGMA cipher (配置为 aes-256-cbc)
    - 提供 execute / query / session 等异步接口
    - 与 database/__init__.py 中 Database 类的接口保持兼容 (init / session / close / health_check)
    - 在 pysqlcipher3 不可用时自动降级为普通 SQLite, 并通过 loguru 发出警告
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Union

from loguru import logger

# 尝试导入 pysqlcipher3, 失败则降级为标准 sqlite3 (无加密)
try:  # pragma: no cover - 依赖外部库, 测试环境通常降级
    from pysqlcipher3 import dbapi2 as sqlcipher  # type: ignore[import-not-found]

    _HAS_SQLCIPHER: bool = True
    _DRIVER_NAME: str = "pysqlcipher3"
except ImportError:  # pragma: no cover - 降级路径
    sqlcipher = sqlite3  # type: ignore[assignment]
    _HAS_SQLCIPHER = False
    _DRIVER_NAME = "sqlite3"


def _escape_pragma_value(value: str) -> str:
    """转义 PRAGMA 文本字面量中的单引号 (SQLite 文本字面量以单引号包裹, 内部单引号需双写)。

    PRAGMA 不支持绑定参数, 因此密钥需以字面量形式拼接, 必须转义以避免注入与语法错误。
    """
    return value.replace("'", "''")


class _Session:
    """事务会话: 在持有连接锁的期间内提供 execute / query / fetchone 操作。

    由 ``EncryptedDatabase.session()`` 上下文管理器创建, 退出时自动 commit / rollback。
    会话内的方法不会重复获取锁 (锁由外层 session 上下文持有), 避免死锁。
    """

    def __init__(self, db: "EncryptedDatabase") -> None:
        self._db: "EncryptedDatabase" = db

    async def execute(self, sql: str, params: Union[tuple, list] = ()) -> Any:
        """在当前事务内执行 SQL (写 / DDL), 不自动提交 (由 session 退出时统一提交)。"""
        return await self._db._exec(sql, params)

    async def query(self, sql: str, params: Union[tuple, list] = ()) -> list[dict]:
        """在当前事务内查询多行, 返回字典列表。"""
        cur = await self._db._exec(sql, params)
        rows = await asyncio.to_thread(cur.fetchall)
        return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: Union[tuple, list] = ()) -> Optional[dict]:
        """在当前事务内查询单行, 返回字典或 None。"""
        cur = await self._db._exec(sql, params)
        row = await asyncio.to_thread(cur.fetchone)
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Union[tuple, list] = ()) -> list[dict]:
        """在当前事务内查询多行 (query 的别名)。"""
        return await self.query(sql, params)


class EncryptedDatabase:
    """AES-256 加密数据库 (基于 pysqlcipher3)。

    对应原软件 wxsqlite3 AES-256 加密方案。当 pysqlcipher3 不可用或未提供密钥时,
    自动降级为普通 SQLite (无加密) 并通过 loguru 发出警告。

    与 ``database.Database`` 类接口兼容: 提供 ``init`` / ``session`` / ``close`` / ``health_check``。

    用法::

        db = EncryptedDatabase("users.db", key="my-secret")
        await db.init()
        await db.execute("CREATE TABLE t (id INTEGER)")
        rows = await db.query("SELECT * FROM t")
        async with db.session() as s:
            await s.execute("INSERT INTO t VALUES (1)")
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        key: Optional[str] = None,
        *,
        echo: bool = False,
    ) -> None:
        """初始化加密数据库配置。

        Args:
            db_path: 数据库文件路径, 可在 init() 时再次指定
            key: AES-256 加密密钥 (明文), 为 None 或空字符串则不加密
            echo: 是否在日志中输出 SQL (预留, 当前未启用详细输出)
        """
        self.db_path: Optional[Path] = Path(db_path) if db_path else None
        self.key: Optional[str] = key
        self.echo: bool = echo
        self._conn: Optional[Any] = None  # sqlcipher.Connection / sqlite3.Connection
        self._lock: asyncio.Lock = asyncio.Lock()
        self._initialized: bool = False
        # 实际是否启用加密 (需要驱动支持 + 提供密钥)
        self._encrypted: bool = bool(_HAS_SQLCIPHER and key)

    # ------------------------------------------------------------------
    # 连接 / 初始化
    # ------------------------------------------------------------------
    async def init(
        self,
        db_path: Optional[Union[str, Path]] = None,
        key: Optional[str] = None,
    ) -> None:
        """打开加密数据库, 执行 PRAGMA key 与 PRAGMA cipher (配置 AES-256)。

        对应原软件 sqlite3_key() 调用。

        Args:
            db_path: 数据库文件路径, 未提供则使用构造函数中的值
            key: 加密密钥, 未提供则使用构造函数中的值

        Raises:
            ValueError: 未提供 db_path 时
        """
        if db_path is not None:
            self.db_path = Path(db_path)
        if key is not None:
            self.key = key
        if self.db_path is None:
            raise ValueError("db_path 未指定, 请在构造函数或 init() 中提供")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._encrypted = bool(_HAS_SQLCIPHER and self.key)

        if not _HAS_SQLCIPHER:
            logger.warning(
                "pysqlcipher3 不可用, EncryptedDatabase 降级为普通 SQLite (无加密): "
                f"{self.db_path}"
            )
        elif not self.key:
            logger.warning(
                "未提供加密密钥, EncryptedDatabase 以普通 SQLite 模式打开 (无加密): "
                f"{self.db_path}"
            )

        path_str = str(self.db_path)

        def _open() -> Any:
            # check_same_thread=False: 配合 asyncio.to_thread 跨线程使用, 由 _lock 串行化访问
            conn = sqlcipher.connect(path_str, check_same_thread=False)
            conn.row_factory = sqlcipher.Row
            if self._encrypted and self.key:
                # 设置密钥 (对应原软件 sqlite3_key)
                conn.execute(f"PRAGMA key = '{_escape_pragma_value(self.key)}'")
                # 配置加密算法为 AES-256-CBC (对应原软件 wxsqlite3_config_cipher)
                conn.execute("PRAGMA cipher = 'aes-256-cbc'")
                # 兼容性参数 (SQLCipher 推荐配置)
                conn.execute("PRAGMA cipher_page_size = 4096")
                conn.execute("PRAGMA kdf_iter = 64000")
                conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA256")
                conn.execute("PRAGMA cipher_use_hmac = ON")
                # 验证密钥是否正确 (读取 schema, 错误密钥会抛异常)
                try:
                    conn.execute("SELECT count(*) FROM sqlite_master")
                except Exception as exc:
                    conn.close()
                    raise RuntimeError(f"加密数据库密钥错误或损坏: {exc}") from exc
            # 通用 SQLite 优化
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.execute("PRAGMA foreign_keys=ON;")
            except Exception:  # noqa: BLE001
                pass
            return conn

        self._conn = await asyncio.to_thread(_open)
        self._initialized = True
        mode = "AES-256 加密" if self._encrypted else "明文 (降级)"
        logger.info(
            f"加密数据库已初始化 (驱动={_DRIVER_NAME}, 模式={mode}): {self.db_path}"
        )

    @property
    def conn(self) -> Any:
        """获取底层连接 (未初始化时抛出 RuntimeError)。"""
        if self._conn is None:
            raise RuntimeError("数据库尚未初始化, 请先调用 init()")
        return self._conn

    @property
    def is_encrypted(self) -> bool:
        """是否实际启用了加密。"""
        return self._encrypted

    # ------------------------------------------------------------------
    # 内部执行 (不加锁, 供 session 复用)
    # ------------------------------------------------------------------
    async def _exec(self, sql: str, params: Union[tuple, list] = ()) -> Any:
        """在底层连接上执行 SQL, 返回游标 (不加锁, 仅供持锁路径调用)。"""
        if self._conn is None:
            raise RuntimeError("数据库尚未初始化, 请先调用 init()")
        params = tuple(params) if not isinstance(params, tuple) else params
        if self.echo:
            logger.debug(f"[EncryptedDB] {sql.strip()[:200]} | params={params}")
        return await asyncio.to_thread(self._conn.execute, sql, params)

    # ------------------------------------------------------------------
    # 通用查询 (自动加锁 + 自动提交)
    # ------------------------------------------------------------------
    async def execute(self, sql: str, params: Union[tuple, list] = ()) -> Any:
        """执行写 / DDL 语句, 自动提交并返回游标。"""
        async with self._lock:
            cur = await self._exec(sql, params)
            await asyncio.to_thread(self._conn.commit)  # type: ignore[union-attr]
            return cur

    async def query(self, sql: str, params: Union[tuple, list] = ()) -> list[dict]:
        """查询多行, 返回字典列表。"""
        async with self._lock:
            cur = await self._exec(sql, params)
            rows = await asyncio.to_thread(cur.fetchall)
            return [dict(r) for r in rows]

    async def fetchone(self, sql: str, params: Union[tuple, list] = ()) -> Optional[dict]:
        """查询单行, 返回字典或 None。"""
        async with self._lock:
            cur = await self._exec(sql, params)
            row = await asyncio.to_thread(cur.fetchone)
            return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Union[tuple, list] = ()) -> list[dict]:
        """查询多行 (query 的别名)。"""
        return await self.query(sql, params)

    # ------------------------------------------------------------------
    # 事务上下文
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def session(self):
        """事务上下文管理器: 在事务内执行多条 SQL, 退出时自动提交, 异常时回滚。

        用法::

            async with db.session() as s:
                await s.execute("INSERT INTO t VALUES (1)")
                rows = await s.query("SELECT * FROM t")
        """
        if self._conn is None:
            raise RuntimeError("数据库尚未初始化, 请先调用 init()")
        await self._lock.acquire()
        try:
            yield _Session(self)
            await asyncio.to_thread(self._conn.commit)  # type: ignore[union-attr]
        except Exception:
            try:
                await asyncio.to_thread(self._conn.rollback)  # type: ignore[union-attr]
            except Exception as rb_exc:  # noqa: BLE001
                logger.warning(f"事务回滚失败: {rb_exc}")
            raise
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # 健康检查 / 关闭
    # ------------------------------------------------------------------
    async def health_check(self) -> bool:
        """健康检查: 能否执行一条查询。"""
        try:
            await self.query("SELECT 1")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"加密数据库健康检查失败: {e}")
            return False

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
            self._initialized = False
            logger.info(f"加密数据库已关闭: {self.db_path}")

    # 上下文管理支持: async with EncryptedDatabase(...) as db
    async def __aenter__(self) -> "EncryptedDatabase":
        if not self._initialized:
            await self.init()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()

    def get_status(self) -> dict:
        """获取数据库状态信息。"""
        return {
            "db_path": str(self.db_path) if self.db_path else None,
            "driver": _DRIVER_NAME,
            "encrypted": self._encrypted,
            "initialized": self._initialized,
        }


__all__ = ["EncryptedDatabase"]
