"""
内存 Hook 单元测试

测试范围:
  - wechat/memory_hook.py : 微信消息接收 Hook

测试内容:
  - HookMessageType 枚举与 from_code 转换
  - MSG_TYPE_ALL 通配符常量
  - MessageHook 初始化与属性
  - register_callback() / unregister_callback() / clear_callbacks() 回调注册
  - feed_message() 手动注入消息并触发回调 (精确类型 + 通配回调)
  - install_hook() 在非 Windows 或偏移量未配置时的降级行为
  - uninstall_hook() 行为

所有测试在 Linux 环境运行, 验证降级行为不崩溃, 且回调机制可正常工作。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest

from wechat.memory_hook import (
    HookMessageType,
    MSG_TYPE_ALL,
    MessageHook,
    IS_WINDOWS,
)
from wechat.dll_injector import PlatformNotSupportedError


# ============================================================================
# 测试: HookMessageType 枚举
# ============================================================================
def test_hook_message_type_values():
    """HookMessageType 各枚举值与微信原始 type 代码一致"""
    assert HookMessageType.TEXT == 1
    assert HookMessageType.IMAGE == 3
    assert HookMessageType.VOICE == 34
    assert HookMessageType.CARD == 42
    assert HookMessageType.VIDEO == 43
    assert HookMessageType.LOCATION == 48
    assert HookMessageType.FILE == 49
    assert HookMessageType.SYSTEM == 10000
    assert HookMessageType.EMOJI == 10002


def test_hook_message_type_from_code_known():
    """from_code 能正确转换已知代码"""
    assert HookMessageType.from_code(1) == HookMessageType.TEXT
    assert HookMessageType.from_code(3) == HookMessageType.IMAGE
    assert HookMessageType.from_code(34) == HookMessageType.VOICE
    assert HookMessageType.from_code(49) == HookMessageType.FILE
    assert HookMessageType.from_code(10000) == HookMessageType.SYSTEM
    assert HookMessageType.from_code(10002) == HookMessageType.EMOJI


def test_hook_message_type_from_code_unknown():
    """未知代码 from_code 默认归为 TEXT"""
    assert HookMessageType.from_code(999) == HookMessageType.TEXT
    assert HookMessageType.from_code(0) == HookMessageType.TEXT


def test_hook_message_type_from_code_string():
    """from_code 支持字符串数字"""
    assert HookMessageType.from_code("1") == HookMessageType.TEXT
    assert HookMessageType.from_code("49") == HookMessageType.FILE
    # 非数字字符串归为 TEXT
    assert HookMessageType.from_code("abc") == HookMessageType.TEXT


def test_hook_message_type_is_int_enum():
    """HookMessageType 是 IntEnum, 可与整数比较"""
    assert HookMessageType.TEXT == 1
    assert int(HookMessageType.IMAGE) == 3


# ============================================================================
# 测试: MSG_TYPE_ALL 通配符
# ============================================================================
def test_msg_type_all_is_wildcard():
    """MSG_TYPE_ALL 应为通配符 '*'"""
    assert MSG_TYPE_ALL == "*"


# ============================================================================
# 测试: MessageHook 初始化
# ============================================================================
def test_message_hook_init_default():
    """MessageHook 默认初始化"""
    hook = MessageHook()
    assert hook is not None
    assert hook.is_installed is False
    assert hook.message_window_handle == 0
    assert hook.wechat_pid is None


def test_message_hook_init_with_injector():
    """MessageHook 可传入 injector 与 dll_module_handle"""
    fake_injector = object()
    hook = MessageHook(injector=fake_injector, dll_module_handle=0x1000)
    assert hook._injector is fake_injector
    assert hook._dll_handle == 0x1000


def test_message_hook_init_custom_window_class():
    """MessageHook 支持自定义窗口类名"""
    hook = MessageHook(window_class_name="MyHookWnd")
    assert hook._window_class_name == "MyHookWnd"


# ============================================================================
# 测试: 回调注册
# ============================================================================
def test_register_callback_text():
    """注册文本消息回调"""
    hook = MessageHook()
    received = []

    def on_text(msg):
        received.append(msg)

    hook.register_callback(HookMessageType.TEXT, on_text)
    # 注入一条文本消息
    hook.feed_message({
        "msg_id": "m1",
        "sender_wxid": "wxid_a",
        "content": "hello",
        "msg_type": int(HookMessageType.TEXT),
    })
    assert len(received) == 1
    assert received[0]["msg_id"] == "m1"


def test_register_callback_all():
    """通配回调接收所有类型消息"""
    hook = MessageHook()
    received = []

    def on_all(msg):
        received.append(msg)

    hook.register_callback(MSG_TYPE_ALL, on_all)
    # 注入不同类型消息
    hook.feed_message({"msg_type": int(HookMessageType.TEXT), "content": "文本"})
    hook.feed_message({"msg_type": int(HookMessageType.IMAGE), "content": "图片"})
    hook.feed_message({"msg_type": int(HookMessageType.FILE), "content": "文件"})
    assert len(received) == 3


def test_register_callback_all_string_star():
    """用字符串 '*' 注册通配回调等效于 MSG_TYPE_ALL"""
    hook = MessageHook()
    received = []

    def on_all(msg):
        received.append(msg)

    hook.register_callback("*", on_all)
    hook.feed_message({"msg_type": int(HookMessageType.TEXT), "content": "x"})
    assert len(received) == 1


def test_register_callback_int_type():
    """用整数注册回调"""
    hook = MessageHook()
    received = []

    def on_image(msg):
        received.append(msg)

    hook.register_callback(3, on_image)  # 3 = IMAGE
    hook.feed_message({"msg_type": 3, "content": "图片"})
    assert len(received) == 1


def test_register_callback_dispatch_order():
    """分发时先精确类型回调, 后通配回调"""
    hook = MessageHook()
    order = []

    def on_exact(msg):
        order.append("exact")

    def on_all(msg):
        order.append("all")

    hook.register_callback(HookMessageType.TEXT, on_exact)
    hook.register_callback(MSG_TYPE_ALL, on_all)
    hook.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert order == ["exact", "all"]


def test_register_callback_no_duplicate():
    """同一回调重复注册不会重复触发"""
    hook = MessageHook()
    received = []

    def cb(msg):
        received.append(msg)

    hook.register_callback(HookMessageType.TEXT, cb)
    hook.register_callback(HookMessageType.TEXT, cb)  # 重复注册
    hook.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert len(received) == 1


def test_unregister_callback():
    """取消注册后回调不再触发"""
    hook = MessageHook()
    received = []

    def cb(msg):
        received.append(msg)

    hook.register_callback(HookMessageType.TEXT, cb)
    hook.unregister_callback(HookMessageType.TEXT, cb)
    hook.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert len(received) == 0


def test_clear_callbacks():
    """清空所有回调"""
    hook = MessageHook()
    received = []

    def cb(msg):
        received.append(msg)

    hook.register_callback(HookMessageType.TEXT, cb)
    hook.register_callback(MSG_TYPE_ALL, cb)
    hook.clear_callbacks()
    hook.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert len(received) == 0


def test_callback_exception_does_not_break_dispatch():
    """单个回调抛异常不影响后续回调执行"""
    hook = MessageHook()
    received = []

    def bad_cb(msg):
        raise ValueError("回调异常测试")

    def good_cb(msg):
        received.append(msg)

    hook.register_callback(HookMessageType.TEXT, bad_cb)
    hook.register_callback(HookMessageType.TEXT, good_cb)
    # 不应抛异常
    hook.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert len(received) == 1


# ============================================================================
# 测试: feed_message 注入消息
# ============================================================================
def test_feed_message_adds_timestamp():
    """feed_message 自动补充 timestamp 字段"""
    hook = MessageHook()
    received = []

    def cb(msg):
        received.append(msg)

    hook.register_callback(MSG_TYPE_ALL, cb)
    hook.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert "timestamp" in received[0]
    assert isinstance(received[0]["timestamp"], float)


def test_feed_message_preserves_existing_timestamp():
    """feed_message 保留已存在的 timestamp"""
    hook = MessageHook()
    received = []

    def cb(msg):
        received.append(msg)

    hook.register_callback(MSG_TYPE_ALL, cb)
    hook.feed_message({"msg_type": int(HookMessageType.TEXT), "timestamp": 12345.0})
    assert received[0]["timestamp"] == 12345.0


def test_feed_message_no_matching_callback():
    """注入的消息类型无对应回调时不报错"""
    hook = MessageHook()
    # 仅注册文本回调, 注入图片消息
    hook.register_callback(HookMessageType.TEXT, lambda m: None)
    hook.feed_message({"msg_type": int(HookMessageType.IMAGE)})
    # 不抛异常即通过


def test_feed_message_full_dict():
    """feed_message 注入完整消息字典"""
    hook = MessageHook()
    received = []

    def cb(msg):
        received.append(msg)

    hook.register_callback(MSG_TYPE_ALL, cb)
    full_msg = {
        "msg_id": "msg_full_001",
        "sender_wxid": "wxid_sender",
        "receiver_wxid": "wxid_self",
        "content": "完整消息内容",
        "msg_type": int(HookMessageType.TEXT),
        "is_group": True,
        "group_wxid": "12345@chatroom",
        "raw_xml": "<xml>raw</xml>",
    }
    hook.feed_message(full_msg)
    assert received[0]["msg_id"] == "msg_full_001"
    assert received[0]["is_group"] is True
    assert received[0]["group_wxid"] == "12345@chatroom"


# ============================================================================
# 测试: install_hook 降级行为
# ============================================================================
def test_install_hook_non_windows_raises():
    """非 Windows 环境下 install_hook 抛 PlatformNotSupportedError"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    hook = MessageHook()
    with pytest.raises(PlatformNotSupportedError):
        hook.install_hook(1234)


