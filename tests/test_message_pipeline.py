"""
消息管道单元测试

测试范围:
  - core/message_pipeline.py : 异步消息处理管道

测试内容:
  - 消息入队 (enqueue)
  - 长消息分片 (超过 max_lines 自动分多条)
  - 发送限速 (sleep_time 间隔)
  - 消息处理器注册和回调
  - 消息确认 (ACK) 机制
  - 管道启动停止
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

from config.instance_config import InstanceConfig
from core.message_pipeline import (
    MessagePipeline,
    MessageState,
    ParsedMessage,
    AckMessage,
)


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


def _make_config(
    msg_split_enabled: bool = True,
    msg_max_lines: int = 5,
    msg_sleep_sec: float = 0.01,
) -> InstanceConfig:
    """创建测试用实例配置"""
    return InstanceConfig(
        instance_id="pipeline_test",
        display_name="管道测试实例",
        msg_split_enabled=msg_split_enabled,
        msg_max_lines=msg_max_lines,
        msg_sleep_sec=msg_sleep_sec,
    )


# ============================================================================
# 测试: 消息入队
# ============================================================================
def test_message_enqueue():
    """测试消息入队"""
    config = _make_config()

    async def _run_test():
        pipeline = MessagePipeline(config)

        # 入队原始字典消息
        await pipeline.enqueue({
            "msg_id": "test_001",
            "msg_type": "text",
            "sender_wxid": "wxid_sender",
            "receiver_wxid": "wxid_receiver",
            "content": "你好",
        })

        # 验证入队计数
        assert pipeline._enqueued == 1, "入队计数不正确"

        # 入队已解析消息
        parsed = ParsedMessage(
            msg_id="test_002",
            msg_type="text",
            sender_wxid="wxid_sender2",
            receiver_wxid="wxid_receiver2",
            content="世界",
        )
        await pipeline.enqueue_message(parsed)
        assert pipeline._enqueued == 2, "入队计数不正确"

        # 验证队列大小
        status = pipeline.get_status()
        assert status["queue_size"] == 2, f"队列大小不正确: {status}"
        assert status["enqueued"] == 2

    _run(_run_test())


# ============================================================================
# 测试: 消息分片
# ============================================================================
def test_message_split():
    """测试长消息分片 (超过 max_lines 自动分多条)"""
    config = _make_config(msg_max_lines=3, msg_sleep_sec=0.0)

    async def _run_test():
        pipeline = MessagePipeline(config)

        # 生成超过 max_lines 的消息
        long_text = "\n".join([f"第{i}行" for i in range(1, 8)])  # 7 行, max_lines=3
        fragments = pipeline._split_message(long_text, 3)

        # 7 行 / 3 行每片 = 3 片 (3+3+1)
        assert len(fragments) == 3, f"分片数量不正确: {len(fragments)}"
        assert fragments[0] == "第1行\n第2行\n第3行"
        assert fragments[1] == "第4行\n第5行\n第6行"
        assert fragments[2] == "第7行"

        # 不超过 max_lines 的消息不拆分
        short_text = "第1行\n第2行"
        fragments_short = pipeline._split_message(short_text, 3)
        assert len(fragments_short) == 1

        # 空消息返回空列表
        assert pipeline._split_message("", 3) == []

        # 恰好等于 max_lines 不拆分
        exact_text = "\n".join([f"第{i}行" for i in range(1, 4)])  # 3 行
        fragments_exact = pipeline._split_message(exact_text, 3)
        assert len(fragments_exact) == 1

    _run(_run_test())


def test_message_split_disabled():
    """测试关闭分片时不拆分"""
    config = _make_config(msg_split_enabled=False, msg_max_lines=2)

    async def _run_test():
        pipeline = MessagePipeline(config)

        # send 方法在 msg_split_enabled=False 时不会分片
        sent_fragments = []

        async def mock_send(wxid, content):
            sent_fragments.append(content)
            return True

        pipeline.set_send_callback(mock_send)

        long_text = "\n".join([f"第{i}行" for i in range(1, 10)])
        await pipeline.send(long_text, "wxid_receiver")

        # 未启用分片, 应该只发送 1 条
        assert len(sent_fragments) == 1, f"关闭分片后应只发送1条, 实际 {len(sent_fragments)}"

    _run(_run_test())


# ============================================================================
# 测试: 发送限速
# ============================================================================
def test_send_throttle():
    """测试发送限速 (sleep_time 间隔)"""
    # 设置较大的 sleep 间隔以便检测
    config = _make_config(msg_max_lines=2, msg_sleep_sec=0.15)

    async def _run_test():
        pipeline = MessagePipeline(config)

        sent_times = []

        async def mock_send(wxid, content):
            sent_times.append(time.monotonic())
            return True

        pipeline.set_send_callback(mock_send)

        # 4 行消息, max_lines=2, 应分 2 片, 片间 sleep 0.15s
        text = "第1行\n第2行\n第3行\n第4行"
        await pipeline.send(text, "wxid_receiver")

        # 应发送 2 片
        assert len(sent_times) == 2, f"应发送2片, 实际 {len(sent_times)}"

        # 验证两片之间有时间间隔 (至少 sleep_sec 的大部分)
        if len(sent_times) >= 2:
            interval = sent_times[1] - sent_times[0]
            assert interval >= 0.10, f"发送间隔过短: {interval:.3f}s, 预期 >= 0.10s"

    _run(_run_test())


# ============================================================================
# 测试: 处理器注册和回调
# ============================================================================
def test_handler_registration():
    """测试消息处理器注册和回调"""
    config = _make_config()

    async def _run_test():
        pipeline = MessagePipeline(config)

        # 注册处理器
        received_messages = []

        async def text_handler(message: ParsedMessage, pipe: MessagePipeline):
            received_messages.append(message)

        pipeline.register_handler("text", text_handler)

        # 注册默认处理器
        default_received = []

        async def default_handler(message: ParsedMessage, pipe: MessagePipeline):
            default_received.append(message)

        pipeline.register_default_handler(default_handler)

        # 启动管道
        await pipeline.start()
        assert pipeline.running is True

        # 入队一条 text 消息
        await pipeline.enqueue({
            "msg_id": "handler_001",
            "msg_type": "text",
            "sender_wxid": "wxid_sender",
            "receiver_wxid": "wxid_receiver",
            "content": "测试处理器",
        })

        # 等待处理完成
        await pipeline.wait_drained(timeout=2.0)

        # 验证 text 处理器被调用
        assert len(received_messages) == 1, f"text处理器应被调用1次, 实际 {len(received_messages)}"
        assert received_messages[0].content == "测试处理器"

        # 入队一条 image 消息 (未注册 image 处理器, 走默认处理器)
        await pipeline.enqueue({
            "msg_id": "handler_002",
            "msg_type": "image",
            "sender_wxid": "wxid_sender",
            "receiver_wxid": "wxid_receiver",
            "content": "[图片]",
        })

        await pipeline.wait_drained(timeout=2.0)

        # 验证默认处理器被调用
        assert len(default_received) == 1, f"默认处理器应被调用1次, 实际 {len(default_received)}"

        await pipeline.stop()
        assert pipeline.running is False

    _run(_run_test())


def test_handler_multiple_registration():
    """测试同一类型注册多个处理器"""
    config = _make_config()

    async def _run_test():
        pipeline = MessagePipeline(config)

        call_order = []

        async def handler_a(message, pipe):
            call_order.append("A")

        async def handler_b(message, pipe):
            call_order.append("B")

        pipeline.register_handler("text", handler_a)
        pipeline.register_handler("text", handler_b)

        # 验证注册了 2 个处理器
        status = pipeline.get_status()
        assert status["handlers"].get("text") == 2

        await pipeline.start()
        await pipeline.enqueue({
            "msg_id": "multi_001",
            "msg_type": "text",
            "sender_wxid": "s",
            "receiver_wxid": "r",
            "content": "多处理器测试",
        })
        await pipeline.wait_drained(timeout=2.0)

        # 两个处理器都应被调用
        assert len(call_order) == 2
        assert "A" in call_order
        assert "B" in call_order

        await pipeline.stop()

    _run(_run_test())


# ============================================================================
# 测试: ACK 机制
# ============================================================================
def test_ack_mechanism():
    """测试消息确认 (ACK) 机制"""
    config = _make_config()

    async def _run_test():
        pipeline = MessagePipeline(config)

        # 注册 ACK 回调
        acks = []

        async def on_ack(ack: AckMessage):
            acks.append(ack)

        pipeline.on_ack(on_ack)

        # 启动管道
        await pipeline.start()

        # 入队消息, 处理过程中会产生 PROCESSING 和 PROCESSED 两个 ACK
        await pipeline.enqueue({
            "msg_id": "ack_001",
            "msg_type": "text",
            "sender_wxid": "s",
            "receiver_wxid": "r",
            "content": "ACK测试",
        })

        await pipeline.wait_drained(timeout=2.0)

        # 验证 ACK 被触发 (至少 PROCESSING 和 PROCESSED)
        ack_statuses = [a.status for a in acks]
        assert MessageState.PROCESSING in ack_statuses, "缺少 PROCESSING ACK"
        assert MessageState.PROCESSED in ack_statuses, "缺少 PROCESSED ACK"

        await pipeline.stop()

    _run(_run_test())


def test_ack_send_callback():
    """测试发送时的 ACK 机制"""
    config = _make_config(msg_max_lines=2, msg_sleep_sec=0.0)

    async def _run_test():
        pipeline = MessagePipeline(config)

        acks = []

        async def on_ack(ack: AckMessage):
            acks.append(ack)

        pipeline.on_ack(on_ack)

        async def mock_send(wxid, content):
            return True

        pipeline.set_send_callback(mock_send)

        # 发送分片消息, 应产生 SENT ACK
        text = "第1行\n第2行\n第3行\n第4行"
        result = await pipeline.send(text, "wxid_receiver")
        assert result is True

        # 验证有 SENT 状态的 ACK
        sent_acks = [a for a in acks if a.status == MessageState.SENT]
        assert len(sent_acks) == 2, f"应有2个SENT ACK, 实际 {len(sent_acks)}"

    _run(_run_test())


def test_ack_send_failure():
    """测试发送失败时的 ACK"""
    config = _make_config(msg_max_lines=2, msg_sleep_sec=0.0)

    async def _run_test():
        pipeline = MessagePipeline(config)

        acks = []

        async def on_ack(ack: AckMessage):
            acks.append(ack)

        pipeline.on_ack(on_ack)

        async def mock_send_fail(wxid, content):
            return False

        pipeline.set_send_callback(mock_send_fail)

        text = "第1行\n第2行\n第3行"
        result = await pipeline.send(text, "wxid_receiver")
        assert result is False, "发送失败应返回 False"

        # 验证有 FAILED 状态的 ACK
        failed_acks = [a for a in acks if a.status == MessageState.FAILED]
        assert len(failed_acks) >= 1, "应有至少1个FAILED ACK"

    _run(_run_test())


# ============================================================================
# 测试: 管道启动停止
# ============================================================================
def test_pipeline_start_stop():
    """测试管道启动停止"""
    config = _make_config()

    async def _run_test():
        pipeline = MessagePipeline(config)

        # 初始状态: 未运行
        assert pipeline.running is False

        # 启动
        await pipeline.start()
        assert pipeline.running is True
        assert pipeline._worker is not None

        # 重复启动 (应忽略)
        await pipeline.start()
        assert pipeline.running is True

        # 停止
        await pipeline.stop()
        assert pipeline.running is False
        assert pipeline._worker is None

        # 重复停止 (应忽略)
        await pipeline.stop()
        assert pipeline.running is False

    _run(_run_test())


def test_pipeline_status():
    """测试管道状态获取"""
    config = _make_config()

    async def _run_test():
        pipeline = MessagePipeline(config)

        async def handler(msg, pipe):
            pass

        pipeline.register_handler("text", handler)
        pipeline.set_send_callback(lambda wxid, content: asyncio.sleep(0))

        status = pipeline.get_status()
        assert status["instance_id"] == "pipeline_test"
        assert status["running"] is False
        assert status["handlers"]["text"] == 1
        assert status["has_send_callback"] is True
        assert status["has_default_handler"] is False
        assert "enqueued" in status
        assert "processed" in status
        assert "sent" in status

    _run(_run_test())


def test_pipeline_empty_send():
    """测试发送空消息"""
    config = _make_config()

    async def _run_test():
        pipeline = MessagePipeline(config)
        result = await pipeline.send("", "wxid_receiver")
        assert result is False, "发送空消息应返回 False"

    _run(_run_test())


def test_pipeline_no_callback_send():
    """测试未注册发送回调时的发送 (模拟发送)"""
    config = _make_config(msg_sleep_sec=0.0)

    async def _run_test():
        pipeline = MessagePipeline(config)
        # 不设置 send_callback, _send 返回 True (模拟发送)
        result = await pipeline.send("测试消息", "wxid_receiver")
        assert result is True, "未注册回调时应模拟发送成功"

    _run(_run_test())
