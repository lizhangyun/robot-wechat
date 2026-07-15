"""
全文搜索基础设施 - 对应原软件 Apache Solr 搜索层

原软件通过 HTTP API 与 Apache Solr 通信, 用于全文搜索:
    - 搜索联系人、消息记录

本模块提供:
    - SearchEngine:      搜索引擎基类 (统一接口)
    - SolrSearch:        基于 aiohttp 与 Solr HTTP API 通信的真实实现
    - MemorySearch:      内存全文搜索 (降级方案, 使用简单字符串匹配)
    - create_search_engine: 工厂函数, 根据配置选择实现

所有组件在依赖缺失时自动降级到内存方案, 不崩溃。
"""
from __future__ import annotations

import asyncio
from typing import Any, Iterable, Optional, Sequence, Union

from loguru import logger

# 可选依赖: aiohttp (与 Solr HTTP API 通信)
try:  # pragma: no cover - 依赖外部库, 测试环境通常降级
    import aiohttp  # type: ignore[import-not-found]

    _HAS_AIOHTTP: bool = True
except ImportError:  # pragma: no cover - 降级路径
    aiohttp = None  # type: ignore[assignment]
    _HAS_AIOHTTP = False


class SearchEngine:
    """搜索引擎基类, 定义统一接口。

    所有搜索实现 (Solr / Memory) 均需实现以下方法:
        - index(collection, documents)   索引文档
        - search(collection, query, ...) 搜索
        - delete(collection, ids)        删除索引
        - commit(collection)             提交索引变更
        - close()                        关闭连接
    """

    async def index(
        self,
        collection: str,
        documents: Sequence[dict],
    ) -> int:
        """索引文档到指定 collection。

        Args:
            collection: Solr collection 名称 (或内存索引集合名)
            documents: 文档列表 (每项为 dict, 应包含 id 字段)

        Returns:
            成功索引的文档数量
        """
        raise NotImplementedError

    async def search(
        self,
        collection: str,
        query: str,
        filters: Optional[dict] = None,
        limit: int = 10,
    ) -> list[dict]:
        """在指定 collection 中搜索文档。

        Args:
            collection: Solr collection 名称
            query: 查询字符串 (Solr 查询语法, 或内存模式下的关键词)
            filters: 字段过滤条件 (key=value 的精确匹配)
            limit: 返回结果上限

        Returns:
            匹配的文档列表
        """
        raise NotImplementedError

    async def delete(
        self,
        collection: str,
        ids: Sequence[Union[str, int]],
    ) -> int:
        """删除指定 collection 中的索引文档。

        Args:
            collection: Solr collection 名称
            ids: 要删除的文档 ID 列表

        Returns:
            成功删除的文档数量
        """
        raise NotImplementedError

    async def commit(self, collection: str) -> bool:
        """提交索引变更 (使其对搜索可见)。

        Args:
            collection: Solr collection 名称

        Returns:
            是否提交成功
        """
        raise NotImplementedError

    async def close(self) -> None:
        """关闭搜索引擎连接 (基类默认空实现)。"""
        pass

    # 上下文管理支持
    async def __aenter__(self) -> "SearchEngine":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()


