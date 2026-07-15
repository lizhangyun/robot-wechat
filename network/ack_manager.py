"""
AckMessage 确认机制 - 对应原软件的消息发送 ACK 确认流程

原软件在消息发送后等待 ACK 确认：
  - 消息发送后，底层 Hook 会返回一个 ACK 信号确认消息已成功投递；
  - 未收到 ACK 的消息会重试发送（最多 max_retries 次）；
  - 记账确认消息同样受此机制保护，确保用户收到记账成功通知。

本模块基于 ``asyncio.Event`` 实现轻量级的 ACK 等待与确认机制，
适用于单进程内的异步消息发送场景。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger


class AckManager:
    """消息确认机制，对应原软件 AckMessage。

    工作流程：
      1. ``send_with_ack(send_func, msg_id, ...)`` 调用 ``send_func`` 发送消息；
      2. 若 ``auto_ack=True``（默认），发送成功即视为收到 ACK；
      3. 若 ``auto_ack=False``，等待外部调用 ``confirm(msg_id)`` 确认 ACK；
      4. 超时未收到 ACK 则重试发送，最多 ``max_retries`` 次。

    Args:
        timeout: 单次 ACK 等待超时秒数（默认 5.0）。
        max_retries: 最大重试次数（默认 3）。
        auto_ack: 是否在发送成功后自动确认 ACK（默认 True）。
                  模拟模式或不支持异步 ACK 的客户端应设为 True；
                  真实 Hook 模式可设为 False 以等待底层 ACK 信号。
    """

    def __init__(
        self,
        timeout: float = 5.0,
        max_retries: int = 3,
        auto_ack: bool = True,
    ) -> None:
        if timeout < 0:
            raise ValueError("timeout 必须 >= 0")
        if max_retries < 0:
            raise ValueError("max_retries 必须 >= 0")
        self.timeout: float = timeout
        self.max_retries: int = max_retries
        self.auto_ack: bool = auto_ack
        # msg_id -> asyncio.Event
        self._events: dict[str, asyncio.Event] = {}
        # msg_id -> 发送结果（成功/失败）
        self._results: dict[str, bool] = {}
        # 已完成的 msg_id 集合（用于过期清理）
        self._completed: set[str] = set()
        self._max_cache: int = 5000

    # ------------------------------------------------------------------ #
    #  发送并等待 ACK
    # ------------------------------------------------------------------ #
    async def send_with_ack(
        self,
        send_func: Callable[..., Awaitable[Any]],
        msg_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        """发送消息并等待 ACK 确认，超时重试。

        Args:
            send_func: 异步发送函数，调用方式为 ``await send_func(*args, **kwargs)``。
                       返回值应具有 ``success`` 属性（如 ``SendResult``），
                       或返回 truthy 值表示成功。
            msg_id: 消息 ID（用于关联 ACK）。
            *args, **kwargs: 传递给 ``send_func`` 的参数。

        Returns:
            是否最终成功（发送成功且收到 ACK）。
        """
        # 准备 ACK 事件
        event = asyncio.Event()
        self._events[msg_id] = event

        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await send_func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning(
                    f"消息 {msg_id} 发送异常 (第 {attempt + 1} 次): {exc}"
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(min(self.timeout, 1.0))
                    continue
                self._cleanup(msg_id, False)
                return False

            # 判断发送是否成功
            success = self._is_success(result)

            if not success:
                last_error = getattr(result, "error", None) or "发送返回失败"
                logger.warning(
                    f"消息 {msg_id} 发送失败 (第 {attempt + 1} 次): {last_error}"
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(min(self.timeout, 1.0))
                    continue
                self._cleanup(msg_id, False)
                return False

            # 发送成功
            if self.auto_ack:
                # 自动确认：发送成功即视为 ACK
                self._cleanup(msg_id, True)
                logger.debug(f"消息 {msg_id} 发送成功 (auto_ack)")
                return True

            # 等待外部 ACK 确认
            event.clear()
            acked = await self.wait_ack(msg_id, self.timeout)
            if acked:
                logger.debug(f"消息 {msg_id} 收到 ACK (第 {attempt + 1} 次发送)")
                self._cleanup(msg_id, True)
                return True

            # ACK 超时，重试
            logger.warning(
                f"消息 {msg_id} 等待 ACK 超时 (第 {attempt + 1} 次), "
                f"剩余重试 {self.max_retries - attempt} 次"
            )

        # 所有重试耗尽
        self._cleanup(msg_id, False)
        return False

    # ------------------------------------------------------------------ #
    #  ACK 确认
    # ------------------------------------------------------------------ #
    async def confirm(self, msg_id: str) -> None:
        """确认消息已收到 ACK。

        外部 ACK 接收器在收到底层 ACK 信号后调用此方法，
        通知 ``send_with_ack`` 消息已被确认。

        Args:
            msg_id: 已确认的消息 ID。
        """
        event = self._events.get(msg_id)
        if event is not None:
            event.set()
            logger.debug(f"消息 {msg_id} ACK 已确认")
        else:
            # 可能是超时后才到达的 ACK，记录但不报错
            logger.debug(f"消息 {msg_id} ACK 到达但已超时清理")

    async def wait_ack(self, msg_id: str, timeout: float) -> bool:
        """等待指定消息的 ACK。

        Args:
            msg_id: 消息 ID。
            timeout: 等待超时秒数。

        Returns:
            是否在超时前收到 ACK。
        """
        event = self._events.get(msg_id)
        if event is None:
            return False
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------ #
    #  状态查询
    # ------------------------------------------------------------------ #
    def is_acked(self, msg_id: str) -> bool:
        """查询消息是否已确认 ACK。"""
        return self._results.get(msg_id, False)

    def pending_count(self) -> int:
        """当前等待 ACK 的消息数量。"""
        return len(self._events) - len(self._completed)

    # ------------------------------------------------------------------ #
    #  内部方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_success(result: Any) -> bool:
        """判断发送结果是否成功。

        支持 ``SendResult``（有 ``success`` 属性）、``bool``、``dict`` 等类型。
        """
        if result is None:
            return False
        if hasattr(result, "success"):
            return bool(result.success)
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            return result.get("success", False) or result.get("code", -1) == 0
        return bool(result)

    def _cleanup(self, msg_id: str, success: bool) -> None:
        """清理消息的 ACK 跟踪状态。"""
        self._results[msg_id] = success
        self._completed.add(msg_id)
        # 延迟移除事件引用（允许 wait_ack 的最后查询）
        if len(self._completed) > self._max_cache:
            # 清理最早的一半已完成记录
            to_remove = list(self._completed)[: self._max_cache // 2]
            for mid in to_remove:
                self._events.pop(mid, None)
                self._results.pop(mid, None)
                self._completed.discard(mid)

    @staticmethod
    def generate_msg_id(prefix: str = "msg") -> str:
        """生成唯一消息 ID。"""
        return f"{prefix}_{uuid.uuid4().hex[:16]}"
