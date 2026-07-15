"""
消息分片器单元测试

测试范围:
  - modules/message_splitter.py : 消息分片发送 (对应原软件 msg_split 功能)

测试内容:
  - MessageSplitter 初始化与参数校验
  - split() 按行数分片 (70 行分界)
  - 短消息不分片
  - needs_split() 判断是否需要分片
  - send_split() 异步分片发送 (使用 Mock 客户端)
  - send() 智能发送 (需分片则分片, 否则直接发送)
  - split_sync() 同步分片接口

对应原软件 config.ini 中的 [msg 消息最多行数] / [msg_split] 配置。
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

from modules.message_splitter import MessageSplitter
from wechat.message_types import SendResult


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


def _make_mock_client(success: bool = True, msg_id: str = "mock_msg"):
    """构造 Mock 微信客户端, send_text 返回指定结果"""
    client = Mock()
    if success:
        client.send_text = AsyncMock(return_value=SendResult.ok(msg_id))
    else:
        client.send_text = AsyncMock(return_value=SendResult.fail("发送失败"))
    return client


# ============================================================================
# 测试: 初始化
# ============================================================================
def test_init_default():
    """默认初始化: max_lines=70, sleep_sec=1.0"""
    splitter = MessageSplitter()
    assert splitter.max_lines == 70
    assert splitter.sleep_sec == 1.0


def test_init_custom_params():
    """自定义参数初始化"""
    splitter = MessageSplitter(max_lines=10, sleep_sec=0.5)
    assert splitter.max_lines == 10
    assert splitter.sleep_sec == 0.5


def test_init_invalid_max_lines():
    """max_lines < 1 抛 ValueError"""
    with pytest.raises(ValueError):
        MessageSplitter(max_lines=0)
    with pytest.raises(ValueError):
        MessageSplitter(max_lines=-5)


def test_init_invalid_sleep_sec():
    """sleep_sec < 0 抛 ValueError"""
    with pytest.raises(ValueError):
        MessageSplitter(sleep_sec=-1.0)


def test_init_zero_sleep():
    """sleep_sec=0 允许 (无间隔)"""
    splitter = MessageSplitter(sleep_sec=0)
    assert splitter.sleep_sec == 0


# ============================================================================
# 测试: split 分片
# ============================================================================
def test_split_empty_text():
    """空文本返回 [""]"""
    splitter = MessageSplitter(max_lines=70)
    result = _run(splitter.split(""))
    assert result == [""]


def test_split_short_text_no_split():
    """短消息 (行数 <= max_lines) 不分片"""
    splitter = MessageSplitter(max_lines=70)
    text = "第一行\n第二行\n第三行"
    result = _run(splitter.split(text))
    assert len(result) == 1
    assert result[0] == text


def test_split_exact_boundary():
    """恰好 max_lines 行不分片"""
    splitter = MessageSplitter(max_lines=5)
    text = "\n".join(f"行{i}" for i in range(5))
    result = _run(splitter.split(text))
    assert len(result) == 1


def test_split_over_boundary():
    """超过 max_lines 行分片"""
    splitter = MessageSplitter(max_lines=5)
    text = "\n".join(f"行{i}" for i in range(12))
    result = _run(splitter.split(text))
    # 12 行 / 5 = 3 片 (5+5+2)
    assert len(result) == 3
    assert result[0].count("\n") == 4  # 第一片 5 行
    assert result[1].count("\n") == 4  # 第二片 5 行
    assert result[2].count("\n") == 1  # 第三片 2 行


def test_split_default_70_lines():
    """默认 70 行分界"""
    splitter = MessageSplitter()  # 默认 max_lines=70
    # 70 行不分片
    text_70 = "\n".join(f"行{i}" for i in range(70))
    assert len(_run(splitter.split(text_70))) == 1
    # 71 行分片
    text_71 = "\n".join(f"行{i}" for i in range(71))
    result = _run(splitter.split(text_71))
    assert len(result) == 2
    assert result[0].count("\n") == 69  # 第一片 70 行
    assert result[1].count("\n") == 0   # 第二片 1 行


def test_split_single_line():
    """单行文本不分片"""
    splitter = MessageSplitter(max_lines=70)
    result = _run(splitter.split("单行文本"))
    assert len(result) == 1
    assert result[0] == "单行文本"


def test_split_preserves_content():
    """分片后内容完整拼接等于原文"""
    splitter = MessageSplitter(max_lines=3)
    text = "\n".join(f"行{i}" for i in range(10))
    result = _run(splitter.split(text))
    # 重新拼接应等于原文
    assert "\n".join(result) == text


def test_split_max_lines_1():
    """max_lines=1 时每行一片"""
    splitter = MessageSplitter(max_lines=1)
    text = "a\nb\nc"
    result = _run(splitter.split(text))
    assert result == ["a", "b", "c"]


# ============================================================================
# 测试: split_sync 同步分片
# ============================================================================
def test_split_sync_short():
    """split_sync 短消息不分片"""
    splitter = MessageSplitter(max_lines=70)
    result = splitter.split_sync("短消息")
    assert result == ["短消息"]


def test_split_sync_long():
    """split_sync 长消息分片"""
    splitter = MessageSplitter(max_lines=3)
    text = "\n".join(f"行{i}" for i in range(7))
    result = splitter.split_sync(text)
    assert len(result) == 3  # 3+3+1


def test_split_sync_empty():
    """split_sync 空文本返回 [""]"""
    splitter = MessageSplitter()
    assert splitter.split_sync("") == [""]


# ============================================================================
# 测试: needs_split 判断
# ============================================================================
def test_needs_split_short():
    """短消息不需要分片"""
    splitter = MessageSplitter(max_lines=70)
    assert splitter.needs_split("短消息") is False
    assert splitter.needs_split("a\nb\nc") is False


def test_needs_split_long():
    """长消息需要分片"""
    splitter = MessageSplitter(max_lines=5)
    text = "\n".join(f"行{i}" for i in range(10))
    assert splitter.needs_split(text) is True


def test_needs_split_exact_boundary():
    """恰好 max_lines 行不需要分片"""
    splitter = MessageSplitter(max_lines=5)
    text = "\n".join(f"行{i}" for i in range(5))
    assert splitter.needs_split(text) is False


def test_needs_split_one_over_boundary():
    """超过一行就需要分片"""
    splitter = MessageSplitter(max_lines=5)
    text = "\n".join(f"行{i}" for i in range(6))
    assert splitter.needs_split(text) is True


def test_needs_split_empty():
    """空文本不需要分片"""
    splitter = MessageSplitter(max_lines=5)
    assert splitter.needs_split("") is False


def test_needs_split_default_70():
    """默认 70 行分界"""
    splitter = MessageSplitter()
    text_70 = "\n".join(f"行{i}" for i in range(70))
    assert splitter.needs_split(text_70) is False
    text_71 = "\n".join(f"行{i}" for i in range(71))
    assert splitter.needs_split(text_71) is True


# ============================================================================
# 测试: send_split 异步分片发送
# ============================================================================
def test_send_split_single_chunk():
    """send_split 单片发送"""
    splitter = MessageSplitter(max_lines=70, sleep_sec=0)
    client = _make_mock_client(success=True, msg_id="msg_1")
    msg_ids = _run(splitter.send_split(client, "wxid_target", "短消息"))
    assert len(msg_ids) == 1
    assert msg_ids[0] == "msg_1"
    client.send_text.assert_awaited_once()


def test_send_split_multiple_chunks():
    """send_split 多片发送"""
    splitter = MessageSplitter(max_lines=3, sleep_sec=0)
    client = _make_mock_client(success=True, msg_id="msg_x")
    text = "\n".join(f"行{i}" for i in range(7))  # 3 片
    msg_ids = _run(splitter.send_split(client, "wxid_target", text))
    assert len(msg_ids) == 3
    assert client.send_text.await_count == 3


def test_send_split_with_sleep():
    """send_split 片间间隔 sleep_sec 秒"""
    splitter = MessageSplitter(max_lines=2, sleep_sec=0.1)
    client = _make_mock_client(success=True)
    text = "\n".join(f"行{i}" for i in range(5))  # 3 片
    import time
    start = time.monotonic()
    _run(splitter.send_split(client, "wxid_target", text))
    elapsed = time.monotonic() - start
    # 3 片, 第一片不等待, 后两片各等 0.1s -> 至少 0.2s
    assert elapsed >= 0.2, f"片间等待不足: {elapsed}s"


def test_send_split_failure_returns_empty_id():
    """send_split 发送失败时对应片 ID 为空字符串"""
    splitter = MessageSplitter(max_lines=3, sleep_sec=0)
    client = _make_mock_client(success=False)
    text = "\n".join(f"行{i}" for i in range(7))
    msg_ids = _run(splitter.send_split(client, "wxid_target", text))
    assert len(msg_ids) == 3
    # 失败的 ID 应为空字符串
    assert all(mid == "" for mid in msg_ids)


def test_send_split_exception_handled():
    """send_split 客户端抛异常时该片 ID 为空, 不中断后续发送"""
    splitter = MessageSplitter(max_lines=2, sleep_sec=0)
    client = Mock()
    call_count = [0]

    async def mock_send(wxid, text):
        call_count[0] += 1
        if call_count[0] == 2:
            raise ConnectionError("网络异常")
        return SendResult.ok(f"msg_{call_count[0]}")

    client.send_text = mock_send
    text = "\n".join(f"行{i}" for i in range(6))  # 3 片
    msg_ids = _run(splitter.send_split(client, "wxid_target", text))
    assert len(msg_ids) == 3
    assert msg_ids[0] == "msg_1"
    assert msg_ids[1] == ""  # 异常片
    assert msg_ids[2] == "msg_3"


def test_send_split_passes_correct_args():
    """send_split 传递正确的 wxid 和分片内容给客户端"""
    splitter = MessageSplitter(max_lines=70, sleep_sec=0)
    client = _make_mock_client(success=True)
    _run(splitter.send_split(client, "wxid_abc", "内容"))
    client.send_text.assert_awaited_once_with("wxid_abc", "内容")


# ============================================================================
# 测试: send 智能发送
# ============================================================================
def test_send_no_split_needed():
    """send 短消息直接发送 (不分片)"""
    splitter = MessageSplitter(max_lines=70, sleep_sec=0)
    client = _make_mock_client(success=True, msg_id="direct_msg")
    msg_ids = _run(splitter.send(client, "wxid_target", "短消息"))
    assert len(msg_ids) == 1
    assert msg_ids[0] == "direct_msg"
    client.send_text.assert_awaited_once()


def test_send_split_needed():
    """send 长消息自动分片发送"""
    splitter = MessageSplitter(max_lines=3, sleep_sec=0)
    client = _make_mock_client(success=True, msg_id="chunk_msg")
    text = "\n".join(f"行{i}" for i in range(7))  # 需分片
    msg_ids = _run(splitter.send(client, "wxid_target", text))
    assert len(msg_ids) == 3
    assert client.send_text.await_count == 3


def test_send_force_split():
    """send force_split=True 时即使短消息也走分片流程"""
    splitter = MessageSplitter(max_lines=70, sleep_sec=0)
    client = _make_mock_client(success=True, msg_id="forced_msg")
    msg_ids = _run(splitter.send(client, "wxid_target", "短消息", force_split=True))
    assert len(msg_ids) == 1
    # force_split 走 send_split 路径
    assert msg_ids[0] == "forced_msg"


def test_send_empty_text():
    """send 空文本"""
    splitter = MessageSplitter(max_lines=70, sleep_sec=0)
    client = _make_mock_client(success=True, msg_id="empty_msg")
    msg_ids = _run(splitter.send(client, "wxid_target", ""))
    assert len(msg_ids) == 1
