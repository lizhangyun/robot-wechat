"""
基础设施层单元测试

测试范围:
  - infrastructure/cache.py   : 缓存 (Memcached / Memory 降级)
  - infrastructure/search.py  : 全文搜索 (Solr / Memory 降级)
  - infrastructure/__init__.py: 工厂函数与导出

测试内容:
  - MemoryCache 的 get / set / delete / get_or_set / invalidate / TTL
  - MemorySearch 的 index / search / delete / commit / filters / limit
  - create_cache() 工厂 (降级模式: 无 aiomcache 时返回 MemoryCache)
  - create_search_engine() 工厂 (降级模式: 无 aiohttp 时返回 MemorySearch)
  - CacheManager 基类接口
  - SearchEngine 基类接口
  - 上下文管理器 async with
  - get_status() 状态查询

所有测试在 Linux 环境运行, 验证降级行为不崩溃。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from infrastructure.cache import (
    CacheManager,
    MemoryCache,
    MemcachedCache,
    create_cache,
    _HAS_MEMCACHED,
)
from infrastructure.search import (
    SearchEngine,
    MemorySearch,
    SolrSearch,
    create_search_engine,
    _HAS_AIOHTTP,
)


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


# ============================================================================
# 测试: CacheManager 基类
# ============================================================================
def test_cache_manager_get_not_implemented():
    """CacheManager 基类 get 抛 NotImplementedError"""
    mgr = CacheManager()
    with pytest.raises(NotImplementedError):
        _run(mgr.get("key"))


def test_cache_manager_set_not_implemented():
    """CacheManager 基类 set 抛 NotImplementedError"""
    mgr = CacheManager()
    with pytest.raises(NotImplementedError):
        _run(mgr.set("key", "value"))


def test_cache_manager_delete_not_implemented():
    """CacheManager 基类 delete 抛 NotImplementedError"""
    mgr = CacheManager()
    with pytest.raises(NotImplementedError):
        _run(mgr.delete("key"))


def test_cache_manager_invalidate_not_implemented():
    """CacheManager 基类 invalidate 抛 NotImplementedError"""
    mgr = CacheManager()
    with pytest.raises(NotImplementedError):
        _run(mgr.invalidate("*"))


def test_cache_manager_close_default():
    """CacheManager 基类 close 默认空实现"""
    mgr = CacheManager()
    _run(mgr.close())  # 不抛异常即通过


def test_cache_manager_get_or_set():
    """CacheManager.get_or_set 缓存穿透模式"""
    # 使用自定义子类实现 get/set
    class SimpleCache(CacheManager):
        def __init__(self):
            self._data = {}

        async def get(self, key):
            return self._data.get(key)

        async def set(self, key, value, ttl=0):
            self._data[key] = value

        async def delete(self, key):
            return key in self._data and self._data.pop(key) is not None

        async def invalidate(self, pattern):
            return 0

    cache = SimpleCache()
    call_count = [0]

    async def factory():
        call_count[0] += 1
        return "computed_value"

    # 第一次: 缓存未命中, 调用 factory
    val1 = _run(cache.get_or_set("k1", factory))
    assert val1 == "computed_value"
    assert call_count[0] == 1
    # 第二次: 缓存命中, 不调用 factory
    val2 = _run(cache.get_or_set("k1", factory))
    assert val2 == "computed_value"
    assert call_count[0] == 1


def test_cache_manager_get_or_set_factory_returns_none():
    """factory 返回 None 时不缓存"""
    class SimpleCache(CacheManager):
        def __init__(self):
            self._data = {}

        async def get(self, key):
            return self._data.get(key)

        async def set(self, key, value, ttl=0):
            self._data[key] = value

        async def delete(self, key):
            return False

        async def invalidate(self, pattern):
            return 0

    cache = SimpleCache()
    result = _run(cache.get_or_set("k", lambda: _noop_none()))
    assert result is None
    # 不应缓存 None
    assert "k" not in cache._data


async def _noop_none():
    return None


def test_cache_manager_async_context_manager():
    """CacheManager 支持 async with 上下文管理"""
    closed = [False]

    class SimpleCache(CacheManager):
        async def close(self):
            closed[0] = True

        async def get(self, key):
            return None

        async def set(self, key, value, ttl=0):
            pass

        async def delete(self, key):
            return False

        async def invalidate(self, pattern):
            return 0

    async def _run_test():
        async with SimpleCache() as cache:
            assert cache is not None
        return closed[0]

    assert _run(_run_test()) is True


# ============================================================================
# 测试: MemoryCache
# ============================================================================
def test_memory_cache_init():
    """MemoryCache 初始化"""
    cache = MemoryCache()
    assert cache._store == {}


def test_memory_cache_set_get():
    """set / get 基本读写"""
    cache = MemoryCache()
    _run(cache.set("key1", "value1"))
    assert _run(cache.get("key1")) == "value1"


def test_memory_cache_get_nonexistent():
    """get 不存在的键返回 None"""
    cache = MemoryCache()
    assert _run(cache.get("nonexistent")) is None


def test_memory_cache_set_overwrite():
    """set 覆盖已存在的值"""
    cache = MemoryCache()
    _run(cache.set("key", "v1"))
    _run(cache.set("key", "v2"))
    assert _run(cache.get("key")) == "v2"


def test_memory_cache_delete():
    """delete 删除键"""
    cache = MemoryCache()
    _run(cache.set("key", "value"))
    assert _run(cache.delete("key")) is True
    assert _run(cache.get("key")) is None


def test_memory_cache_delete_nonexistent():
    """delete 不存在的键返回 False"""
    cache = MemoryCache()
    assert _run(cache.delete("nonexistent")) is False


def test_memory_cache_get_or_set():
    """get_or_set 缓存穿透"""
    cache = MemoryCache()
    call_count = [0]

    async def factory():
        call_count[0] += 1
        return {"data": "computed"}

    val1 = _run(cache.get_or_set("k", factory))
    assert val1 == {"data": "computed"}
    assert call_count[0] == 1
    val2 = _run(cache.get_or_set("k", factory))
    assert val2 == {"data": "computed"}
    assert call_count[0] == 1  # 第二次命中缓存


def test_memory_cache_invalidate_pattern():
    """invalidate 通配符批量失效"""
    cache = MemoryCache()
    _run(cache.set("user:1", "a"))
    _run(cache.set("user:2", "b"))
    _run(cache.set("user:3", "c"))
    _run(cache.set("group:1", "d"))

    count = _run(cache.invalidate("user:*"))
    assert count == 3
    assert _run(cache.get("user:1")) is None
    assert _run(cache.get("user:2")) is None
    assert _run(cache.get("user:3")) is None
    assert _run(cache.get("group:1")) == "d"  # 不受影响


def test_memory_cache_invalidate_all():
    """invalidate 通配符 * 失效全部"""
    cache = MemoryCache()
    _run(cache.set("a", 1))
    _run(cache.set("b", 2))
    _run(cache.set("c", 3))
    count = _run(cache.invalidate("*"))
    assert count == 3
    assert _run(cache.get("a")) is None


def test_memory_cache_invalidate_no_match():
    """invalidate 无匹配返回 0"""
    cache = MemoryCache()
    _run(cache.set("a", 1))
    count = _run(cache.invalidate("nonexistent:*"))
    assert count == 0


def test_memory_cache_ttl_expiration():
    """set 带 TTL, 过期后 get 返回 None"""
    cache = MemoryCache()
    _run(cache.set("temp", "value", ttl=1))
    # 立即可读
    assert _run(cache.get("temp")) == "value"
    # 模拟过期: 直接修改 expire_at
    cache._store["temp"] = ("value", time.time() - 1)
    assert _run(cache.get("temp")) is None


def test_memory_cache_ttl_zero_no_expiration():
    """TTL=0 表示不过期"""
    cache = MemoryCache()
    _run(cache.set("perm", "value", ttl=0))
    # expire_at 应为 0
    _, expire_at = cache._store["perm"]
    assert expire_at == 0.0


def test_memory_cache_get_status():
    """get_status 返回缓存状态"""
    cache = MemoryCache()
    _run(cache.set("k1", "v1"))
    _run(cache.set("k2", "v2"))
    status = cache.get_status()
    assert status["backend"] == "memory"
    assert status["keys"] == 2


def test_memory_cache_get_status_empty():
    """空缓存 get_status"""
    cache = MemoryCache()
    status = cache.get_status()
    assert status["backend"] == "memory"
    assert status["keys"] == 0


def test_memory_cache_async_context_manager():
    """MemoryCache 支持 async with"""
    cache = MemoryCache()

    async def _run_test():
        async with cache as c:
            await c.set("k", "v")
            assert await c.get("k") == "v"

    _run(_run_test())


def test_memory_cache_complex_values():
    """MemoryCache 支持复杂值 (字典/列表)"""
    cache = MemoryCache()
    _run(cache.set("dict", {"a": 1, "b": [2, 3]}))
    _run(cache.set("list", [1, 2, 3]))
    assert _run(cache.get("dict")) == {"a": 1, "b": [2, 3]}
    assert _run(cache.get("list")) == [1, 2, 3]


# ============================================================================
# 测试: create_cache 工厂
# ============================================================================
def test_create_cache_default():
    """create_cache 默认返回 MemoryCache"""
    cache = create_cache()
    assert isinstance(cache, MemoryCache)


def test_create_cache_memory_explicit():
    """create_cache 显式指定 memory"""
    cache = create_cache({"backend": "memory"})
    assert isinstance(cache, MemoryCache)


def test_create_cache_memcached_degrades():
    """create_cache 指定 memcached 但无 aiomcache 时降级为 MemoryCache"""
    cache = create_cache({"backend": "memcached", "host": "127.0.0.1", "port": 11211})
    # 降级: 无 aiomcache 时应返回 MemoryCache
    assert isinstance(cache, MemoryCache)


def test_create_cache_none_config():
    """create_cache None 配置返回 MemoryCache"""
    cache = create_cache(None)
    assert isinstance(cache, MemoryCache)


def test_create_cache_empty_config():
    """create_cache 空配置返回 MemoryCache"""
    cache = create_cache({})
    assert isinstance(cache, MemoryCache)


def test_create_cache_unknown_backend():
    """create_cache 未知 backend 返回 MemoryCache (默认)"""
    cache = create_cache({"backend": "redis"})
    assert isinstance(cache, MemoryCache)


def test_create_cache_returns_cache_manager():
    """create_cache 返回 CacheManager 子类"""
    cache = create_cache()
    assert isinstance(cache, CacheManager)


# ============================================================================
# 测试: SearchEngine 基类
# ============================================================================
def test_search_engine_index_not_implemented():
    """SearchEngine 基类 index 抛 NotImplementedError"""
    engine = SearchEngine()
    with pytest.raises(NotImplementedError):
        _run(engine.index("col", [{"id": "1"}]))


def test_search_engine_search_not_implemented():
    """SearchEngine 基类 search 抛 NotImplementedError"""
    engine = SearchEngine()
    with pytest.raises(NotImplementedError):
        _run(engine.search("col", "query"))


def test_search_engine_delete_not_implemented():
    """SearchEngine 基类 delete 抛 NotImplementedError"""
    engine = SearchEngine()
    with pytest.raises(NotImplementedError):
        _run(engine.delete("col", ["1"]))


def test_search_engine_commit_not_implemented():
    """SearchEngine 基类 commit 抛 NotImplementedError"""
    engine = SearchEngine()
    with pytest.raises(NotImplementedError):
        _run(engine.commit("col"))


def test_search_engine_close_default():
    """SearchEngine 基类 close 默认空实现"""
    engine = SearchEngine()
    _run(engine.close())  # 不抛异常即通过


def test_search_engine_async_context_manager():
    """SearchEngine 支持 async with"""
    closed = [False]

    class SimpleEngine(SearchEngine):
        async def close(self):
            closed[0] = True

        async def index(self, collection, documents):
            return 0

        async def search(self, collection, query, filters=None, limit=10):
            return []

        async def delete(self, collection, ids):
            return 0

        async def commit(self, collection):
            return True

    async def _run_test():
        async with SimpleEngine() as engine:
            assert engine is not None
        return closed[0]

    assert _run(_run_test()) is True


# ============================================================================
# 测试: MemorySearch
# ============================================================================
def test_memory_search_init():
    """MemorySearch 初始化"""
    engine = MemorySearch()
    assert engine._collections == {}


def test_memory_search_index_basic():
    """index 索引文档"""
    engine = MemorySearch()
    docs = [
        {"id": "1", "name": "张三", "city": "北京"},
        {"id": "2", "name": "李四", "city": "上海"},
    ]
    count = _run(engine.index("users", docs))
    assert count == 2


def test_memory_search_index_no_id_skipped():
    """缺少 id 字段的文档被跳过"""
    engine = MemorySearch()
    docs = [
        {"id": "1", "name": "张三"},
        {"name": "无ID"},  # 缺少 id
    ]
    count = _run(engine.index("users", docs))
    assert count == 1


def test_memory_search_index_overwrite():
    """相同 id 的文档覆盖旧文档"""
    engine = MemorySearch()
    _run(engine.index("users", [{"id": "1", "name": "旧"}]))
    _run(engine.index("users", [{"id": "1", "name": "新"}]))
    results = _run(engine.search("users", "新"))
    assert len(results) == 1
    assert results[0]["name"] == "新"


def test_memory_search_search_basic():
    """search 关键词搜索"""
    engine = MemorySearch()
    _run(engine.index("users", [
        {"id": "1", "name": "张三", "city": "北京"},
        {"id": "2", "name": "李四", "city": "上海"},
        {"id": "3", "name": "王五", "city": "广州"},
    ]))
    results = _run(engine.search("users", "张"))
    assert len(results) == 1
    assert results[0]["id"] == "1"


def test_memory_search_search_case_insensitive():
    """search 不区分大小写"""
    engine = MemorySearch()
    _run(engine.index("docs", [
        {"id": "1", "title": "Hello World"},
    ]))
    results = _run(engine.search("docs", "hello"))
    assert len(results) == 1
    results = _run(engine.search("docs", "HELLO"))
    assert len(results) == 1


def test_memory_search_search_empty_query():
    """空 query 匹配全部"""
    engine = MemorySearch()
    _run(engine.index("users", [
        {"id": "1", "name": "张三"},
        {"id": "2", "name": "李四"},
    ]))
    results = _run(engine.search("users", ""))
    assert len(results) == 2


def test_memory_search_search_with_filters():
    """search 带字段过滤"""
    engine = MemorySearch()
    _run(engine.index("users", [
        {"id": "1", "name": "张三", "city": "北京"},
        {"id": "2", "name": "李四", "city": "上海"},
        {"id": "3", "name": "王五", "city": "北京"},
    ]))
    results = _run(engine.search("users", "", filters={"city": "北京"}))
    assert len(results) == 2
    assert all(r["city"] == "北京" for r in results)


def test_memory_search_search_with_query_and_filters():
    """search 同时带 query 和 filters"""
    engine = MemorySearch()
    _run(engine.index("users", [
        {"id": "1", "name": "张三", "city": "北京"},
        {"id": "2", "name": "李四", "city": "北京"},
        {"id": "3", "name": "张三", "city": "上海"},
    ]))
    results = _run(engine.search("users", "张", filters={"city": "北京"}))
    assert len(results) == 1
    assert results[0]["id"] == "1"


def test_memory_search_search_limit():
    """search limit 限制返回数量"""
    engine = MemorySearch()
    _run(engine.index("users", [
        {"id": str(i), "name": f"用户{i}"} for i in range(20)
    ]))
    results = _run(engine.search("users", "", limit=5))
    assert len(results) == 5


def test_memory_search_search_nonexistent_collection():
    """search 不存在的 collection 返回空列表"""
    engine = MemorySearch()
    results = _run(engine.search("nonexistent", "query"))
    assert results == []


def test_memory_search_delete():
    """delete 删除索引文档"""
    engine = MemorySearch()
    _run(engine.index("users", [
        {"id": "1", "name": "张三"},
        {"id": "2", "name": "李四"},
    ]))
    count = _run(engine.delete("users", ["1"]))
    assert count == 1
    results = _run(engine.search("users", ""))
    assert len(results) == 1
    assert results[0]["id"] == "2"


def test_memory_search_delete_nonexistent():
    """delete 不存在的 id 返回 0"""
    engine = MemorySearch()
    _run(engine.index("users", [{"id": "1", "name": "张三"}]))
    count = _run(engine.delete("users", ["999"]))
    assert count == 0


def test_memory_search_delete_multiple():
    """delete 批量删除"""
    engine = MemorySearch()
    _run(engine.index("users", [
        {"id": "1", "name": "a"},
        {"id": "2", "name": "b"},
        {"id": "3", "name": "c"},
    ]))
    count = _run(engine.delete("users", ["1", "2", "3"]))
    assert count == 3


def test_memory_search_commit():
    """commit 内存模式始终返回 True"""
    engine = MemorySearch()
    assert _run(engine.commit("any")) is True


def test_memory_search_get_status():
    """get_status 返回搜索引擎状态"""
    engine = MemorySearch()
    _run(engine.index("users", [{"id": "1"}, {"id": "2"}]))
    _run(engine.index("msgs", [{"id": "1"}]))
    status = engine.get_status()
    assert status["backend"] == "memory"
    assert status["collections"]["users"] == 2
    assert status["collections"]["msgs"] == 1


def test_memory_search_get_status_empty():
    """空引擎 get_status"""
    engine = MemorySearch()
    status = engine.get_status()
    assert status["backend"] == "memory"
    assert status["collections"] == {}


def test_memory_search_async_context_manager():
    """MemorySearch 支持 async with"""
    engine = MemorySearch()

    async def _run_test():
        async with engine as e:
            await e.index("c", [{"id": "1"}])
            results = await e.search("c", "")
            assert len(results) == 1

    _run(_run_test())


def test_memory_search_multiple_collections_isolated():
    """多个 collection 数据相互隔离"""
    engine = MemorySearch()
    _run(engine.index("col1", [{"id": "1", "name": "a"}]))
    _run(engine.index("col2", [{"id": "1", "name": "b"}]))
    # 搜索 col1 不应返回 col2 的数据
    results1 = _run(engine.search("col1", ""))
    assert len(results1) == 1
    assert results1[0]["name"] == "a"
    results2 = _run(engine.search("col2", ""))
    assert len(results2) == 1
    assert results2[0]["name"] == "b"


# ============================================================================
# 测试: create_search_engine 工厂
# ============================================================================
def test_create_search_engine_default():
    """create_search_engine 默认返回 MemorySearch"""
    engine = create_search_engine()
    assert isinstance(engine, MemorySearch)


def test_create_search_engine_memory_explicit():
    """create_search_engine 显式指定 memory"""
    engine = create_search_engine({"backend": "memory"})
    assert isinstance(engine, MemorySearch)


def test_create_search_engine_solr_degrades():
    """create_search_engine 指定 solr 但无 aiohttp 时降级为 MemorySearch"""
    engine = create_search_engine({
        "backend": "solr",
        "base_url": "http://127.0.0.1:8983/solr",
    })
    assert isinstance(engine, MemorySearch)


def test_create_search_engine_none_config():
    """create_search_engine None 配置返回 MemorySearch"""
    engine = create_search_engine(None)
    assert isinstance(engine, MemorySearch)


def test_create_search_engine_empty_config():
    """create_search_engine 空配置返回 MemorySearch"""
    engine = create_search_engine({})
    assert isinstance(engine, MemorySearch)


def test_create_search_engine_unknown_backend():
    """create_search_engine 未知 backend 返回 MemorySearch"""
    engine = create_search_engine({"backend": "elasticsearch"})
    assert isinstance(engine, MemorySearch)


def test_create_search_engine_returns_search_engine():
    """create_search_engine 返回 SearchEngine 子类"""
    engine = create_search_engine()
    assert isinstance(engine, SearchEngine)


# ============================================================================
# 测试: 降级模式标志
# ============================================================================
def test_has_memcached_flag():
    """_HAS_MEMCACHED 标志存在"""
    assert isinstance(_HAS_MEMCACHED, bool)


def test_has_aiohttp_flag():
    """_HAS_AIOHTTP 标志存在"""
    assert isinstance(_HAS_AIOHTTP, bool)
