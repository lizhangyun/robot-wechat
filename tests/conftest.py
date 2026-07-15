"""
共享测试夹具 - 提供干净的应用实例和临时数据库

所有测试通过 app_client 夹具获取一个基于 mock 模式的 FastAPI TestClient,
每个测试使用独立的临时 SQLite 数据库, 互不干扰。
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict, deque
from pathlib import Path

# 确保项目根目录在 sys.path 中 (支持直接 pytest 运行)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

# 导入全局单例 (测试中需要重置其状态以保证隔离)
from config.settings import settings
from core.engine import engine
from core.websocket_manager import ws_manager
from database import Database
from database.manager import db_manager
from network.message_queue import message_queue
from security.firewall import ip_firewall
from security.license import license_manager
from wechat.hook_interface import APICommand
from wechat.message_types import MessageData, MessageType, SendResult


def _reset_singletons() -> None:
    """重置所有全局单例的内部状态, 确保测试之间完全隔离。

    涵盖: 数据库连接、引擎实例表、WebSocket 连接池、消息队列订阅/缓存、
          防火墙缓存、许可证缓存。
    """
    # ---- 数据库 ----
    db_manager._db = None  # 关闭已有连接, 下次 init() 会重新打开

    # ---- 核心引擎 ----
    engine._instances = {}        # 清空内存中的实例状态
    engine._started = False       # 允许重新 start()
    engine._sub_tags = []         # 清空消息队列订阅标签

    # ---- WebSocket 管理器 ----
    ws_manager._connections = {}  # 清空所有连接

    # ---- 消息队列 ----
    message_queue._subscribers = defaultdict(dict)
    message_queue._inflight = defaultdict(deque)
    message_queue._recent = defaultdict(lambda: deque(maxlen=500))
    # 重建死信队列 (新队列绑定到新的事件循环)
    message_queue._dead_letter = asyncio.Queue(maxsize=1000)
    message_queue._running = False
    message_queue._tasks = []
    message_queue._consumer_counter = 0

    # ---- IP 防火墙 ----
    ip_firewall._loaded = False
    ip_firewall._black_ips = []
    ip_firewall._white_ips = []

    # ---- 许可证 ----
    license_manager._status = {"valid": False, "reason": "未验证", "last_check": 0}
    license_manager._cache = {}


# ---------------------------------------------------------------------------
# 核心夹具
# ---------------------------------------------------------------------------

@pytest.fixture
def app_client(tmp_path):
    """创建一个干净的 mock 模式应用实例。

    - 使用 pytest 的 tmp_path 生成临时数据库文件, 测试结束后自动清理
    - 每次调用前重置所有全局单例, 保证测试隔离
    - 通过 TestClient 的上下文管理器自动触发 lifespan (引擎启动/停止)

    Yields:
        TestClient: 已启动的 FastAPI 测试客户端
    """
    from api.server import create_app

    # 将数据库和许可证缓存指向临时目录, 避免污染真实数据
    db_manager.db_path = tmp_path / "test.db"
    license_manager._cache_path = tmp_path / "license_cache.json"
    license_manager.vef_path = tmp_path / "run.vef"

    # 重置全局单例状态
    _reset_singletons()

    # 创建 mock 模式应用
    app = create_app(mock=True)

    # TestClient 作为上下文管理器: 进入时执行 lifespan 启动, 退出时执行关闭
    with TestClient(app) as client:
        yield client

    # 测试结束后再次重置, 防止残留状态影响后续测试
    _reset_singletons()


@pytest.fixture
def create_instance(app_client):
    """返回一个辅助函数, 通过 API 创建测试实例。

    用法:
        def test_xxx(app_client, create_instance):
            create_instance("my-id", "显示名", "wxid_xxx")
            ...

    Returns:
        callable: create_instance(instance_id, display_name, wxid) -> dict
    """
    def _create(
        instance_id: str = "test-instance",
        display_name: str = "测试实例",
        wxid: str = "wxid_test",
    ) -> dict:
        resp = app_client.post("/api/instance/create", json={
            "instance_id": instance_id,
            "display_name": display_name,
            "wxid": wxid,
        })
        assert resp.status_code == 200, f"创建实例失败: {resp.text}"
        assert resp.json()["success"] is True
        return resp.json()

    return _create


# =========================================================================== #
#  业务模块测试夹具（记账 / 自动回复 / 群管理 / 定时任务）
#
#  以下夹具不依赖真实微信：MockWeChatClient 记录所有调用便于断言，
#  db 为每个测试函数独立的内存数据库，make_message 为消息构造工厂。
# =========================================================================== #
class MockWeChatClient:
    """模拟微信客户端（不连接真实微信）。

    实现 :class:`WeChatHookInterface` 所需方法：
    - ``send_text`` / ``send_image`` / ``send_file`` 记录到对应列表并返回成功结果；
    - ``api`` 统一返回 ``{"code": 0, ...}``（可被测试覆写以模拟失败）；
    - ``get_group_members`` 返回可配置的成员列表。

    所有调用均记录在 ``api_calls`` / ``sent_texts`` 等列表中，供断言使用。
    """

    def __init__(self) -> None:
        self.instance_id: str = "test_instance"
        # 记录各类调用
        self.sent_texts: list[tuple[str, str]] = []
        self.sent_images: list[tuple[str, str]] = []
        self.sent_files: list[tuple[str, str]] = []
        self.api_calls: list[tuple[object, dict]] = []
        # api 默认返回成功；测试可覆写为失败字典以模拟异常
        self.api_result: dict | None = None
        # 群成员映射：group_wxid -> 成员列表
        self.group_members_map: dict[str, list[dict]] = {}
        self.login_info: dict = {
            "wxid": "wxid_self_000",
            "nickname": "机器人本体",
            "alias": "robot_self",
        }
        self._callback = None

    # --- 生命周期 ---
    async def init(self, instance_id: str) -> bool:
        self.instance_id = instance_id
        return True

    async def load_window(self) -> bool:
        return True

    async def uninstall(self) -> bool:
        return True

    # --- 消息发送 ---
    async def send_text(self, wxid: str, text: str) -> SendResult:
        self.sent_texts.append((wxid, text))
        return SendResult.ok(f"mock_text_{len(self.sent_texts)}")

    async def send_image(self, wxid: str, path: str) -> SendResult:
        self.sent_images.append((wxid, path))
        return SendResult.ok(f"mock_img_{len(self.sent_images)}")

    async def send_file(self, wxid: str, path: str) -> SendResult:
        self.sent_files.append((wxid, path))
        return SendResult.ok(f"mock_file_{len(self.sent_files)}")

    # --- 核心 API ---
    async def api(self, command: object, params: dict) -> dict:
        self.api_calls.append((command, dict(params)))
        if self.api_result is not None:
            return dict(self.api_result)
        # 统一返回成功结构，便于撤回/公告/@所有人判断通过
        return {"code": 0, "msg": "ok", "data": None}

    # --- 查询 ---
    async def get_contacts(self) -> list[dict]:
        return [
            {"wxid": "wxid_test001", "nickname": "张三", "remark": ""},
            {"wxid": "wxid_test002", "nickname": "李四", "remark": ""},
        ]

    async def get_groups(self) -> list[dict]:
        return [
            {"group_wxid": "12345678901@chatroom", "group_name": "测试群A", "member_count": 6},
        ]

    async def get_group_members(self, group_wxid: str) -> list[dict]:
        if group_wxid in self.group_members_map:
            return self.group_members_map[group_wxid]
        # 默认返回 3 个成员
        return [
            {"wxid": "wxid_self_000", "nickname": "机器人本体", "display_name": "小助手"},
            {"wxid": "wxid_test001", "nickname": "张三", "display_name": "张三"},
            {"wxid": "wxid_test002", "nickname": "李四", "display_name": "李四"},
        ]

    async def get_login_info(self) -> dict:
        return dict(self.login_info)

    # --- 回调 ---
    def set_message_callback(self, callback) -> None:
        self._callback = callback


@pytest_asyncio.fixture
async def db():
    """每个测试函数独立的内存数据库，测试结束自动关闭。

    SQLite ``:memory:`` 经 SQLAlchemy StaticPool 在跨会话间共享同一内存库，
    故同一测试内多次 ``db.session()`` 数据互通。
    """
    database = Database(":memory:")
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def mock_client():
    """模拟微信客户端。"""
    return MockWeChatClient()


@pytest.fixture
def make_message():
    """构造 MessageData 的便捷工厂。

    返回一个闭包，调用时传入参数即可生成群/私聊文本消息；
    群消息自动补 ``wxid:\\n正文`` 前缀（content_body 会剥离）。
    """

    def _make(
        content: str,
        *,
        msg_id: str = "msg_1",
        sender_wxid: str = "wxid_test001",
        receiver_wxid: str = "wxid_self_000",
        is_group: bool = True,
        group_wxid: str | None = "12345678901@chatroom",
        msg_type: MessageType = MessageType.TEXT,
    ) -> MessageData:
        if is_group and ":\n" not in content:
            content = f"{sender_wxid}:\n{content}"
        return MessageData(
            msg_id=msg_id,
            sender_wxid=sender_wxid,
            receiver_wxid=receiver_wxid,
            content=content,
            msg_type=msg_type,
            is_group=is_group,
            group_wxid=group_wxid,
        )

    return _make
