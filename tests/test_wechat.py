"""
微信接口单元测试

测试范围:
  - wechat/message_types.py  : 消息类型枚举和模型
  - wechat/hook_interface.py : Hook 接口定义完整性、API 命令常量
  - wechat/wechat_client.py  : Mock 客户端 (模拟模式)

所有测试在 Mock 模式下进行, 不依赖真实微信。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from wechat.message_types import MessageType, MessageData, SendResult
from wechat.hook_interface import APICommand, WeChatHookInterface, MessageCallback
from wechat.wechat_client import WeChatClient, create_client, _MockDataGenerator


# ============================================================================
# 辅助函数
# ============================================================================
def _run(coro):
    """在同步测试中运行异步协程"""
    return asyncio.run(coro)


# ============================================================================
# 测试: 消息类型枚举和模型
# ============================================================================
def test_message_types():
    """测试消息类型枚举和模型"""
    # 枚举值验证
    assert MessageType.TEXT.value == "text"
    assert MessageType.IMAGE.value == "image"
    assert MessageType.FILE.value == "file"
    assert MessageType.VIDEO.value == "video"
    assert MessageType.VOICE.value == "voice"
    assert MessageType.CARD.value == "card"
    assert MessageType.LINK.value == "link"
    assert MessageType.SYSTEM.value == "system"
    assert MessageType.EMOJI.value == "emoji"
    assert MessageType.LOCATION.value == "location"

    # from_code 代码映射
    assert MessageType.from_code(1) == MessageType.TEXT
    assert MessageType.from_code(3) == MessageType.IMAGE
    assert MessageType.from_code(34) == MessageType.VOICE
    assert MessageType.from_code(42) == MessageType.CARD
    assert MessageType.from_code(43) == MessageType.VIDEO
    assert MessageType.from_code(48) == MessageType.LOCATION
    assert MessageType.from_code(49) == MessageType.FILE
    assert MessageType.from_code(10000) == MessageType.SYSTEM
    assert MessageType.from_code(10002) == MessageType.EMOJI

    # 未知代码默认按文本处理
    assert MessageType.from_code(999) == MessageType.TEXT
    assert MessageType.from_code("unknown") == MessageType.TEXT
    assert MessageType.from_code("1") == MessageType.TEXT  # 字符串数字


def test_message_data_model():
    """测试 MessageData 数据模型"""
    # 基本构造
    msg = MessageData(
        msg_id="msg_001",
        sender_wxid="wxid_sender",
        receiver_wxid="wxid_receiver",
        content="你好",
        msg_type=MessageType.TEXT,
    )
    assert msg.msg_id == "msg_001"
    assert msg.content == "你好"
    assert msg.msg_type == MessageType.TEXT
    assert msg.is_group is False
    assert msg.at_users == []

    # 群消息 content_body
    group_msg = MessageData(
        msg_id="msg_002",
        sender_wxid="wxid_actual_sender",
        receiver_wxid="wxid_self",
        content="wxid_actual_sender:\n大家好",
        msg_type=MessageType.TEXT,
        is_group=True,
        group_wxid="12345@chatroom",
    )
    assert group_msg.content_body == "大家好"
    assert group_msg.actual_sender_in_group == "wxid_actual_sender"

    # 非群消息 content_body 返回原文
    assert msg.content_body == "你好"
    assert msg.actual_sender_in_group is None

    # is_at 判断
    at_msg = MessageData(
        msg_id="msg_003",
        sender_wxid="wxid_other",
        receiver_wxid="wxid_self",
        content="@你 帮个忙",
        msg_type=MessageType.TEXT,
        at_users=["wxid_self", "wxid_other2"],
    )
    assert at_msg.is_at("wxid_self") is True
    assert at_msg.is_at("wxid_not_at") is False

    # 无 @ 用户
    assert msg.is_at("wxid_self") is False


def test_message_data_validator():
    """测试 MessageData 的 msg_type 验证器"""
    # 传入整数代码
    msg = MessageData(
        msg_id="msg_001",
        sender_wxid="s",
        receiver_wxid="r",
        msg_type=3,  # IMAGE
    )
    assert msg.msg_type == MessageType.IMAGE

    # 传入字符串值
    msg2 = MessageData(
        msg_id="msg_002",
        sender_wxid="s",
        receiver_wxid="r",
        msg_type="image",
    )
    assert msg2.msg_type == MessageType.IMAGE

    # 传入未知字符串 -> 默认 TEXT
    msg3 = MessageData(
        msg_id="msg_003",
        sender_wxid="s",
        receiver_wxid="r",
        msg_type="unknown_type",
    )
    assert msg3.msg_type == MessageType.TEXT


def test_message_data_from_dict():
    """测试 MessageData.from_dict / to_dict"""
    data = {
        "msg_id": "msg_100",
        "sender_wxid": "wxid_from",
        "receiver_wxid": "wxid_to",
        "content": "测试消息",
        "msg_type": "text",
        "is_group": True,
        "group_wxid": "group@chatroom",
        "at_users": ["wxid_self"],
    }
    msg = MessageData.from_dict(data)
    assert msg.msg_id == "msg_100"
    assert msg.is_group is True
    assert msg.group_wxid == "group@chatroom"

    # to_dict 往返
    d = msg.to_dict()
    assert d["msg_id"] == "msg_100"
    assert d["content"] == "测试消息"


def test_send_result():
    """测试 SendResult 模型"""
    # 成功结果
    ok = SendResult.ok("msg_id_123")
    assert ok.success is True
    assert ok.msg_id == "msg_id_123"
    assert ok.error is None

    # 失败结果
    fail = SendResult.fail("发送失败原因")
    assert fail.success is False
    assert fail.msg_id is None
    assert fail.error == "发送失败原因"

    # 无 msg_id 的成功
    ok2 = SendResult.ok()
    assert ok2.success is True
    assert ok2.msg_id is None


# ============================================================================
# 测试: Mock 客户端初始化
# ============================================================================
def test_mock_client_init():
    """测试 Mock 客户端初始化"""
    async def _run_test():
        client = WeChatClient(instance_id="test_init", mock=True)

        # 初始化前状态
        assert client.mock is True
        assert client._initialized is False

        # 初始化
        ok = await client.init("test_init")
        assert ok is True
        assert client._initialized is True

        # 加载窗口 (Mock 模式)
        ok = await client.load_window()
        assert ok is True
        assert client._window_loaded is True

        # 登录信息
        login_info = await client.get_login_info()
        assert login_info["wxid"] == "wxid_self_000"
        assert login_info["nickname"] == "机器人本体"

        # 卸载
        ok = await client.uninstall()
        assert ok is True
        assert client._initialized is False

    _run(_run_test())


def test_create_client_factory():
    """测试 create_client 工厂函数"""
    client = create_client(instance_id="factory_test", mock=True)
    assert isinstance(client, WeChatClient)
    assert client.mock is True
    assert client.instance_id == "factory_test"


# ============================================================================
# 测试: Mock 发送文本消息
# ============================================================================
def test_mock_send_text():
    """测试 Mock 发送文本消息"""
    async def _run_test():
        client = WeChatClient(instance_id="send_text_test", mock=True)
        await client.init("send_text_test")

        # 发送文本
        result = await client.send_text("wxid_test001", "你好，这是测试消息")
        assert result.success is True
        assert result.msg_id is not None
        assert result.msg_id.startswith("mock_send_")

        # 发送空文本应失败
        result_empty = await client.send_text("wxid_test001", "")
        assert result_empty.success is False
        assert "为空" in result_empty.error

        await client.uninstall()

    _run(_run_test())


def test_mock_send_text_long():
    """测试 Mock 发送长文本消息 (分片)"""
    async def _run_test():
        client = WeChatClient(instance_id="send_long_test", mock=True)
        await client.init("send_long_test")

        # 生成长文本 (超过 msg_max_lines)
        from config.settings import settings
        original_max_lines = settings.msg_max_lines
        settings.msg_max_lines = 5  # 临时设置小的分片阈值

        long_text = "\n".join([f"第{i}行内容" for i in range(1, 20)])  # 19行
        result = await client.send_text("wxid_test001", long_text)
        assert result.success is True

        # 恢复设置
        settings.msg_max_lines = original_max_lines

        await client.uninstall()

    _run(_run_test())


# ============================================================================
# 测试: Mock 发送图片
# ============================================================================
def test_mock_send_image():
    """测试 Mock 发送图片"""
    async def _run_test():
        client = WeChatClient(instance_id="send_img_test", mock=True)
        await client.init("send_img_test")

        # 发送图片
        result = await client.send_image("wxid_test001", "/tmp/test_image.png")
        assert result.success is True
        assert result.msg_id is not None
        assert result.msg_id.startswith("mock_img_")

        # 空路径应失败
        result_empty = await client.send_image("wxid_test001", "")
        assert result_empty.success is False

        await client.uninstall()

    _run(_run_test())


def test_mock_send_file():
    """测试 Mock 发送文件"""
    async def _run_test():
        client = WeChatClient(instance_id="send_file_test", mock=True)
        await client.init("send_file_test")

        result = await client.send_file("wxid_test001", "/tmp/test_file.pdf")
        assert result.success is True
        assert result.msg_id is not None
        assert result.msg_id.startswith("mock_file_")

        # 空路径应失败
        result_empty = await client.send_file("wxid_test001", "")
        assert result_empty.success is False

        await client.uninstall()

    _run(_run_test())


# ============================================================================
# 测试: Mock 获取联系人
# ============================================================================
def test_mock_get_contacts():
    """测试 Mock 获取联系人"""
    async def _run_test():
        client = WeChatClient(instance_id="get_contacts_test", mock=True)
        await client.init("get_contacts_test")

        contacts = await client.get_contacts()
        assert isinstance(contacts, list)
        assert len(contacts) > 0, "Mock 联系人列表不应为空"

        # 验证联系人结构
        first = contacts[0]
        assert "wxid" in first
        assert "nickname" in first
        assert "remark" in first

        await client.uninstall()

    _run(_run_test())


def test_mock_get_groups():
    """测试 Mock 获取群聊列表"""
    async def _run_test():
        client = WeChatClient(instance_id="get_groups_test", mock=True)
        await client.init("get_groups_test")

        groups = await client.get_groups()
        assert isinstance(groups, list)
        assert len(groups) > 0

        first = groups[0]
        assert "group_wxid" in first
        assert "group_name" in first
        assert "member_count" in first

        await client.uninstall()

    _run(_run_test())


def test_mock_get_group_members():
    """测试 Mock 获取群成员"""
    async def _run_test():
        client = WeChatClient(instance_id="get_members_test", mock=True)
        await client.init("get_members_test")

        members = await client.get_group_members("12345678901@chatroom")
        assert isinstance(members, list)
        assert len(members) > 0

        first = members[0]
        assert "wxid" in first
        assert "nickname" in first

        await client.uninstall()

    _run(_run_test())


# ============================================================================
# 测试: Mock 消息回调
# ============================================================================
def test_mock_message_callback():
    """测试 Mock 消息回调"""
    async def _run_test():
        client = WeChatClient(instance_id="callback_test", mock=True)
        await client.init("callback_test")
        await client.load_window()

        received = []

        async def on_message(msg: MessageData):
            received.append(msg)

        client.set_message_callback(on_message)

        # 启动模拟消息生成 (短间隔)
        await client.start(msg_interval=0.1)

        # 等待收到消息
        await asyncio.sleep(0.5)

        # 停止
        await client.stop()

        # 应收到至少 1 条模拟消息
        assert len(received) >= 1, f"应收到至少1条消息, 实际 {len(received)}"

        # 验证消息结构
        msg = received[0]
        assert isinstance(msg, MessageData)
        assert msg.msg_id is not None
        assert msg.sender_wxid is not None
        assert msg.content is not None

        await client.uninstall()

    _run(_run_test())


def test_mock_callback_dispatch():
    """测试消息分发 (_dispatch_message)"""
    async def _run_test():
        client = WeChatClient(instance_id="dispatch_test", mock=True)

        received = []

        async def on_message(msg: MessageData):
            received.append(msg)

        client.set_message_callback(on_message)

        # 手动分发消息
        test_msg = MessageData(
            msg_id="manual_001",
            sender_wxid="wxid_manual",
            receiver_wxid="wxid_self",
            content="手动分发测试",
            msg_type=MessageType.TEXT,
        )
        await client._dispatch_message(test_msg)

        assert len(received) == 1
        assert received[0].content == "手动分发测试"


def test_mock_callback_no_callback():
    """测试无回调时分发消息不报错"""
    async def _run_test():
        client = WeChatClient(instance_id="no_cb_test", mock=True)
        # 不设置回调
        test_msg = MessageData(
            msg_id="no_cb_001",
            sender_wxid="s",
            receiver_wxid="r",
            content="无回调测试",
        )
        # 应不抛异常
        await client._dispatch_message(test_msg)

    _run(_run_test())


# ============================================================================
# 测试: Hook 接口定义完整性
# ============================================================================
def test_hook_interface():
    """测试 Hook 接口定义完整性"""
    # WeChatHookInterface 是抽象基类
    assert issubclass(WeChatClient, WeChatHookInterface)

    # 验证抽象方法存在
    abstract_methods = {
        "init", "load_window", "uninstall", "api",
        "send_text", "send_image", "send_file",
        "get_contacts", "get_groups", "get_group_members",
        "get_login_info", "set_message_callback",
    }
    # WeChatClient 应实现所有抽象方法
    for method_name in abstract_methods:
        assert hasattr(WeChatClient, method_name), \
            f"WeChatClient 缺少方法: {method_name}"

    # 验证不能直接实例化抽象类
    try:
        WeChatHookInterface()
        assert False, "抽象类不应能直接实例化"
    except TypeError:
        pass  # 预期行为


def test_mock_data_generator():
    """测试 Mock 数据生成器"""
    # 联系人
    contacts = _MockDataGenerator.contacts()
    assert len(contacts) > 0
    assert all("wxid" in c and "nickname" in c for c in contacts)

    # 群
    groups = _MockDataGenerator.groups()
    assert len(groups) > 0
    assert all("group_wxid" in g for g in groups)

    # 群成员
    members = _MockDataGenerator.group_members("12345678901@chatroom")
    assert len(members) > 0
    assert any(m["wxid"] == "wxid_self_000" for m in members), "群成员应包含自身"

    # 随机消息
    msg = _MockDataGenerator.random_message("wxid_self_000")
    assert isinstance(msg, MessageData)
    assert msg.receiver_wxid == "wxid_self_000"


# ============================================================================
# 测试: API 命令常量 (API[0]~API[24])
# ============================================================================
def test_api_commands():
    """测试 API 命令常量 (API[0]~API[24])"""
    # 验证 25 个命令槽位 (0~24)
    all_cmds = APICommand.all_commands()
    assert len(all_cmds) == 25, f"应有25个命令, 实际 {len(all_cmds)}"

    # 验证命令编号连续 (0~24)
    for i in range(25):
        assert i in all_cmds, f"缺少命令编号 {i}"

    # 验证发送类命令
    assert APICommand.SEND_TEXT == 0
    assert APICommand.SEND_IMAGE == 1
    assert APICommand.SEND_FILE == 2
    assert APICommand.SEND_CARD == 3
    assert APICommand.SEND_LINK == 4
    assert APICommand.SEND_GIF == 5
    assert APICommand.SEND_AT == 6
    assert APICommand.SEND_PATPAT == 7
    assert APICommand.REVOKE_MSG == 8

    # 验证查询类命令
    assert APICommand.GET_CONTACTS == 9
    assert APICommand.GET_GROUPS == 10
    assert APICommand.GET_GROUP_MEMBERS == 11
    assert APICommand.GET_CONTACT_DETAIL == 12
    assert APICommand.GET_PUBLIC_CONTENT == 13
    assert APICommand.GET_LOGIN_INFO == 14

    # 验证好友管理类命令
    assert APICommand.ADD_FRIEND == 15
    assert APICommand.DEL_FRIEND == 16
    assert APICommand.ACCEPT_FRIEND == 17
    assert APICommand.EDIT_REMARK == 18
    assert APICommand.BLACKLIST == 19

    # 验证群管理类命令
    assert APICommand.GROUP_CREATE == 20
    assert APICommand.GROUP_INVITE == 21
    assert APICommand.GROUP_KICK == 22
    assert APICommand.GROUP_ANNOUNCEMENT == 23

    # 验证其他命令
    assert APICommand.OCR_IMAGE == 24

    # 验证命令名称
    assert all_cmds[0] == "SEND_TEXT"
    assert all_cmds[24] == "OCR_IMAGE"


def test_api_mock_api():
    """测试 Mock 模式下 API 调用"""
    async def _run_test():
        client = WeChatClient(instance_id="api_test", mock=True)
        await client.init("api_test")

        # 测试 GET_CONTACTS 命令
        result = await client.api(APICommand.GET_CONTACTS, {})
        assert result["code"] == 0
        assert isinstance(result["data"], list)
        assert len(result["data"]) > 0

        # 测试 GET_GROUPS 命令
        result = await client.api(APICommand.GET_GROUPS, {})
        assert result["code"] == 0
        assert isinstance(result["data"], list)

        # 测试 GET_GROUP_MEMBERS 命令
        result = await client.api(APICommand.GET_GROUP_MEMBERS, {"group_wxid": "12345678901@chatroom"})
        assert result["code"] == 0
        assert isinstance(result["data"], list)

        # 测试 GET_LOGIN_INFO 命令
        result = await client.api(APICommand.GET_LOGIN_INFO, {})
        assert result["code"] == 0
        assert "wxid" in result["data"]

        # 测试 SEND_TEXT 命令
        result = await client.api(APICommand.SEND_TEXT, {"wxid": "test", "content": "hello"})
        assert result["code"] == 0
        assert "msg_id" in result["data"]

        # 测试命令名称转编号
        assert client._normalize_command("SEND_TEXT") == 0
        assert client._normalize_command("0") == 0
        assert client._normalize_command(0) == 0
        assert client._normalize_command("unknown") == 0  # 未知默认0

        await client.uninstall()

    _run(_run_test())
