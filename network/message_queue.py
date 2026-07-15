"""
本地异步消息队列 - 基于 asyncio.Queue 的发布/订阅实现

特性:
  - 发布/订阅 (topic 模式)
  - 多消费者 (同一 topic 多个回调同时消费)
  - 消息确认 (ack/nack)
  - 死信队列 (nack 超过最大重试次数的消息进入死信队列)
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

# 消费者回调类型: 接收消息, 返回 bool (True=ack, False=nack)
MessageCallback = Callable[["Message"], Awaitable[bool]]


@dataclass
class Message:
    """队列消息"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    topic: str = ""
    payload: Any = None
    created_at: float = field(default_factory=time.time)
    retry_count: int = 0
    # 消费该消息的消费者 ID, 用于 ack/nack 确认
    consumer_tag: str = ""

    def ack(self) -> None:
        """确认消息 (标记在 _inflight 中移除)"""
        # 实际确认逻辑由队列统一处理, 这里仅记录语义
        self._acked = True

    def nack(self) -> None:
        """否认消息 (将触发重投或进入死信队列)"""
        self._acked = False

    _acked: bool = False


@dataclass
class _PendingDelivery:
    """一次待确认的投递"""
    message: Message
    consumer_tag: str
    callback: MessageCallback


class MessageQueue:
    """
    异步消息队列 (单进程内, 多消费者广播)

    用法:
        mq = MessageQueue()
        await mq.subscribe("chat", my_callback)
        await mq.publish("chat", {"text": "hello"})
    """

    def __init__(self, max_retry: int = 3, dead_letter_capacity: int = 1000) -> None:
        self._subscribers: dict[str, dict[str, MessageCallback]] = defaultdict(dict)
        # 每个 topic 维护一个投递队列, 顺序消费并等待确认
        self._inflight: dict[str, deque[_PendingDelivery]] = defaultdict(deque)
        self._dead_letter: asyncio.Queue = asyncio.Queue(maxsize=dead_letter_capacity)
        self._max_retry: int = max_retry
        self._consumer_counter: int = 0
        self._tasks: list[asyncio.Task] = []
        self._running: bool = False
        # 同步消息缓冲 (供非异步消费者读取最近消息)
        self._recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))

    # ======================== 订阅 / 取消订阅 ========================

    def subscribe(self, topic: str, callback: MessageCallback) -> str:
        """
        订阅 topic, 返回 consumer_tag (用于取消订阅)

        callback: async 函数, 接收 Message, 返回 True(ack) / False(nack)
        """
        self._consumer_counter += 1
        consumer_tag = f"ctag-{self._consumer_counter}"
        self._subscribers[topic][consumer_tag] = callback
        logger.debug(f"订阅 topic={topic} consumer_tag={consumer_tag}")
        return consumer_tag

    def unsubscribe(self, topic: str, consumer_tag: str) -> None:
        """取消订阅"""
        self._subscribers.get(topic, {}).pop(consumer_tag, None)

    def subscriber_count(self, topic: str) -> int:
        """返回某 topic 的消费者数量"""
        return len(self._subscribers.get(topic, {}))

    # ======================== 发布 ========================

    async def publish(self, topic: str, payload: Any) -> str:
        """
        发布消息到 topic, 广播给所有订阅者

        返回 message_id
        """
        msg = Message(topic=topic, payload=payload)
        # 记录最近消息 (供 REST 接口拉取)
        self._recent[topic].append(msg)
        subscribers = self._subscribers.get(topic, {})
        if not subscribers:
            logger.debug(f"发布消息到 topic={topic}, 无订阅者, 仅缓存")
            return msg.id
        # 为每个消费者创建一份投递任务
        for consumer_tag, callback in subscribers.items():
            delivery = _PendingDelivery(
                message=Message(
                    id=msg.id, topic=topic, payload=payload,
                    created_at=msg.created_at, consumer_tag=consumer_tag,
                ),
                consumer_tag=consumer_tag,
                callback=callback,
            )
            self._inflight[topic].append(delivery)
        # 启动消费协程 (若未运行)
        self._ensure_dispatcher(topic)
        return msg.id

    # ======================== 消费 / 确认 ========================

    def _ensure_dispatcher(self, topic: str) -> None:
        """确保该 topic 的分发协程在运行"""
        if not self._running:
            self._running = True
        # 每个 topic 一个常驻分发协程
        task = asyncio.create_task(self._dispatch_loop(topic))
        self._tasks.append(task)

    async def _dispatch_loop(self, topic: str) -> None:
        """顺序处理某 topic 的待确认投递"""
        queue = self._inflight[topic]
        while True:
            if not queue:
                await asyncio.sleep(0.05)
                continue
            delivery = queue.popleft()
            try:
                ok = await delivery.callback(delivery.message)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"消费者 {delivery.consumer_tag} 处理消息异常: {exc}"
                )
                ok = False

            if ok:
                continue  # ack, 消费成功

            # nack: 重试
            delivery.message.retry_count += 1
            if delivery.message.retry_count > self._max_retry:
                logger.error(
                    f"消息 {delivery.message.id} 达到最大重试次数, 进入死信队列"
                )
                await self._push_dead_letter(delivery.message)
            else:
                # 重新放回队列尾部稍后重试
                await asyncio.sleep(0.1)
                queue.append(delivery)

    async def _push_dead_letter(self, message: Message) -> None:
        """将消息放入死信队列"""
        try:
            self._dead_letter.put_nowait(message)
        except asyncio.QueueFull:
            logger.error("死信队列已满, 丢弃最早消息")
            try:
                self._dead_letter.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dead_letter.put_nowait(message)

    # ======================== 死信 / 最近消息查询 ========================

    def dead_letter_count(self) -> int:
        """返回死信队列中消息数量"""
        return self._dead_letter.qsize()

    async def drain_dead_letter(self, limit: int = 100) -> list[Message]:
        """取出死信队列中的消息 (用于排查)"""
        drained: list[Message] = []
        for _ in range(limit):
            try:
                drained.append(self._dead_letter.get_nowait())
            except asyncio.QueueEmpty:
                break
        return drained

    def recent(self, topic: str, limit: int = 50) -> list[Message]:
        """返回某 topic 最近的消息 (只读快照)"""
        items = list(self._recent.get(topic, []))
        return items[-limit:]

    # ======================== 生命周期 ========================

    async def start(self) -> None:
        """启动队列 (标记运行状态)"""
        self._running = True
        logger.info("消息队列已启动")

    async def stop(self) -> None:
        """停止队列, 取消所有分发协程"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("消息队列已停止")


# 全局单例
message_queue = MessageQueue()
