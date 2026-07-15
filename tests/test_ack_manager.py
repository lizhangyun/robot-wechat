"""
AckManager 确认机制单元测试

测试范围:
  - network/ack_manager.py : 消息发送 ACK 确认机制

测试内容:
  - AckManager 初始化与参数校验
  - send_with_ack() auto_ack 模式 (发送即确认)
  - confirm() 手动确认
  - wait_ack() 超时处理
  - 重试机制 (max_retries)
  - send_with_ack() 失败后重试成功
  - is_acked() / pending_count() 状态查询
  - generate_msg_id() 消息 ID 生成

对应原软件 AckMessage 消息发送确认流程。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from network.ack_manager import AckManager
from wechat.message_types import SendResult


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


# ============================================================================
# 测试: 初始化
# ============================================================================
def test_init_default():
    """默认初始化"""
    mgr = AckManager()
    assert mgr.timeout == 5.0
    assert mgr.max_retries == 3
    assert mgr.auto_ack is True


def test_init_custom_params():
    """自定义参数初始化"""
    mgr = AckManager(timeout=10.0, max_retries=5, auto_ack=False)
    assert mgr.timeout == 10.0
    assert mgr.max_retries == 5
    assert mgr.auto_ack is False


def test_init_invalid_timeout():
    """timeout < 0 抛 ValueError"""
    with pytest.raises(ValueError):
        AckManager(timeout=-1.0)


def test_init_invalid_max_retries():
    """max_retries < 0 抛 ValueError"""
    with pytest.raises(ValueError):
        AckManager(max_retries=-1)


def test_init_zero_timeout():
    """timeout=0 允许"""
    mgr = AckManager(timeout=0)
    assert mgr.timeout == 0


def test_init_zero_retries():
    """max_retries=0 允许 (不重试)"""
    mgr = AckManager(max_retries=0)
    assert mgr.max_retries == 0


# ============================================================================
# 测试: send_with_ack auto_ack 模式
# ============================================================================
def test_send_with_ack_auto_ack_success():
    """auto_ack 模式下发送成功即确认"""
    mgr = AckManager(auto_ack=True, max_retries=3)

    async def send_func(wxid, text):
        return SendResult.ok("msg_001")

    result = _run(mgr.send_with_ack(send_func, "msg_001", "wxid_target", "hello"))
    assert result is True
    assert mgr.is_acked("msg_001") is True


def test_send_with_ack_auto_ack_failure():
    """auto_ack 模式下发送失败返回 False (重试后仍失败)"""
    mgr = AckManager(auto_ack=True, max_retries=2, timeout=0.1)

    call_count = [0]

    async def send_func(wxid, text):
        call_count[0] += 1
        return SendResult.fail("发送失败")

    result = _run(mgr.send_with_ack(send_func, "msg_fail", "wxid_target", "hello"))
    assert result is False
    # 应重试 max_retries + 1 = 3 次
    assert call_count[0] == 3
    assert mgr.is_acked("msg_fail") is False


def test_send_with_ack_auto_ack_exception():
    """auto_ack 模式下发送抛异常返回 False"""
    mgr = AckManager(auto_ack=True, max_retries=1, timeout=0.1)

    async def send_func(wxid, text):
        raise ConnectionError("网络异常")

    result = _run(mgr.send_with_ack(send_func, "msg_exc", "wxid_target", "hello"))
    assert result is False
    assert mgr.is_acked("msg_exc") is False


def test_send_with_ack_retry_then_success():
    """发送失败后重试成功"""
    mgr = AckManager(auto_ack=True, max_retries=3, timeout=0.1)

    call_count = [0]

    async def send_func(wxid, text):
        call_count[0] += 1
        if call_count[0] < 3:
            return SendResult.fail("暂时失败")
        return SendResult.ok("msg_retry_ok")

    result = _run(mgr.send_with_ack(send_func, "msg_retry", "wxid_target", "hello"))
    assert result is True
    assert call_count[0] == 3
    assert mgr.is_acked("msg_retry") is True


def test_send_with_ack_retry_exception_then_success():
    """发送异常后重试成功"""
    mgr = AckManager(auto_ack=True, max_retries=3, timeout=0.1)

    call_count = [0]

    async def send_func(wxid, text):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionError("首次异常")
        return SendResult.ok("msg_ok")

    result = _run(mgr.send_with_ack(send_func, "msg_retry_exc", "wxid_target", "hello"))
    assert result is True
    assert call_count[0] == 2


def test_send_with_ack_no_retries():
    """max_retries=0 不重试"""
    mgr = AckManager(auto_ack=True, max_retries=0, timeout=0.1)

    call_count = [0]

    async def send_func(wxid, text):
        call_count[0] += 1
        return SendResult.fail("失败")

    result = _run(mgr.send_with_ack(send_func, "msg_no_retry", "wxid_target", "hello"))
    assert result is False
    assert call_count[0] == 1  # 只调用一次, 不重试


# ============================================================================
# 测试: 手动 ACK 模式 (auto_ack=False)
# ============================================================================
def test_send_with_ack_manual_confirm():
    """auto_ack=False 时需手动 confirm 才算成功"""
    mgr = AckManager(auto_ack=False, max_retries=0, timeout=1.0)

    async def send_func(wxid, text):
        return SendResult.ok("msg_manual")

    async def _run_test():
        # 启动发送任务
        task = asyncio.create_task(
            mgr.send_with_ack(send_func, "msg_manual", "wxid_target", "hello")
        )
        # 等待一小段时间让发送完成, 然后手动确认
        await asyncio.sleep(0.1)
        await mgr.confirm("msg_manual")
        result = await task
        return result

    result = _run(_run_test())
    assert result is True
    assert mgr.is_acked("msg_manual") is True


def test_send_with_ack_manual_timeout():
    """auto_ack=False 超时未确认返回 False"""
    mgr = AckManager(auto_ack=False, max_retries=0, timeout=0.2)

    async def send_func(wxid, text):
        return SendResult.ok("msg_timeout")

    result = _run(mgr.send_with_ack(send_func, "msg_timeout", "wxid_target", "hello"))
    assert result is False
    assert mgr.is_acked("msg_timeout") is False


def test_send_with_ack_manual_timeout_then_retry():
    """auto_ack=False 超时后重试, 重试时确认成功"""
    mgr = AckManager(auto_ack=False, max_retries=2, timeout=0.2)

    call_count = [0]

    async def send_func(wxid, text):
        call_count[0] += 1
        return SendResult.ok(f"msg_{call_count[0]}")

    async def _run_test():
        task = asyncio.create_task(
            mgr.send_with_ack(send_func, "msg_rt", "wxid_target", "hello")
        )
        # 第一次发送后不确认, 等待超时重试
        # 在第二次发送后确认
        await asyncio.sleep(0.5)  # 等待第一次超时 + 重试
        await mgr.confirm("msg_rt")
        result = await task
        return result

    result = _run(_run_test())
    assert result is True
    # 应至少发送 2 次 (第一次超时, 第二次确认)
    assert call_count[0] >= 2


# ============================================================================
# 测试: confirm / wait_ack
# ============================================================================
def test_confirm_nonexistent_msg():
    """confirm 不存在的 msg_id 不抛异常"""
    mgr = AckManager()
    _run(mgr.confirm("nonexistent"))  # 不抛异常即通过


def test_wait_ack_nonexistent_msg():
    """wait_ack 不存在的 msg_id 返回 False"""
    mgr = AckManager()
    result = _run(mgr.wait_ack("nonexistent", 0.1))
    assert result is False


def test_wait_ack_already_set():
    """wait_ack 已确认的事件立即返回 True"""
    mgr = AckManager()
    # 手动创建事件并设置
    event = asyncio.Event()
    event.set()
    mgr._events["preset"] = event
    result = _run(mgr.wait_ack("preset", 0.1))
    assert result is True


def test_wait_ack_timeout():
    """wait_ack 超时返回 False"""
    mgr = AckManager()
    event = asyncio.Event()
    mgr._events["pending"] = event
    result = _run(mgr.wait_ack("pending", 0.1))
    assert result is False


def test_confirm_sets_event():
    """confirm 设置事件, 唤醒等待中的 wait_ack"""
    mgr = AckManager()

    async def _run_test():
        event = asyncio.Event()
        mgr._events["to_confirm"] = event

        # 启动等待任务
        wait_task = asyncio.create_task(mgr.wait_ack("to_confirm", 2.0))
        await asyncio.sleep(0.05)  # 确保等待已开始
        await mgr.confirm("to_confirm")
        result = await wait_task
        return result

    result = _run(_run_test())
    assert result is True


# ============================================================================
# 测试: 状态查询
# ============================================================================
def test_is_acked_default_false():
    """未确认的消息 is_acked 返回 False"""
    mgr = AckManager()
    assert mgr.is_acked("any_msg") is False


def test_is_acked_after_success():
    """发送成功后 is_acked 返回 True"""
    mgr = AckManager(auto_ack=True)

    async def send_func():
        return SendResult.ok("msg_ok")

    _run(mgr.send_with_ack(send_func, "msg_ok"))
    assert mgr.is_acked("msg_ok") is True


def test_is_acked_after_failure():
    """发送失败后 is_acked 返回 False"""
    mgr = AckManager(auto_ack=True, max_retries=0, timeout=0.1)

    async def send_func():
        return SendResult.fail("失败")

    _run(mgr.send_with_ack(send_func, "msg_bad"))
    assert mgr.is_acked("msg_bad") is False


def test_pending_count():
    """pending_count 返回等待 ACK 的消息数"""
    mgr = AckManager(auto_ack=False, timeout=5.0)
    # 初始为 0
    assert mgr.pending_count() == 0

    # 创建未完成的事件
    event = asyncio.Event()
    mgr._events["pending1"] = event
    assert mgr.pending_count() == 1

    event2 = asyncio.Event()
    mgr._events["pending2"] = event2
    assert mgr.pending_count() == 2


def test_pending_count_after_completion():
    """完成后 pending_count 减少"""
    mgr = AckManager(auto_ack=True)

    async def send_func():
        return SendResult.ok("msg_done")

    _run(mgr.send_with_ack(send_func, "msg_done"))
    # 完成后 pending 应为 0
    assert mgr.pending_count() == 0


# ============================================================================
# 测试: generate_msg_id
# ============================================================================
def test_generate_msg_id_default_prefix():
    """generate_msg_id 默认前缀 msg_"""
    mid = AckManager.generate_msg_id()
    assert mid.startswith("msg_")
    assert len(mid) > len("msg_")


def test_generate_msg_id_custom_prefix():
    """generate_msg_id 支持自定义前缀"""
    mid = AckManager.generate_msg_id("text")
    assert mid.startswith("text_")


def test_generate_msg_id_unique():
    """generate_msg_id 生成唯一 ID"""
    ids = {AckManager.generate_msg_id() for _ in range(100)}
    assert len(ids) == 100, "生成的消息 ID 应唯一"


# ============================================================================
# 测试: _is_success 判断
# ============================================================================
def test_is_success_send_result_ok():
    """SendResult.ok 视为成功"""
    assert AckManager._is_success(SendResult.ok("msg")) is True


def test_is_success_send_result_fail():
    """SendResult.fail 视为失败"""
    assert AckManager._is_success(SendResult.fail("err")) is False


def test_is_success_bool_true():
    """True 视为成功"""
    assert AckManager._is_success(True) is True


def test_is_success_bool_false():
    """False 视为失败"""
    assert AckManager._is_success(False) is False


def test_is_success_none():
    """None 视为失败"""
    assert AckManager._is_success(None) is False


def test_is_success_dict_success_key():
    """dict 含 success=True 视为成功"""
    assert AckManager._is_success({"success": True}) is True
    assert AckManager._is_success({"success": False}) is False


def test_is_success_dict_code_zero():
    """dict code=0 视为成功"""
    assert AckManager._is_success({"code": 0}) is True
    assert AckManager._is_success({"code": 1}) is False


def test_is_success_truthy_object():
    """其他 truthy 对象视为成功"""
    assert AckManager._is_success("non-empty") is True
    assert AckManager._is_success("") is False
    assert AckManager._is_success(42) is True
    assert AckManager._is_success(0) is False


# ============================================================================
# 测试: 多消息并发
# ============================================================================
def test_multiple_messages_independent():
    """多条消息的 ACK 状态相互独立"""
    mgr = AckManager(auto_ack=True, max_retries=0, timeout=0.1)

    async def send_ok():
        return SendResult.ok("ok")

    async def send_fail():
        return SendResult.fail("fail")

    r1 = _run(mgr.send_with_ack(send_ok, "msg_ok"))
    r2 = _run(mgr.send_with_ack(send_fail, "msg_fail"))

    assert r1 is True
    assert r2 is False
    assert mgr.is_acked("msg_ok") is True
    assert mgr.is_acked("msg_fail") is False
