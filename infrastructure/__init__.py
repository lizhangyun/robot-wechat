"""
基础设施层 - 对应原软件使用的多种基础设施组件

聚合以下子模块, 统一对外导出:

- cache:   Memcached 缓存 (aiomcache) / 内存缓存 (降级)
- search:  Apache Solr 全文搜索 (aiohttp) / 内存搜索 (降级)

数据库加密 (pysqlcipher3) 与 RabbitMQ 消息队列 (aio-pika) 分别位于
database/encrypted_db.py 与 network/message_queue.py, 因其归属各自领域模块。

所有组件在依赖缺失时自动降级到内存方案, 不崩溃。
"""
from __future__ import annotations

from infrastructure.cache import (
    CacheManager,
    MemoryCache,
    MemcachedCache,
    create_cache,
)
from infrastructure.search import (
    MemorySearch,
    SearchEngine,
    SolrSearch,
    create_search_engine,
)

__all__ = [
    # 缓存
    "CacheManager",
    "MemoryCache",
    "MemcachedCache",
    "create_cache",
    # 搜索
    "SearchEngine",
    "MemorySearch",
    "SolrSearch",
    "create_search_engine",
]
