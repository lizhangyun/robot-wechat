"""
HTTP 客户端 - 基于 httpx 的异步 HTTP 客户端

特性:
  - GET / POST / PUT / DELETE
  - 自动重试机制 (指数退避)
  - 超时控制
  - Cookie 管理 (持久化)
  - 代理支持
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Optional, Union

import httpx
from loguru import logger


class HttpRequestError(Exception):
    """HTTP 请求异常 (重试耗尽后抛出)"""

    def __init__(self, message: str, status_code: int = 0, response: Optional[httpx.Response] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class HttpClient:
    """异步 HTTP 客户端 (支持重试 / 超时 / Cookie / 代理)"""

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        retry_statuses: Optional[tuple[int, ...]] = None,
        proxy: Optional[str] = None,
        default_headers: Optional[dict[str, str]] = None,
        cookies: Optional[httpx.Cookies] = None,
        verify: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = retry_backoff
        # 默认对这些状态码重试
        self.retry_statuses: tuple[int, ...] = retry_statuses or (408, 429, 500, 502, 503, 504)
        self.proxy = proxy
        self.default_headers: dict[str, str] = default_headers or {}
        self.cookies: httpx.Cookies = cookies or httpx.Cookies()
        self.verify = verify
        self._client: Optional[httpx.AsyncClient] = None

    # ======================== 生命周期 ========================

    async def __aenter__(self) -> "HttpClient":
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def open(self) -> None:
        """创建底层 httpx.AsyncClient"""
        if self._client is not None:
            return
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout, connect=10.0),
            "cookies": self.cookies,
            "verify": self.verify,
            "headers": self.default_headers,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = httpx.AsyncClient(**kwargs)
        logger.debug("HttpClient 已打开")

    async def close(self) -> None:
        """关闭底层客户端"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.debug("HttpClient 已关闭")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("HttpClient 尚未打开, 请先调用 open() 或使用 async with")
        return self._client

    # ======================== 请求方法 ========================

    async def get(self, url: str, *, params: Optional[dict] = None,
                  headers: Optional[dict] = None, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, params=params, headers=headers, **kwargs)

    async def post(self, url: str, *, json: Any = None, data: Any = None,
                   headers: Optional[dict] = None, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, json=json, data=data, headers=headers, **kwargs)

    async def put(self, url: str, *, json: Any = None, data: Any = None,
                  headers: Optional[dict] = None, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, json=json, data=data, headers=headers, **kwargs)

    async def delete(self, url: str, *, params: Optional[dict] = None,
                     headers: Optional[dict] = None, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, params=params, headers=headers, **kwargs)

    async def request(self, method: str, url: str, *,
                      max_retries: Optional[int] = None,
                      retry_statuses: Optional[tuple[int, ...]] = None,
                      **kwargs: Any) -> httpx.Response:
        """
        发起 HTTP 请求 (带自动重试)

        重试策略:
          - 网络异常 (ConnectError/ReadTimeout/RemoteProtocolError) -> 重试
          - 指定状态码 -> 重试
          - 指数退避 + 抖动
        """
        retries = self.max_retries if max_retries is None else max(0, max_retries)
        statuses = self.retry_statuses if retry_statuses is None else retry_statuses
        last_exc: Optional[Exception] = None
        last_resp: Optional[httpx.Response] = None

        full_url = url if (url.startswith("http://") or url.startswith("https://")) else f"{self.base_url}{url}"

        for attempt in range(retries + 1):
            try:
                resp = await self.client.request(method, full_url, **kwargs)
                if resp.status_code in statuses and attempt < retries:
                    await self._sleep_backoff(attempt)
                    logger.warning(f"请求 {method} {full_url} 返回 {resp.status_code}, "
                                   f"第 {attempt + 1}/{retries} 次重试")
                    last_resp = resp
                    continue
                return resp
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                    httpx.PoolTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt < retries:
                    await self._sleep_backoff(attempt)
                    logger.warning(f"请求 {method} {full_url} 网络异常 {type(exc).__name__}, "
                                   f"第 {attempt + 1}/{retries} 次重试")
                    continue
                break
            except httpx.HTTPError as exc:
                # 其他 HTTP 错误不重试
                raise HttpRequestError(str(exc)) from exc

        if last_resp is not None:
            raise HttpRequestError(
                f"请求 {method} {full_url} 重试 {retries} 次后仍失败, 状态码 {last_resp.status_code}",
                status_code=last_resp.status_code, response=last_resp,
            )
        raise HttpRequestError(
            f"请求 {method} {full_url} 重试 {retries} 次后仍失败: {last_exc}"
        )

    # ======================== 便捷方法 ========================

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET 并返回 JSON"""
        resp = await self.get(url, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        """POST 并返回 JSON"""
        resp = await self.post(url, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def set_header(self, key: str, value: str) -> None:
        """设置默认请求头"""
        self.default_headers[key] = value
        if self._client is not None:
            self._client.headers[key] = value

    def set_cookie(self, name: str, value: str, domain: str = "") -> None:
        """设置 Cookie"""
        self.cookies.set(name, value, domain=domain)

    # ======================== 内部方法 ========================

    async def _sleep_backoff(self, attempt: int) -> None:
        """指数退避 + 抖动"""
        delay = self.retry_backoff * (2 ** attempt) + random.uniform(0, 0.1)
        await asyncio.sleep(delay)
