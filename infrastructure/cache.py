"""
缓存基础设施 - 对应原软件 Memcached 缓存层

原软件使用 Memcached 进行查询缓存加速:
    - 函数: BeginQueryCache, DeleteQueryCache, SetCache, GetCache
    - 缓存统计数据、联系人信息、群成员列表

本模块提供:
    - CacheManager:    缓存管理器基类 (统一接口)
    - MemcachedCache:  基于 aiomcache 的真实 Memcached 实现
    - MemoryCache:     内存字典缓存 (降级方案)
    - create_cache:    工厂函数, 根据配置选择实现

所有组件在依赖缺失时自动降级到内存方案, 不崩溃。
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from typing import Any, Awaitable, Callable, Optional, Union

from loguru import logger

# 可选依赖: aiomcache (真实 Memcached 客户端)
try:  # pragma: no cover - 依赖外部库, 测试环境通常降级
    import aiomcache  # type: ignore[import-not-found]

    _HAS_MEMCACHED: bool = True
except ImportError:  # pragma: no cover - 降级路径
    aiomcache = None  # type: ignore[assignment]
    _HAS_MEMCACHED = False


class CacheManager:
    """缓存管理器基类, 定义统一接口。

    所有缓存实现 (Memcached / Memory) 均需实现以下方法:
        - get(key)                  查询缓存
        - set(key, value, ttl)      写入缓存
        - delete(key)               删除缓存
        - invalidate(pattern)       批量失效 (通配符匹配)
        - close()                   关闭连接

    同时提供缓存穿透模式 get_or_set 的默认实现。
    """

    async def get(self, key: str) -> Any:
        """查询缓存, 不存在返回 None。"""
        raise NotImplementedError

    async def set(self, key: str, value: Any, ttl: int = 0) -> None:
        """写入缓存。

        Args:
            key: 缓存键
            value: 缓存值 (将被 JSON 序列化存储)
            ttl: 过期时间 (秒), 0 表示不过期
        """
        raise NotImplementedError

    async def delete(self, key: str) -> bool:
        """删除缓存, 返回是否删除成功。"""
        raise NotImplementedError

    async def invalidate(self, pattern: str) -> int:
        """批量失效匹配 pattern (fnmatch 通配符) 的缓存键, 返回失效数量。"""
        raise NotImplementedError

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl: int = 0,
    ) -> Any:
        """缓存穿透模式: 缓存未命中时调用 factory 生成值并写入缓存。

        对应原软件 BeginQueryCache 语义: 先查缓存, 未命中则查源并回填。

        Args:
            key: 缓存键
            factory: async 工厂函数, 缓存未命中时调用以生成值
            ttl: 过期时间 (秒)

        Returns:
            缓存命中或新生成的值
        """
        value = await self.get(key)
        if value is not None:
            logger.debug(f"缓存命中: {key}")
            return value
        logger.debug(f"缓存未命中, 调用 factory: {key}")
        value = await factory()
        if value is not None:
            await self.set(key, value, ttl)
        return value

    async def close(self) -> None:
        """关闭缓存连接 (基类默认空实现)。"""
        pass

    # 上下文管理支持
    async def __aenter__(self) -> "CacheManager":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()


class MemoryCache(CacheManager):
    """内存字典缓存 (降级方案)。

    使用进程内字典存储, 支持过期时间 (TTL) 与通配符批量失效。
    适用于开发环境或无 Memcached 时的降级场景。
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expire_at)
        self._lock: asyncio.Lock = asyncio.Lock()
        logger.info("内存缓存 (MemoryCache) 已初始化")

    async def get(self, key: str) -> Any:
        async with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            value, expire_at = item
            if expire_at and expire_at < time.time():
                self._store.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any, ttl: int = 0) -> None:
        async with self._lock:
            expire_at = time.time() + ttl if ttl > 0 else 0.0
            self._store[key] = (value, expire_at)

    async def delete(self, key: str) -> bool:
        async with self._lock:
            existed = key in self._store
            self._store.pop(key, None)
            return existed

    async def invalidate(self, pattern: str) -> int:
        async with self._lock:
            keys_to_del = [k for k in self._store if fnmatch.fnmatch(k, pattern)]
            for k in keys_to_del:
                del self._store[k]
        if keys_to_del:
            logger.debug(f"内存缓存批量失效: pattern={pattern}, count={len(keys_to_del)}")
        return len(keys_to_del)

    def get_status(self) -> dict:
        """获取缓存状态。"""
        return {
            "backend": "memory",
            "keys": len(self._store),
        }


