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
import json
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

from loguru import logger

# 可选依赖: aio-pika (真实 RabbitMQ 客户端), 缺失时 RabbitMQQueue 不可用并由工厂降级
try:  # pragma: no cover - 依赖外部库, 测试环境通常降级
    import aio_pika  # type: ignore[import-not-found]
    from aio_pika import IncomingMessage, Message as AioPikaMessage  # type: ignore[import-not-found]
    from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractExchange, AbstractQueue  # type: ignore[import-not-found]

    _HAS_AIO_PIKA: bool = True
except ImportError:  # pragma: no cover - 降级路径
    aio_pika = None  # type: ignore[assignment]
    IncomingMessage = None  # type: ignore[assignment,misc]
    AioPikaMessage = None  # type: ignore[assignment,misc]
    AbstractChannel = None  # type: ignore[assignment,misc]
    AbstractConnection = None  # type: ignore[assignment,misc]
    AbstractExchange = None  # type: ignore[assignment,misc]
    AbstractQueue = None  # type: ignore[assignment,misc]
    _HAS_AIO_PIKA = False

# 消费者回调类型: 接收消息, 返回 bool (True=ack, False=nack)
MessageCallback = Callable[["Message"], Awaitable[bool]]

# RabbitMQ 消费回调类型: 接收 (反序列化后的 payload, 原始 IncomingMessage), 无返回值
# 消费者通过调用 RabbitMQQueue.ack(message) 显式确认消息
RabbitMQCallback = Callable[[Any, Any], Awaitable[None]]


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


