"""
消息处理管道 - 对应原软件的消息收发与处理流程

处理流程:
    消息进入(enqueue) -> 解析(_parse) -> 路由(_route) -> 处理(handler) -> 发送(_send)

特性:
- 异步消息队列 (asyncio.Queue)
- 处理器注册模式 (按 msg_type 注册, 支持 default handler)
- 消息分片 (msg_split): 超过 max_lines 的消息自动拆分为多条发送
- 发送间隔控制 (sleep_time): 分片之间自动 sleep, 防止风控
- 消息确认机制 (AckMessage): 处理 / 发送结果可通过 ack 回调通知
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

from loguru import logger

from config.instance_config import InstanceConfig


# ============================================================================
# 数据结构
# ============================================================================
class MessageState(str, Enum):
    """消息生命周期状态"""
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    SENT = "sent"
    FAILED = "failed"
    ACKED = "acked"


@dataclass
class ParsedMessage:
    """解析后的消息"""
    msg_id: str
    msg_type: str  # text / image / file / video / voice / system
    sender_wxid: str
    receiver_wxid: str
    content: str = ""
    is_received: bool = True  # True=收到 False=发出
    raw_xml: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class AckMessage:
    """消息确认回执"""
    msg_id: str
    status: MessageState
    error: str = ""
    timestamp: float = field(default_factory=time.time)


# 处理器类型: 接收 ParsedMessage 与所属管道
MessageHandler = Callable[["ParsedMessage", "MessagePipeline"], Awaitable[None]]
# 默认处理器 (未匹配 msg_type 时调用)
DefaultHandler = MessageHandler
# 发送回调: (receiver_wxid, content) -> 是否成功
SendCallback = Callable[[str, str], Awaitable[bool]]
# ACK 回调
AckCallback = Callable[[AckMessage], Awaitable[None]]


class MessagePipeline:
    """异步消息处理管道"""

    def __init__(self, config: InstanceConfig) -> None:
        self.config: InstanceConfig = config
        self._queue: asyncio.Queue[Union[ParsedMessage, dict]] = asyncio.Queue()
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._default_handler: Optional[DefaultHandler] = None
        self._send_callback: Optional[SendCallback] = None
        self._ack_callbacks: list[AckCallback] = []
        self._running: bool = False
        self._worker: Optional[asyncio.Task] = None
        # 统计
        self._enqueued: int = 0
        self._processed: int = 0
        self._sent: int = 0
        self._send_failed: int = 0
        logger.debug(f"[{config.instance_id}] 消息管道已创建")

    # ------------------------------------------------------------------
    # 处理器 / 回调注册
    # ------------------------------------------------------------------
    def register_handler(self, msg_type: str, handler: MessageHandler) -> None:
        """注册指定消息类型的处理器 (可注册多个)"""
        self._handlers.setdefault(msg_type, []).append(handler)
        logger.info(
            f"[{self.config.instance_id}] 已注册消息处理器: type={msg_type}, "
            f"handler={getattr(handler, '__name__', handler)}"
        )

    def register_default_handler(self, handler: DefaultHandler) -> None:
        """注册默认处理器 (未匹配到具体类型时调用)"""
        self._default_handler = handler
        logger.info(f"[{self.config.instance_id}] 已注册默认消息处理器")

    def set_send_callback(self, callback: SendCallback) -> None:
        """设置发送回调 (由微信网络层注入, 实际负责将消息发出)"""
        self._send_callback = callback
        logger.info(f"[{self.config.instance_id}] 已设置发送回调")

    def on_ack(self, callback: AckCallback) -> None:
        """注册消息确认回调"""
        self._ack_callbacks.append(callback)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动消息处理循环"""
        if self._running:
            logger.warning(f"[{self.config.instance_id}] 消息管道已在运行")
            return
        self._running = True
        self._worker = asyncio.create_task(self._process_loop(), name=f"pipeline-{self.config.instance_id}")
        logger.info(f"[{self.config.instance_id}] 消息管道已启动")

    async def stop(self) -> None:
        """停止消息处理循环"""
        if not self._running:
            return
        self._running = False
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        logger.info(
            f"[{self.config.instance_id}] 消息管道已停止 "
            f"(入队={self._enqueued}, 处理={self._processed}, "
            f"发送={self._sent}, 失败={self._send_failed})"
        )

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # 入队
    # ------------------------------------------------------------------
    async def enqueue(self, raw: dict) -> None:
        """消息进入 (原始字典, 将由 _parse 解析)"""
        await self._queue.put(raw)
        self._enqueued += 1
        logger.debug(f"[{self.config.instance_id}] 消息入队: {raw.get('msg_id')}")

    async def enqueue_message(self, message: ParsedMessage) -> None:
        """直接入队已解析的消息"""
        await self._queue.put(message)
        self._enqueued += 1
        logger.debug(f"[{self.config.instance_id}] 已解析消息入队: {message.msg_id}")

    # ------------------------------------------------------------------
    # 处理循环: 解析 -> 路由 -> 处理
    # ------------------------------------------------------------------
    async def _process_loop(self) -> None:
        """主处理循环"""
        logger.info(f"[{self.config.instance_id}] 消息处理循环已开始")
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                message = await self._parse(item)
                await self._route(message)
                self._processed += 1
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[{self.config.instance_id}] 消息处理异常: {e}")
            finally:
                self._queue.task_done()
        logger.info(f"[{self.config.instance_id}] 消息处理循环已退出")

    async def _parse(self, item: Union[ParsedMessage, dict]) -> ParsedMessage:
        """解析原始消息为 ParsedMessage

        流程: 消息进入 -> 解析
        """
        if isinstance(item, ParsedMessage):
            return item
        # 字典 -> ParsedMessage
        msg_id = str(item.get("msg_id") or item.get("MsgId") or uuid.uuid4().hex)
        msg_type = str(item.get("msg_type") or item.get("type") or "text").lower()
        sender = str(item.get("sender_wxid") or item.get("sender") or "")
        receiver = str(item.get("receiver_wxid") or item.get("receiver") or "")
        content = str(item.get("content") or item.get("Content") or "")
        is_received = bool(item.get("is_received", True))
        raw_xml = str(item.get("raw_xml") or item.get("xml") or "")
        extra = {
            k: v
            for k, v in item.items()
            if k
            not in {
                "msg_id", "MsgId", "msg_type", "type",
                "sender_wxid", "sender", "receiver_wxid", "receiver",
                "content", "Content", "is_received", "raw_xml", "xml",
            }
        }
        return ParsedMessage(
            msg_id=msg_id,
            msg_type=msg_type,
            sender_wxid=sender,
            receiver_wxid=receiver,
            content=content,
            is_received=is_received,
            raw_xml=raw_xml,
            extra=extra,
        )

    async def _route(self, message: ParsedMessage) -> None:
        """路由消息到对应处理器

        流程: 解析 -> 路由 -> 处理
        """
        await self.ack(message.msg_id, MessageState.PROCESSING)
        handlers = self._handlers.get(message.msg_type, [])
        matched = False
        for handler in handlers:
            matched = True
            try:
                await handler(message, self)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    f"[{self.config.instance_id}] 处理器异常 "
                    f"type={message.msg_type}, handler={getattr(handler, '__name__', handler)}: {e}"
                )
        # 未匹配到具体处理器, 走默认处理器
        if not matched and self._default_handler is not None:
            try:
                await self._default_handler(message, self)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    f"[{self.config.instance_id}] 默认处理器异常: {e}"
                )
        await self.ack(message.msg_id, MessageState.PROCESSED)

    # ------------------------------------------------------------------
    # 发送: 分片 + 间隔控制
    # ------------------------------------------------------------------
    def _split_message(self, content: str, max_lines: int) -> list[str]:
        """消息分片: 超过 max_lines 行的消息自动拆分为多条

        流程: 处理 -> 发送 (msg_split)
        """
        if not content:
            return []
        lines = content.split("\n")
        if len(lines) <= max_lines:
            return [content]
        fragments: list[str] = []
        for i in range(0, len(lines), max_lines):
            fragments.append("\n".join(lines[i : i + max_lines]))
        logger.debug(
            f"[{self.config.instance_id}] 消息分片: {len(lines)} 行 -> {len(fragments)} 片"
        )
        return fragments

    async def send(self, content: str, receiver_wxid: str) -> bool:
        """发送消息 (自动分片 + 间隔控制)

        流程: 处理 -> 发送
        Returns: 所有分片是否全部发送成功
        """
        if not content:
            logger.warning(f"[{self.config.instance_id}] 发送内容为空, 跳过")
            return False

        if self.config.msg_split_enabled:
            fragments = self._split_message(content, self.config.msg_max_lines)
        else:
            fragments = [content]

        if not fragments:
            return False

        all_ok = True
        for index, fragment in enumerate(fragments):
            # 分片之间按配置间隔发送 (sleep_time), 防止风控
            if index > 0:
                await asyncio.sleep(self.config.msg_sleep_sec)
            ok = await self._send(fragment, receiver_wxid)
            if ok:
                self._sent += 1
                await self.ack(f"send-{uuid.uuid4().hex[:8]}", MessageState.SENT)
            else:
                self._send_failed += 1
                all_ok = False
                logger.warning(
                    f"[{self.config.instance_id}] 发送失败 "
                    f"receiver={receiver_wxid}, 片段={index + 1}/{len(fragments)}"
                )
                await self.ack(
                    f"send-{uuid.uuid4().hex[:8]}",
                    MessageState.FAILED,
                    error=f"片段 {index + 1} 发送失败",
                )
        return all_ok

    async def _send(self, content: str, receiver_wxid: str) -> bool:
        """实际发送 (调用注入的发送回调)"""
        if self._send_callback is None:
            logger.debug(
                f"[{self.config.instance_id}] 发送回调未注册, 模拟发送 -> "
                f"{receiver_wxid}: {content[:50]!r}"
            )
            return True  # 未注册回调时视为成功 (便于测试)
        try:
            return bool(await self._send_callback(receiver_wxid, content))
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[{self.config.instance_id}] 发送回调异常: {e}")
            return False

    # ------------------------------------------------------------------
    # 消息确认机制
    # ------------------------------------------------------------------
    async def ack(
        self,
        msg_id: str,
        status: MessageState,
        error: str = "",
    ) -> None:
        """发出消息确认"""
        ack = AckMessage(msg_id=msg_id, status=status, error=error)
        for callback in self._ack_callbacks:
            try:
                await callback(ack)
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[{self.config.instance_id}] ACK 回调异常: {e}")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    async def wait_drained(self, timeout: Optional[float] = None) -> None:
        """等待队列中所有消息处理完成"""
        if timeout is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.config.instance_id}] 等待队列排空超时")
        else:
            await self._queue.join()

    def get_status(self) -> dict:
        """获取消息管道状态"""
        return {
            "instance_id": self.config.instance_id,
            "running": self._running,
            "queue_size": self._queue.qsize(),
            "enqueued": self._enqueued,
            "processed": self._processed,
            "sent": self._sent,
            "send_failed": self._send_failed,
            "handlers": {k: len(v) for k, v in self._handlers.items()},
            "has_default_handler": self._default_handler is not None,
            "has_send_callback": self._send_callback is not None,
        }


__all__ = [
    "MessageState",
    "ParsedMessage",
    "AckMessage",
    "MessagePipeline",
    "MessageHandler",
    "SendCallback",
    "AckCallback",
]