class MemcachedCache(CacheManager):
    """基于 aiomcache 的真实 Memcached 缓存。

    对应原软件 Memcached 缓存层。连接池由 aiomcache 内部管理。
    值以 JSON 序列化后存储, 支持过期时间与 (基于本地键注册表的) 批量失效。

    注意: Memcached 原生不支持通配符批量删除, invalidate() 依赖本进程内维护的
    键注册表进行匹配, 仅能失效通过本实例 set() 写入的键。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11211,
        pool_size: int = 10,
        *,
        key_prefix: str = "",
    ) -> None:
        """初始化 Memcached 客户端。

        Args:
            host: Memcached 主机
            port: Memcached 端口
            pool_size: 连接池大小
            key_prefix: 键前缀 (用于多实例命名空间隔离)
        """
        if not _HAS_MEMCACHED:
            raise RuntimeError(
                "aiomcache 不可用, 无法使用 MemcachedCache; "
                "请安装 aiomcache 或改用 MemoryCache"
            )
        self._host: str = host
        self._port: int = port
        self._pool_size: int = pool_size
        self._key_prefix: str = key_prefix
        self._client: aiomcache.Client = aiomcache.Client(
            host, port, pool_size=pool_size
        )
        # 本地键注册表, 用于 invalidate 通配符匹配
        self._known_keys: set[str] = set()
        self._keys_lock: asyncio.Lock = asyncio.Lock()
        logger.info(
            f"Memcached 缓存已初始化: {host}:{port} (pool_size={pool_size})"
        )

    def _full_key(self, key: str) -> str:
        """加上前缀的完整键。"""
        return f"{self._key_prefix}{key}" if self._key_prefix else key

    @staticmethod
    def _encode(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")

    @staticmethod
    def _decode(data: bytes) -> Any:
        return json.loads(data.decode("utf-8"))

    async def get(self, key: str) -> Any:
        try:
            data = await self._client.get(self._full_key(key).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Memcached get 失败: key={key}, err={exc}")
            return None
        if data is None:
            return None
        try:
            return self._decode(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(f"Memcached 缓存值反序列化失败: key={key}, err={exc}")
            return None

    async def set(self, key: str, value: Any, ttl: int = 0) -> None:
        full_key = self._full_key(key).encode("utf-8")
        data = self._encode(value)
        try:
            await self._client.set(full_key, data, exptime=int(ttl))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Memcached set 失败: key={key}, err={exc}")
            return
        async with self._keys_lock:
            self._known_keys.add(key)

    async def delete(self, key: str) -> bool:
        try:
            await self._client.delete(self._full_key(key).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Memcached delete 失败: key={key}, err={exc}")
            return False
        async with self._keys_lock:
            self._known_keys.discard(key)
        return True

    async def invalidate(self, pattern: str) -> int:
        async with self._keys_lock:
            keys_to_del = [k for k in self._known_keys if fnmatch.fnmatch(k, pattern)]
        count = 0
        for k in keys_to_del:
            if await self.delete(k):
                count += 1
        if count:
            logger.debug(
                f"Memcached 批量失效: pattern={pattern}, count={count}"
            )
        return count

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"关闭 Memcached 连接时异常: {exc}")
        logger.info("Memcached 连接已关闭")

    def get_status(self) -> dict:
        """获取缓存状态。"""
        return {
            "backend": "memcached",
            "host": self._host,
            "port": self._port,
            "pool_size": self._pool_size,
            "known_keys": len(self._known_keys),
        }


# ============================================================================
# 工厂函数: 根据配置选择缓存实现
# ============================================================================
def create_cache(config: Optional[dict] = None) -> CacheManager:
    """根据配置创建缓存管理器。

    配置示例::

        # 内存缓存 (降级)
        {"backend": "memory"}

        # Memcached
        {
            "backend": "memcached",
            "host": "127.0.0.1",
            "port": 11211,
            "pool_size": 10,
            "key_prefix": "robot3:",
        }

    Args:
        config: 配置字典, 默认使用内存缓存

    Returns:
        CacheManager 实例 (MemoryCache 或 MemcachedCache)
    """
    config = config or {}
    backend = config.get("backend", "memory")

    if backend == "memcached":
        if not _HAS_MEMCACHED:
            logger.warning(
                "aiomcache 不可用, Memcached 缓存不可用, 降级为内存缓存 (MemoryCache)"
            )
            return MemoryCache()
        host = config.get("host", "127.0.0.1")
        port = int(config.get("port", 11211))
        pool_size = int(config.get("pool_size", 10))
        key_prefix = config.get("key_prefix", "")
        logger.info(f"创建 Memcached 缓存: {host}:{port}")
        return MemcachedCache(
            host=host, port=port, pool_size=pool_size, key_prefix=key_prefix
        )

    # 默认 / memory
    logger.info("创建内存缓存 (MemoryCache)")
    return MemoryCache()


__all__ = [
    "CacheManager",
    "MemoryCache",
    "MemcachedCache",
    "create_cache",
]
