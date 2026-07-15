"""
防重放攻击机制 - 对应原软件 AntiReplay 组件

功能：
- 为每条请求生成时间戳与随机 nonce；
- 服务端验证时间窗口（默认 300 秒）与 nonce 唯一性；
- 维护已使用 nonce 的 LRU 缓存，防止重放攻击。

工作原理
========
重放攻击（Replay Attack）指攻击者截获合法请求后，原封不动地重新发送，
以骗取服务端响应。防重放的核心两道防线：

1. **时间窗口**：请求附带时间戳，服务端拒绝超出 ``time_window`` 秒的请求，
   使截获的请求在窗口外失效；
2. **nonce 唯一性**：每条请求附带一次性随机数（nonce），服务端记录已用
   nonce，同一 nonce 的重复请求直接拒绝。

两者结合即可抵御重放：即使攻击者在时间窗口内重放，nonce 也已用过。

LRU 缓存
========
nonce 缓存采用 LRU（Least Recently Used）策略，容量上限
``max_nonce_cache``。超出上限时淘汰最久未访问的 nonce，避免内存无限增长。
时间窗口外的 nonce 也会在过期清理时被淘汰。

典型用法
========

客户端创建带防重放信息的请求::

    manager = AntiReplayManager(time_window=300)
    request = manager.create_request({"action": "send", "content": "hello"})
    # request = {"timestamp": 1700000000, "nonce": "abc123...", "data": {...}}

服务端验证请求::

    if manager.verify_request(request):
        # 处理请求
        ...
    else:
        # 拒绝（时间窗口外 或 nonce 重复）
        ...
"""
from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from typing import Any, Optional

from loguru import logger