class MemorySearch(SearchEngine):
    """内存全文搜索 (降级方案)。

    使用进程内字典存储文档, 采用简单的字符串包含匹配实现全文搜索。
    适用于开发环境或无 Solr 时的降级场景。

    - 文档需包含 ``id`` 字段作为唯一标识
    - search 时对所有字段值进行小写包含匹配
    - filters 为字段精确匹配
    """

    def __init__(self) -> None:
        # collection -> {doc_id: doc}
        self._collections: dict[str, dict[str, dict]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        logger.info("内存搜索引擎 (MemorySearch) 已初始化")

    async def index(
        self,
        collection: str,
        documents: Sequence[dict],
    ) -> int:
        async with self._lock:
            store = self._collections.setdefault(collection, {})
            count = 0
            for doc in documents:
                doc_id = str(doc.get("id", ""))
                if not doc_id:
                    logger.warning(f"文档缺少 id 字段, 跳过: {doc}")
                    continue
                store[doc_id] = dict(doc)
                count += 1
        if count:
            logger.debug(f"内存索引写入: collection={collection}, count={count}")
        return count

    async def search(
        self,
        collection: str,
        query: str,
        filters: Optional[dict] = None,
        limit: int = 10,
    ) -> list[dict]:
        async with self._lock:
            store = self._collections.get(collection, {})
            # 复制快照, 避免在锁外处理
            docs = list(store.values())
        q = (query or "").lower().strip()
        results: list[dict] = []
        for doc in docs:
            # 应用字段精确过滤
            if filters:
                matched = True
                for k, v in filters.items():
                    if str(doc.get(k, "")) != str(v):
                        matched = False
                        break
                if not matched:
                    continue
            # 空 query 视为匹配全部
            if not q:
                results.append(doc)
                continue
            # 对所有字段值进行包含匹配
            for v in doc.values():
                if q in str(v).lower():
                    results.append(doc)
                    break
        return results[:limit]

    async def delete(
        self,
        collection: str,
        ids: Sequence[Union[str, int]],
    ) -> int:
        async with self._lock:
            store = self._collections.get(collection, {})
            count = 0
            for i in ids:
                if store.pop(str(i), None) is not None:
                    count += 1
        if count:
            logger.debug(f"内存索引删除: collection={collection}, count={count}")
        return count

    async def commit(self, collection: str) -> bool:
        # 内存模式无需提交, 始终即时可见
        return True

    def get_status(self) -> dict:
        """获取搜索引擎状态。"""
        return {
            "backend": "memory",
            "collections": {
                name: len(docs) for name, docs in self._collections.items()
            },
        }


class SolrSearch(SearchEngine):
    """基于 aiohttp 与 Solr HTTP API 通信的真实搜索引擎。

    对应原软件 Apache Solr 全文搜索。通过 Solr 的 REST 接口完成
    索引 / 搜索 / 删除 / 提交操作。

    主要接口:
        - /update/json/docs  : 索引文档 (POST JSON)
        - /select            : 搜索 (GET, wt=json)
        - /update            : 删除 / 提交 (POST JSON)

    需要 aiohttp 作为 HTTP 客户端。aiohttp 不可用时应通过
    create_search_engine() 工厂降级为 MemorySearch。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8983/solr",
        collection: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        """初始化 Solr 搜索引擎。

        Args:
            base_url: Solr 根 URL (如 http://127.0.0.1:8983/solr)
            collection: 默认 collection (可在各方法中通过 collection 参数覆盖)
            timeout: HTTP 请求超时 (秒)
        """
        if not _HAS_AIOHTTP:
            raise RuntimeError(
                "aiohttp 不可用, 无法使用 SolrSearch; "
                "请安装 aiohttp 或改用 MemorySearch"
            )
        self._base_url: str = base_url.rstrip("/")
        self._default_collection: Optional[str] = collection
        self._timeout: "aiohttp.ClientTimeout" = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional["aiohttp.ClientSession"] = None
        logger.info(f"Solr 搜索引擎已初始化: base_url={self._base_url}")

    def _resolve_collection(self, collection: Optional[str]) -> str:
        """解析实际使用的 collection。"""
        col = collection or self._default_collection
        if not col:
            raise ValueError("未指定 collection, 请在方法参数或构造函数中提供")
        return col

    async def _ensure_session(self) -> "aiohttp.ClientSession":
        """惰性创建并复用 aiohttp 会话。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def index(
        self,
        collection: str,
        documents: Sequence[dict],
    ) -> int:
        col = self._resolve_collection(collection)
        if not documents:
            return 0
        session = await self._ensure_session()
        url = f"{self._base_url}/{col}/update/json/docs"
        try:
            async with session.post(url, json=list(documents)) as resp:
                resp.raise_for_status()
                await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Solr 索引失败: collection={col}, err={exc}")
            raise
        logger.debug(f"Solr 索引写入: collection={col}, count={len(documents)}")
        return len(documents)

    async def search(
        self,
        collection: str,
        query: str,
        filters: Optional[dict] = None,
        limit: int = 10,
    ) -> list[dict]:
        col = self._resolve_collection(collection)
        session = await self._ensure_session()
        # 空 query 视为匹配全部
        q = query if query else "*:*"
        params: dict[str, Any] = {
            "q": q,
            "rows": int(limit),
            "wt": "json",
        }
        if filters:
            # 字段精确过滤, 使用 fq 参数
            fq_parts = [f"{k}:{_solr_escape(str(v))}" for k, v in filters.items()]
            params["fq"] = " AND ".join(fq_parts)
        url = f"{self._base_url}/{col}/select"
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Solr 搜索失败: collection={col}, err={exc}")
            raise
        docs = data.get("response", {}).get("docs", [])
        return list(docs)

    async def delete(
        self,
        collection: str,
        ids: Sequence[Union[str, int]],
    ) -> int:
        col = self._resolve_collection(collection)
        if not ids:
            return 0
        session = await self._ensure_session()
        url = f"{self._base_url}/{col}/update"
        # Solr 删除语法: {"delete": [{"id": "..."}, ...]} 或 {"delete": {"query": "..."}}
        delete_body: dict[str, Any] = {
            "delete": [{"id": str(i)} for i in ids]
        }
        try:
            async with session.post(
                url, json=delete_body, params={"commit": "true"}
            ) as resp:
                resp.raise_for_status()
                await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Solr 删除失败: collection={col}, err={exc}")
            raise
        logger.debug(f"Solr 索引删除: collection={col}, count={len(ids)}")
        return len(ids)

    async def commit(self, collection: str) -> bool:
        col = self._resolve_collection(collection)
        session = await self._ensure_session()
        url = f"{self._base_url}/{col}/update"
        try:
            async with session.post(url, params={"commit": "true"}) as resp:
                resp.raise_for_status()
                await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Solr 提交失败: collection={col}, err={exc}")
            raise
        logger.debug(f"Solr 索引已提交: collection={col}")
        return True

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"关闭 Solr HTTP 会话时异常: {exc}")
        self._session = None
        logger.info("Solr 搜索引擎连接已关闭")

    def get_status(self) -> dict:
        """获取搜索引擎状态。"""
        return {
            "backend": "solr",
            "base_url": self._base_url,
            "default_collection": self._default_collection,
            "session_open": self._session is not None
            and not self._session.closed,
        }


def _solr_escape(value: str) -> str:
    """转义 Solr 查询中的特殊字符 (用于 fq 精确过滤)。

    将需要按字面量匹配的特殊字符前加反斜杠, 并用双引号包裹。
    """
    # Solr 保留字符: + - && || ! ( ) { } [ ] ^ " ~ * ? : \ /
    special = set('+-&&||!(){}[]^"~*?:\\/ ')
    escaped = "".join("\\" + ch if ch in special else ch for ch in value)
    return f'"{escaped}"'


# ============================================================================
# 工厂函数: 根据配置选择搜索引擎
# ============================================================================
def create_search_engine(config: Optional[dict] = None) -> SearchEngine:
    """根据配置创建搜索引擎。

    配置示例::

        # 内存搜索 (降级)
        {"backend": "memory"}

        # Solr
        {
            "backend": "solr",
            "base_url": "http://127.0.0.1:8983/solr",
            "collection": "robot3",
            "timeout": 30,
        }

    Args:
        config: 配置字典, 默认使用内存搜索

    Returns:
        SearchEngine 实例 (MemorySearch 或 SolrSearch)
    """
    config = config or {}
    backend = config.get("backend", "memory")

    if backend == "solr":
        if not _HAS_AIOHTTP:
            logger.warning(
                "aiohttp 不可用, Solr 搜索不可用, 降级为内存搜索引擎 (MemorySearch)"
            )
            return MemorySearch()
        base_url = config.get("base_url", "http://127.0.0.1:8983/solr")
        collection = config.get("collection")
        timeout = float(config.get("timeout", 30))
        logger.info(f"创建 Solr 搜索引擎: base_url={base_url}")
        return SolrSearch(base_url=base_url, collection=collection, timeout=timeout)

    # 默认 / memory
    logger.info("创建内存搜索引擎 (MemorySearch)")
    return MemorySearch()


__all__ = [
    "SearchEngine",
    "MemorySearch",
    "SolrSearch",
    "create_search_engine",
]