# ============================================================================
# RabbitMQ 真实消息队列 (基于 aio-pika)
# ============================================================================
class RabbitMQQueue:
    """基于 aio-pika 的真实 RabbitMQ 消息队列。

    对应原软件 AMQP 消息队列 (RabbitMQ), 消息流:
        消息接收 -> 入队 -> 工作线程处理 -> 结果入队 -> 发送线程发送

    原软件函数映射:
        - CreateChannel  -> connect() 内部创建 channel
        - BasicPublish   -> publish()
        - BasicConsume   -> consume()
        - AckMessage     -> ack()

    消息序列化使用 JSON。当 aio-pika 不可用时, 应通过 create_message_queue()
    工厂降级为内存队列 MessageQueue。

    用法::

        mq = RabbitMQQueue()
        await mq.connect("amqp://guest:guest@127.0.0.1/", "robot3")
        await mq.declare_queue("task_queue", durable=True)
        await mq.publish("task_queue", {"text": "hello"})

        async def cb(payload, message):
            print(payload)
            await mq.ack(message)

        await mq.consume("task_queue", cb)
        await mq.close()
    """

    def __init__(
        self,
        url: Optional[str] = None,
        exchange_name: str = "robot3",
        exchange_type: Optional[str] = "direct",
    ) -> None:
        """初始化 RabbitMQ 队列配置。

        Args:
            url: RabbitMQ 连接 URL (amqp://...), 可在 connect() 时再次指定
            exchange_name: 交换机名称
            exchange_type: 交换机类型 (direct / topic / fanout / headers)
        """
        if not _HAS_AIO_PIKA:
            raise RuntimeError(
                "aio-pika 不可用, 无法使用 RabbitMQQueue; 请安装 aio-pika 或改用 MessageQueue"
            )
        self._url: Optional[str] = url
        self._exchange_name: str = exchange_name
        self._exchange_type: str = exchange_type or "direct"
        self._connection: Optional[AbstractConnection] = None
        self._channel: Optional[AbstractChannel] = None
        self._exchange: Optional[AbstractExchange] = None
        self._queues: dict[str, AbstractQueue] = {}
        self._consumer_tags: dict[str, str] = {}
        self._connected: bool = False

    @property
    def is_connected(self) -> bool:
        """是否已连接到 RabbitMQ。"""
        return self._connected

    # ------------------------------------------------------------------
    # 连接 / 通道 / 交换机
    # ------------------------------------------------------------------
    async def connect(
        self,
        url: Optional[str] = None,
        exchange_name: Optional[str] = None,
    ) -> None:
        """连接到 RabbitMQ 并声明交换机 (对应原软件 CreateChannel)。

        Args:
            url: RabbitMQ 连接 URL, 未提供则使用构造函数中的值
            exchange_name: 交换机名称, 未提供则使用构造函数中的值

        Raises:
            RuntimeError: 未提供 URL 或 aio-pika 不可用
        """
        if not _HAS_AIO_PIKA:  # pragma: no cover - 由工厂避免
            raise RuntimeError("aio-pika 不可用, 无法连接 RabbitMQ")
        if url is not None:
            self._url = url
        if exchange_name is not None:
            self._exchange_name = exchange_name
        if not self._url:
            raise RuntimeError("未提供 RabbitMQ 连接 URL")

        # connect_robust 支持断线自动重连
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        # 声明持久化交换机
        exchange_type_map = {
            "direct": aio_pika.ExchangeType.DIRECT,
            "topic": aio_pika.ExchangeType.TOPIC,
            "fanout": aio_pika.ExchangeType.FANOUT,
            "headers": aio_pika.ExchangeType.HEADERS,
        }
        etype = exchange_type_map.get(
            self._exchange_type, aio_pika.ExchangeType.DIRECT
        )
        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, etype, durable=True
        )
        self._connected = True
        logger.info(
            f"已连接 RabbitMQ: url={self._url}, exchange={self._exchange_name} "
            f"({self._exchange_type})"
        )

    async def declare_queue(
        self,
        name: str,
        durable: bool = True,
        bind_routing_key: Optional[str] = None,
    ) -> Any:
        """声明队列 (可选绑定到交换机的路由键)。

        Args:
            name: 队列名称
            durable: 是否持久化
            bind_routing_key: 绑定到交换机的路由键, None 则使用 name

        Returns:
            声明的队列对象
        """
        if self._channel is None:
            raise RuntimeError("尚未连接 RabbitMQ, 请先调用 connect()")
        queue = await self._channel.declare_queue(name, durable=durable)
        routing_key = bind_routing_key if bind_routing_key is not None else name
        await queue.bind(self._exchange, routing_key=routing_key)
        self._queues[name] = queue
        logger.debug(f"已声明队列: {name} (durable={durable}, routing_key={routing_key})")
        return queue

    # ------------------------------------------------------------------
    # 发布 / 消费 / 确认 (对应原软件 BasicPublish / BasicConsume / AckMessage)
    # ------------------------------------------------------------------
    async def publish(
        self,
        routing_key: str,
        message: Any,
        *,
        persistent: bool = True,
    ) -> None:
        """发布消息到交换机 (对应原软件 BasicPublish)。

        Args:
            routing_key: 路由键
            message: 消息内容 (将被 JSON 序列化)
            persistent: 是否标记为持久化消息
        """
        if self._exchange is None:
            raise RuntimeError("尚未连接 RabbitMQ, 请先调用 connect()")
        body = json.dumps(message, ensure_ascii=False, default=str).encode("utf-8")
        delivery_mode = (
            aio_pika.DeliveryMode.PERSISTENT
            if persistent
            else aio_pika.DeliveryMode.NOT_PERSISTENT
        )
        amqp_message = aio_pika.Message(
            body=body,
            delivery_mode=delivery_mode,
            content_type="application/json",
        )
        await self._exchange.publish(amqp_message, routing_key=routing_key)
        logger.debug(f"已发布消息: routing_key={routing_key}, size={len(body)}B")

    async def consume(
        self,
        queue_name: str,
        callback: RabbitMQCallback,
        *,
        no_ack: bool = False,
    ) -> str:
        """消费队列消息 (对应原软件 BasicConsume)。

        消费者收到消息后, payload 已被 JSON 反序列化; 原始 IncomingMessage 一并传入,
        消费者处理完成后应调用 ack(message) 显式确认 (对应原软件 AckMessage)。

        Args:
            queue_name: 队列名称 (不存在则自动声明为持久化队列)
            callback: async 回调, 签名 (payload, message) -> None
            no_ack: 是否自动确认 (True 时无需手动 ack)

        Returns:
            consumer_tag
        """
        if self._channel is None:
            raise RuntimeError("尚未连接 RabbitMQ, 请先调用 connect()")
        queue = self._queues.get(queue_name)
        if queue is None:
            queue = await self.declare_queue(queue_name, durable=True)

        async def _wrapper(message: "IncomingMessage") -> None:
            try:
                payload = json.loads(message.body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.error(f"消息反序列化失败, 予以否认: {exc}")
                if not no_ack:
                    await message.nack(requeue=False)
                return
            try:
                await callback(payload, message)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"消费回调执行异常: {exc}")
                if not no_ack:
                    await message.nack(requeue=False)
                return
            if no_ack:
                # no_ack 模式下消息已自动确认
                return
            # 显式确认由消费者通过 ack() 调用完成, 此处不自动 ack

        consumer_tag = await queue.consume(_wrapper, no_ack=no_ack)
        self._consumer_tags[queue_name] = consumer_tag
        logger.info(f"已开始消费队列: {queue_name} (no_ack={no_ack})")
        return consumer_tag

    async def ack(self, message: Any) -> None:
        """确认消息 (对应原软件 AckMessage)。

        Args:
            message: consume 回调中收到的 IncomingMessage
        """
        if IncomingMessage is not None and isinstance(message, IncomingMessage):
            await message.ack()
            logger.debug("消息已确认 (ack)")
        else:
            logger.warning("ack: 收到非 IncomingMessage 对象, 忽略")

    async def nack(self, message: Any, requeue: bool = False) -> None:
        """否认消息 (可选重新入队)。"""
        if IncomingMessage is not None and isinstance(message, IncomingMessage):
            await message.nack(requeue=requeue)
            logger.debug(f"消息已否认 (nack, requeue={requeue})")
        else:
            logger.warning("nack: 收到非 IncomingMessage 对象, 忽略")

    # ------------------------------------------------------------------
    # 取消消费 / 关闭
    # ------------------------------------------------------------------
    async def cancel(self, queue_name: str) -> None:
        """取消某队列的消费。"""
        consumer_tag = self._consumer_tags.pop(queue_name, None)
        queue = self._queues.get(queue_name)
        if queue is not None and consumer_tag is not None:
            await queue.cancel(consumer_tag)
            logger.info(f"已取消消费队列: {queue_name}")

    async def close(self) -> None:
        """关闭 RabbitMQ 连接。"""
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"关闭 RabbitMQ 连接时异常: {exc}")
        self._channel = None
        self._exchange = None
        self._connection = None
        self._queues.clear()
        self._consumer_tags.clear()
        self._connected = False
        logger.info("RabbitMQ 连接已关闭")

    def get_status(self) -> dict:
        """获取队列状态。"""
        return {
            "backend": "rabbitmq",
            "url": self._url,
            "exchange": self._exchange_name,
            "exchange_type": self._exchange_type,
            "connected": self._connected,
            "queues": list(self._queues.keys()),
        }

    # 上下文管理支持
    async def __aenter__(self) -> "RabbitMQQueue":
        if not self._connected:
            await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()


