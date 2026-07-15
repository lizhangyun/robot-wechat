"""
网络模块单元测试

测试范围:
  - network/message_queue.py : 异步消息队列 (发布/订阅, ACK, 死信队列)
  - network/http_client.py   : HTTP 客户端初始化 (不实际请求)
  - network/updater.py       : 版本号比较

所有测试不依赖外部网络服务。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from network.message_queue import MessageQueue, Message
from network.http_client import HttpClient, HttpRequestError
from network.updater import AutoUpdater, UpdateInfo


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


# ============================================================================
# 测试: 消息队列发布订阅
# ============================================================================
def test_message_queue_pubsub():
    """测试发布订阅"""
    async def _run_test():
        mq = MessageQueue(max_retry=3)
        await mq.start()

        received = []

        async def callback(msg: Message) -> bool:
            received.append(msg)
            return True  # ack

        # 订阅
        tag = mq.subscribe("test_topic", callback)
        assert mq.subscriber_count("test_topic") == 1

        # 发布消息
        msg_id = await mq.publish("test_topic", {"text": "hello"})
        assert msg_id is not None

        # 等待消费
        await asyncio.sleep(0.3)

        assert len(received) == 1, f"应收到1条消息, 实际 {len(received)}"
        assert received[0].payload == {"text": "hello"}
        assert received[0].topic == "test_topic"

        # 取消订阅
        mq.unsubscribe("test_topic", tag)
        assert mq.subscriber_count("test_topic") == 0

        await mq.stop()

    _run(_run_test())


def test_message_queue_multi_subscriber():
    """测试多订阅者广播"""
    async def _run_test():
        mq = MessageQueue(max_retry=3)
        await mq.start()

        received_a = []
        received_b = []

        async def callback_a(msg: Message) -> bool:
            received_a.append(msg.payload)
            return True

        async def callback_b(msg: Message) -> bool:
            received_b.append(msg.payload)
            return True

        mq.subscribe("broadcast", callback_a)
        mq.subscribe("broadcast", callback_b)
        assert mq.subscriber_count("broadcast") == 2

        await mq.publish("broadcast", "消息内容")
        await asyncio.sleep(0.3)

        # 两个订阅者都应收到
        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0] == "消息内容"
        assert received_b[0] == "消息内容"

        await mq.stop()

    _run(_run_test())


def test_message_queue_no_subscriber():
    """测试无订阅者时发布 (仅缓存)"""
    async def _run_test():
        mq = MessageQueue(max_retry=3)
        await mq.start()

        # 无订阅者发布
        msg_id = await mq.publish("empty_topic", {"data": "test"})
        assert msg_id is not None

        # recent 中应有记录
        recent = mq.recent("empty_topic")
        assert len(recent) == 1
        assert recent[0].payload == {"data": "test"}

        await mq.stop()

    _run(_run_test())


# ============================================================================
# 测试: 消息确认 (ACK)
# ============================================================================
def test_message_queue_ack():
    """测试消息确认 (ACK)"""
    async def _run_test():
        mq = MessageQueue(max_retry=3)
        await mq.start()

        processed = []

        async def callback(msg: Message) -> bool:
            processed.append(msg.payload)
            return True  # ack 成功

        mq.subscribe("ack_topic", callback)
        await mq.publish("ack_topic", "ack消息")
        await asyncio.sleep(0.3)

        # ack 后消息只处理一次
        assert len(processed) == 1
        assert processed[0] == "ack消息"

        # 等待确认没有重试
        await asyncio.sleep(0.5)
        assert len(processed) == 1, "ack后不应重试"

        await mq.stop()

    _run(_run_test())


def test_message_queue_nack_retry():
    """测试消息否认 (NACK) 后重试"""
    async def _run_test():
        mq = MessageQueue(max_retry=3)
        await mq.start()

        attempt_count = 0

        async def callback(msg: Message) -> bool:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                return False  # nack, 触发重试
            return True  # 第3次成功

        mq.subscribe("nack_topic", callback)
        await mq.publish("nack_topic", "重试消息")

        # 等待足够时间让重试完成
        await asyncio.sleep(2.0)

        # 应重试到成功
        assert attempt_count == 3, f"应重试3次, 实际 {attempt_count} 次"

        await mq.stop()

    _run(_run_test())


# ============================================================================
# 测试: 死信队列
# ============================================================================
def test_message_queue_dead_letter():
    """测试死信队列 (超过最大重试次数)"""
    async def _run_test():
        mq = MessageQueue(max_retry=2, dead_letter_capacity=100)
        await mq.start()

        attempt_count = 0

        async def callback(msg: Message) -> bool:
            nonlocal attempt_count
            attempt_count += 1
            return False  # 始终失败

        mq.subscribe("dead_topic", callback)
        await mq.publish("dead_topic", "死信消息")

        # 等待足够时间让重试耗尽
        await asyncio.sleep(3.0)

        # 应重试 max_retry + 1 次 (初始 + 2次重试)
        # retry_count 从 0 开始, >max_retry 时进入死信
        # 第1次: retry_count=0, nack, retry_count->1
        # 第2次: retry_count=1, nack, retry_count->2
        # 第3次: retry_count=2, nack, retry_count->3 > 2, 进入死信
        assert attempt_count == 3, f"应尝试3次, 实际 {attempt_count} 次"

        # 死信队列应有 1 条消息
        assert mq.dead_letter_count() == 1, \
            f"死信队列应有1条消息, 实际 {mq.dead_letter_count()}"

        # 取出死信消息
        dead_letters = await mq.drain_dead_letter()
        assert len(dead_letters) == 1
        assert dead_letters[0].payload == "死信消息"

        await mq.stop()

    _run(_run_test())


def test_message_queue_recent():
    """测试最近消息缓存"""
    async def _run_test():
        mq = MessageQueue(max_retry=3)
        await mq.start()

        async def callback(msg: Message) -> bool:
            return True

        mq.subscribe("recent_topic", callback)

        # 发布多条消息
        for i in range(5):
            await mq.publish("recent_topic", f"msg_{i}")

        await asyncio.sleep(0.5)

        # 查询最近消息
        recent = mq.recent("recent_topic", limit=3)
        assert len(recent) == 3, f"应返回3条最近消息, 实际 {len(recent)}"

        # 查询全部
        all_recent = mq.recent("recent_topic", limit=50)
        assert len(all_recent) == 5

        await mq.stop()

    _run(_run_test())


def test_message_queue_start_stop():
    """测试消息队列启动停止"""
    async def _run_test():
        mq = MessageQueue(max_retry=3)
        await mq.start()
        assert mq._running is True

        await mq.stop()
        assert mq._running is False
        assert len(mq._tasks) == 0

    _run(_run_test())


# ============================================================================
# 测试: HTTP 客户端初始化
# ============================================================================
def test_http_client_init():
    """测试 HTTP 客户端初始化 (不实际请求)"""
    client = HttpClient(
        base_url="https://api.example.com",
        timeout=15.0,
        max_retries=3,
        retry_backoff=0.5,
    )

    # 验证配置
    assert client.base_url == "https://api.example.com"
    assert client.timeout == 15.0
    assert client.max_retries == 3
    assert client.retry_backoff == 0.5
    assert client._client is None  # 尚未 open


def test_http_client_open_close():
    """测试 HTTP 客户端打开和关闭"""
    async def _run_test():
        client = HttpClient(base_url="https://api.example.com", timeout=10.0)

        # 打开
        await client.open()
        assert client._client is not None

        # 重复打开 (应无副作用)
        await client.open()
        assert client._client is not None

        # 关闭
        await client.close()
        assert client._client is None

    _run(_run_test())


def test_http_client_context_manager():
    """测试 HTTP 客户端上下文管理器"""
    async def _run_test():
        async with HttpClient(timeout=5.0) as client:
            assert client._client is not None
        # 退出后应关闭
        assert client._client is None

    _run(_run_test())


def test_http_client_defaults():
    """测试 HTTP 客户端默认值"""
    client = HttpClient()
    assert client.base_url == ""
    assert client.timeout == 30.0
    assert client.max_retries == 3
    assert (408, 429, 500, 502, 503, 504) == client.retry_statuses


def test_http_client_set_header():
    """测试设置请求头"""
    client = HttpClient()
    client.set_header("Authorization", "Bearer token123")
    assert client.default_headers["Authorization"] == "Bearer token123"


def test_http_client_set_cookie():
    """测试设置 Cookie"""
    client = HttpClient()
    client.set_cookie("session", "abc123", domain="example.com")
    # cookies 对象应有该值
    assert "session" in client.cookies


def test_http_client_base_url_strip():
    """测试 base_url 去除尾部斜杠"""
    client = HttpClient(base_url="https://api.example.com/")
    assert client.base_url == "https://api.example.com"

    client2 = HttpClient(base_url="https://api.example.com")
    assert client2.base_url == "https://api.example.com"


# ============================================================================
# 测试: 版本号比较
# ============================================================================
def test_updater_version_compare():
    """测试版本号比较"""
    cmp = AutoUpdater.compare_versions

    # 相等
    assert cmp("1.0.0", "1.0.0") == 0
    assert cmp("2.5.3", "2.5.3") == 0

    # 大于
    assert cmp("1.0.1", "1.0.0") == 1
    assert cmp("2.0.0", "1.9.9") == 1
    assert cmp("1.10.0", "1.9.0") == 1
    assert cmp("3.0.0", "2.99.99") == 1

    # 小于
    assert cmp("1.0.0", "1.0.1") == -1
    assert cmp("1.9.9", "2.0.0") == -1
    assert cmp("1.9.0", "1.10.0") == -1

    # 带前缀 v
    assert cmp("v1.0.0", "1.0.0") == 0
    assert cmp("v2.0.0", "v1.0.0") == 1
    assert cmp("V1.0.0", "v1.0.0") == 0

    # 不同长度
    assert cmp("1.0", "1.0.0") == 0  # 1.0 补齐为 1.0.0
    assert cmp("1.0.0.1", "1.0.0") == 1
    assert cmp("1.0", "1.0.1") == -1

    # 空值
    assert cmp("", "") == 0
    assert cmp("1.0.0", "") == 1


def test_updater_parse_repo():
    """测试仓库标识解析"""
    # GitHub owner/repo
    updater = AutoUpdater(repo="owner/repo", current_version="1.0.0")
    platform, owner_repo = updater._parse_repo()
    assert platform == "github"
    assert owner_repo == "owner/repo"

    # GitHub 完整 URL
    updater2 = AutoUpdater(repo="https://github.com/owner/repo")
    platform2, owner_repo2 = updater2._parse_repo()
    assert platform2 == "github"
    assert owner_repo2 == "owner/repo"

    # Gitee 完整 URL
    updater3 = AutoUpdater(repo="https://gitee.com/owner/repo")
    platform3, owner_repo3 = updater3._parse_repo()
    assert platform3 == "gitee"
    assert owner_repo3 == "owner/repo"

    # 带 .git 后缀
    updater4 = AutoUpdater(repo="https://github.com/owner/repo.git")
    platform4, owner_repo4 = updater4._parse_repo()
    assert platform4 == "github"
    assert owner_repo4 == "owner/repo"


def test_update_info():
    """测试更新信息解析"""
    data = {
        "tag_name": "v1.2.3",
        "name": "Release 1.2.3",
        "body": "修复若干bug",
        "published_at": "2025-01-01T00:00:00Z",
        "html_url": "https://github.com/owner/repo/releases/tag/v1.2.3",
        "assets": [
            {
                "browser_download_url": "https://github.com/owner/repo/releases/download/v1.2.3/update.zip",
                "size": 1024000,
            }
        ],
    }
    info = UpdateInfo(data)
    assert info.version == "1.2.3"  # 去除 v 前缀
    assert info.name == "Release 1.2.3"
    assert info.body == "修复若干bug"
    assert info.download_url == "https://github.com/owner/repo/releases/download/v1.2.3/update.zip"
    assert info.download_size == 1024000

    # to_dict
    d = info.to_dict()
    assert d["version"] == "1.2.3"
    assert d["download_url"] is not None


def test_update_info_no_assets():
    """测试无资源的更新信息"""
    info = UpdateInfo({"tag_name": "v0.1.0", "name": "Initial"})
    assert info.version == "0.1.0"
    assert info.download_url is None
    assert info.download_size == 0