# ====================================================================== #
#  防重放管理器
# ====================================================================== #
class AntiReplayManager:
    """防重放攻击管理器。

    维护已使用 nonce 的 LRU 缓存，验证请求的时间窗口与 nonce 唯一性。

    Args:
        time_window: 时间窗口（秒），默认 300 秒（5 分钟）。
            超出窗口的请求视为过期，拒绝处理。
        max_nonce_cache: nonce 缓存最大容量，默认 10000。
            超出时按 LRU 策略淘汰最久未访问的 nonce。
    """

    def __init__(
        self,
        time_window: int = 300,
        max_nonce_cache: int = 10000,
    ) -> None:
        self.time_window: int = max(1, int(time_window))
        self.max_nonce_cache: int = max(100, int(max_nonce_cache))

        # LRU 缓存：nonce -> 接收时间戳
        # OrderedDict 维护插入顺序，move_to_end 实现 LRU
        self._nonce_cache: "OrderedDict[str, float]" = OrderedDict()
        self._lock_added: bool = False

    # ------------------------------------------------------------------ #
    #  生成器
    # ------------------------------------------------------------------ #
    def generate_nonce(self) -> str:
        """生成随机 nonce。

        使用 :mod:`secrets` 生成密码学安全的 32 字节随机数，
        返回 64 字符的十六进制字符串。

        Returns:
            64 字符 hex 随机 nonce。
        """
        return secrets.token_hex(32)

    def generate_timestamp(self) -> int:
        """生成当前时间戳（Unix 秒）。

        Returns:
            当前 Unix 时间戳（整数秒）。
        """
        return int(time.time())

    # ------------------------------------------------------------------ #
    #  验证
    # ------------------------------------------------------------------ #
    def validate(self, timestamp: int, nonce: str) -> bool:
        """验证请求是否有效（时间窗口 + nonce 唯一性）。

        验证流程：
        1. 时间戳为空或非整数 -> 拒绝；
        2. 时间戳超出 ``time_window`` 窗口 -> 拒绝（过期或未来太远）；
        3. nonce 为空 -> 拒绝；
        4. nonce 已在缓存中 -> 拒绝（重放）；
        5. 通过验证 -> 将 nonce 加入缓存，返回 True。

        Args:
            timestamp: 请求时间戳（Unix 秒）。
            nonce: 请求随机 nonce。

        Returns:
            有效返回 True，无效（过期/重放/格式错误）返回 False。
        """
        # 1. 时间戳格式校验
        if not isinstance(timestamp, (int, float)) or timestamp <= 0:
            logger.debug(f"防重放校验失败：时间戳无效 timestamp={timestamp}")
            return False

        # 2. 时间窗口校验（允许未来少量时钟偏差，上限为 time_window）
        now = int(time.time())
        ts = int(timestamp)
        if ts < now - self.time_window:
            logger.debug(
                f"防重放校验失败：请求过期 timestamp={ts} "
                f"now={now} window={self.time_window}"
            )
            return False
        if ts > now + self.time_window:
            logger.debug(
                f"防重放校验失败：时间戳超前过多 timestamp={ts} "
                f"now={now} window={self.time_window}"
            )
            return False

        # 3. nonce 格式校验
        if not nonce or not isinstance(nonce, str):
            logger.debug(f"防重放校验失败：nonce 为空")
            return False

        # 4. nonce 唯一性校验（重放检测）
        if nonce in self._nonce_cache:
            logger.warning(f"防重放校验失败：nonce 重复（重放攻击） nonce={nonce[:16]}...")
            return False

        # 5. 通过验证，记录 nonce
        self._add_nonce(nonce, float(ts))
        return True

    # ------------------------------------------------------------------ #
    #  请求创建 / 验证（封装）
    # ------------------------------------------------------------------ #
    def create_request(self, data: dict) -> dict:
        """创建带防重放信息的请求。

        在原始数据外层包装 ``timestamp`` 与 ``nonce`` 字段，原始数据放入
        ``data`` 字段。

        Args:
            data: 原始请求数据字典。

        Returns:
            包装后的请求::

                {
                    "timestamp": 1700000000,
                    "nonce": "a1b2c3...",
                    "data": {...}
                }
        """
        return {
            "timestamp": self.generate_timestamp(),
            "nonce": self.generate_nonce(),
            "data": data,
        }

    def verify_request(self, request: dict) -> bool:
        """验证请求的防重放信息。

        从请求中提取 ``timestamp`` 与 ``nonce``，调用 :meth:`validate`。
        验证通过后 nonce 会被记入缓存（防止同一请求再次通过）。

        Args:
            request: 待验证的请求字典，需含 ``timestamp`` 与 ``nonce``。

        Returns:
            验证通过返回 True，否则 False。
        """
        if not isinstance(request, dict):
            logger.debug("防重放校验失败：请求非字典")
            return False

        timestamp = request.get("timestamp")
        nonce = request.get("nonce")

        if timestamp is None or nonce is None:
            logger.debug("防重放校验失败：缺少 timestamp 或 nonce")
            return False

        return self.validate(int(timestamp), str(nonce))

    # ------------------------------------------------------------------ #
    #  缓存管理
    # ------------------------------------------------------------------ #
    def _add_nonce(self, nonce: str, timestamp: float) -> None:
        """将 nonce 加入 LRU 缓存，必要时淘汰最旧条目并清理过期项。"""
        self._nonce_cache[nonce] = timestamp
        # 将新加入的移到末尾（最近使用）
        self._nonce_cache.move_to_end(nonce)
        self._lock_added = True

        # 超容量时淘汰最旧的（LRU）
        while len(self._nonce_cache) > self.max_nonce_cache:
            evicted = self._nonce_cache.popitem(last=False)
            logger.debug(f"nonce 缓存淘汰（LRU）: {evicted[0][:16]}...")

        # 顺便清理过期 nonce（惰性清理）
        self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        """清理已超出时间窗口的过期 nonce（惰性清理）。"""
        now = int(time.time())
        cutoff = now - self.time_window
        expired = [
            n for n, ts in self._nonce_cache.items() if ts < cutoff
        ]
        for n in expired:
            self._nonce_cache.pop(n, None)
        if expired:
            logger.debug(f"清理过期 nonce {len(expired)} 条")

    def clear_cache(self) -> None:
        """清空 nonce 缓存。"""
        self._nonce_cache.clear()
        self._lock_added = False

    @property
    def cache_size(self) -> int:
        """当前 nonce 缓存中的条目数。"""
        return len(self._nonce_cache)

    # ------------------------------------------------------------------ #
    #  序列化（便于持久化 nonce 缓存）
    # ------------------------------------------------------------------ #
    def export_cache(self) -> list[dict[str, Any]]:
        """导出 nonce 缓存为可序列化列表。

        Returns:
            ``[{"nonce": ..., "timestamp": ...}, ...]`` 列表。
        """
        return [
            {"nonce": n, "timestamp": ts}
            for n, ts in self._nonce_cache.items()
        ]

    def import_cache(self, items: list[dict[str, Any]]) -> None:
        """从列表导入 nonce 缓存（合并，自动去重与清理过期）。

        Args:
            items: :meth:`export_cache` 导出的列表。
        """
        now = int(time.time())
        cutoff = now - self.time_window
        for item in items:
            n = item.get("nonce")
            ts = item.get("timestamp")
            if not n or ts is None:
                continue
            if ts < cutoff:
                continue  # 跳过已过期
            if n not in self._nonce_cache:
                self._nonce_cache[n] = float(ts)
                self._nonce_cache.move_to_end(n)
        # 超容量时淘汰
        while len(self._nonce_cache) > self.max_nonce_cache:
            self._nonce_cache.popitem(last=False)


# ====================================================================== #
#  自测入口
# ====================================================================== #
def _self_test() -> None:
    """防重放管理器自测。"""
    mgr = AntiReplayManager(time_window=300, max_nonce_cache=1000)

    # 生成 nonce 与时间戳
    nonce = mgr.generate_nonce()
    ts = mgr.generate_timestamp()
    assert len(nonce) == 64, f"nonce 长度应为 64, 实际 {len(nonce)}"

    # 首次验证通过
    assert mgr.validate(ts, nonce) is True, "首次验证应通过"
    # 重复 nonce 验证失败（重放）
    assert mgr.validate(ts, nonce) is False, "重复 nonce 应拒绝"
    # 过期时间戳验证失败
    assert mgr.validate(ts - 400, mgr.generate_nonce()) is False, "过期请求应拒绝"
    # 未来时间戳（超前太多）验证失败
    assert mgr.validate(ts + 400, mgr.generate_nonce()) is False, "超前请求应拒绝"

    # create_request / verify_request 往返
    req = mgr.create_request({"action": "test"})
    assert mgr.verify_request(req) is True, "verify_request 应通过"
    assert mgr.verify_request(req) is False, "重复请求应拒绝"

    logger.info(f"防重放自测通过，缓存大小: {mgr.cache_size}")


if __name__ == "__main__":
    _self_test()