def test_install_hook_non_windows_with_injector_raises():
    """非 Windows 环境下即使有 injector, install_hook 也抛异常"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    fake_injector = object()
    hook = MessageHook(injector=fake_injector, dll_module_handle=0x1000)
    with pytest.raises(PlatformNotSupportedError):
        hook.install_hook(1234)


# ============================================================================
# 测试: uninstall_hook 行为
# ============================================================================
def test_uninstall_hook_non_windows_returns_true():
    """非 Windows 环境下 uninstall_hook 返回 True (降级, 直接置 installed=False)"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    hook = MessageHook()
    # 未安装直接卸载
    result = hook.uninstall_hook()
    assert result is True
    assert hook.is_installed is False


def test_uninstall_hook_when_not_installed():
    """Hook 未安装时 uninstall_hook 返回 True (无需卸载)"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    hook = MessageHook()
    assert hook.is_installed is False
    result = hook.uninstall_hook()
    assert result is True


def test_uninstall_hook_after_fake_install():
    """模拟已安装状态后卸载 (非 Windows 直接清理 installed 标志)"""
    if IS_WINDOWS:
        pytest.skip("仅在非 Windows 平台测试降级行为")
    hook = MessageHook()
    # 手动标记为已安装 (绕过 install_hook 的平台检查)
    hook._installed = True
    result = hook.uninstall_hook()
    # 非 Windows 降级路径: 仅置 installed=False 并返回 True
    assert result is True
    assert hook.is_installed is False


# ============================================================================
# 测试: 多 Hook 实例隔离
# ============================================================================
def test_multiple_hooks_isolated():
    """多个 MessageHook 实例的回调互不影响"""
    hook1 = MessageHook()
    hook2 = MessageHook()
    received1 = []
    received2 = []

    hook1.register_callback(HookMessageType.TEXT, lambda m: received1.append(m))
    hook2.register_callback(HookMessageType.TEXT, lambda m: received2.append(m))

    hook1.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert len(received1) == 1
    assert len(received2) == 0

    hook2.feed_message({"msg_type": int(HookMessageType.TEXT)})
    assert len(received1) == 1
    assert len(received2) == 1


# ============================================================================
# 测试: _normalize_msg_type 归一化
# ============================================================================
def test_normalize_msg_type_enum():
    """枚举类型归一化为字符串"""
    key = MessageHook._normalize_msg_type(HookMessageType.TEXT)
    assert key == "1"


def test_normalize_msg_type_int():
    """整数归一化为字符串"""
    key = MessageHook._normalize_msg_type(49)
    assert key == "49"


def test_normalize_msg_type_wildcard():
    """通配符归一化为 '*'"""
    assert MessageHook._normalize_msg_type(MSG_TYPE_ALL) == "*"
    assert MessageHook._normalize_msg_type("*") == "*"


def test_normalize_msg_type_string():
    """字符串原样返回"""
    assert MessageHook._normalize_msg_type("10000") == "10000"