# ============================================================================
# 工厂函数: 根据配置选择 RabbitMQ 或内存队列
# ============================================================================
def create_message_queue(
    config: Optional[dict] = None,
) -> Union[MessageQueue, "RabbitMQQueue"]:
    """根据配置创建消息队列。

    配置示例::

        # 内存队列 (降级)
        {"backend": "memory"}

        # RabbitMQ
        {
            "backend": "rabbitmq",
            "url": "amqp://guest:guest@127.0.0.1:5672/",
            "exchange": "robot3",
            "exchange_type": "direct",
        }

    Args:
        config: 配置字典, 默认使用内存队列

    Returns:
        MessageQueue (内存) 或 RabbitMQQueue (真实 RabbitMQ)
    """
    config = config or {}
    backend = config.get("backend", "memory")

    if backend == "rabbitmq":
        if not _HAS_AIO_PIKA:
            logger.warning(
                "aio-pika 不可用, RabbitMQ 队列不可用, 降级为内存消息队列 (MessageQueue)"
            )
            return MessageQueue()
        url = config.get("url", "amqp://guest:guest@127.0.0.1:5672/")
        exchange = config.get("exchange", "robot3")
        exchange_type = config.get("exchange_type", "direct")
        logger.info(f"创建 RabbitMQ 消息队列: exchange={exchange}")
        return RabbitMQQueue(url=url, exchange_name=exchange, exchange_type=exchange_type)

    # 默认 / memory
    max_retry = config.get("max_retry", 3)
    dead_letter_capacity = config.get("dead_letter_capacity", 1000)
    logger.info("创建内存消息队列 (MessageQueue)")
    return MessageQueue(max_retry=max_retry, dead_letter_capacity=dead_letter_capacity)


__all__ = [
    "Message",
    "MessageCallback",
    "MessageQueue",
    "RabbitMQCallback",
    "RabbitMQQueue",
    "create_message_queue",
    "message_queue",
]
