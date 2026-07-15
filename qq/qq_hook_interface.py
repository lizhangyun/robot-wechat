"""
QQ Hook 抽象接口

定义所有 QQ 客户端实现必须遵循的统一接口(QQHookInterface)，
以及对应原软件 qq.dll 的 API 命令枚举(QQAPICommand)。

QQ NT 与微信的接口差异
======================
- QQ 使用 ``uin``（QQ号）作为用户标识，微信使用 ``wxid``；
- QQ NT 基于 Electron + Node.js 架构，Hook 目标为 ``wrapper.node``；
- 原软件 ``qq.dll`` 与 ``weixin.dll`` 导出相同的四个函数：
  ``init`` / ``api`` / ``loadWindow`` / ``uninstall``。

本模块复用 :mod:`wechat.message_types` 中的 ``MessageData`` / ``SendResult``
作为消息数据模型（其字段足够通用），QQ 客户端以 uin 填充 ``sender_wxid``
等字段，业务层无需关心底层是 QQ 还是微信。

抽象接口使业务模块与具体 Hook 实现解耦：
- 模拟模式(MockMode)与真实 Hook 模式(HookMode)均实现此接口；
- 业务层只依赖接口，便于单测与切换底层实现。

类型补充
========
- :data:`QQMessageCallback`   业务层异步消息回调（接收标准 ``MessageData``）
- :data:`QQHookCallback`      Hook 层同步消息回调（接收原始消息字典）
- :class:`QQMessage`          Hook 层原始消息数据类，可转换为标准 ``MessageData``
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Optional, Union

from wechat.message_types import MessageData, MessageType, SendResult

# 消息回调类型：接收一条 MessageData，无返回值
QQMessageCallback = Callable[[MessageData], Awaitable[None]]

# Hook 层消息回调类型：接收原始消息字典，无返回值（同步）
QQHookCallback = Callable[[dict[str, Any]], None]


# ====================================================================== #
#  QQ API 命令枚举（IntEnum）
# ====================================================================== #
class QQAPICommand(IntEnum):
    """QQ API 命令枚举。

    对应原软件 ``qq.dll`` 导出的 ``api(command, params)`` 中的 command 编号。
    采用 :class:`IntEnum` 以便直接作为 ctypes 的 int 参数传递。

    .. note::
       本枚举是 *复刻版自身* 的统一命令编号方案，用于在模拟模式与
       真实 Hook 模式间保持调用语义一致。它与原易语言软件逆向得到的
       API 编号语义一致（见 :data:`qq.qq_offsets.ORIGINAL_API_TABLE`）。
    """

    # === 生命周期 ===
    INIT = 0              # 初始化（获取登录信息/完成初始化）

    # === 发送类 ===
    SEND_TEXT = 1         # 发送文本消息
    SEND_IMAGE = 2        # 发送图片消息
    SEND_FILE = 3         # 发送文件
    SEND_AT = 4           # 发送群@消息
    SEND_CARD = 14        # 发送名片
    SEND_LINK = 15        # 发送链接
    SEND_APP = 16         # 发送小程序/App消息
    FORWARD_MSG = 17      # 转发消息
    REVOKE_MSG = 18       # 撤回消息

    # === 查询类 ===
    GET_CONTACTS = 5      # 获取好友/联系人列表
    GET_GROUPS = 6        # 获取群聊列表
    GET_GROUP_MEMBERS = 7 # 获取群成员列表
    EDIT_REMARK = 8       # 修改备注
    GROUP_ANNOUNCEMENT = 9  # 发布/修改群公告
    GROUP_KICK = 10       # 踢出群成员
    GROUP_INVITE = 11     # 邀请好友入群
    GET_MSG_RECORD = 12   # 获取消息记录
    GROUP_QRCODE = 19     # 获取群二维码

    # === 消息接收（Hook 回调） ===
    RECV_MSG = 13         # 接收新消息（Hook 回调推送）

    # === 群管理类 ===
    GROUP_CREATE = 20     # 创建群聊
    GROUP_QUIT = 21       # 退出群
    GROUP_RENAME = 22     # 修改群名
    GROUP_MUTE = 23       # 群禁言

    # === 其他 ===
    GET_LOGIN_INFO = 24   # 获取登录账号信息

    @classmethod
    def all_commands(cls) -> dict[int, str]:
        """返回 {命令编号: 名称} 映射，便于日志与调试。"""
        return {int(c): c.name for c in cls}

    @classmethod
    def from_value(cls, value: Union[int, str, "QQAPICommand"]) -> "QQAPICommand":
        """从 int / 字符串 / 枚举值转换为 :class:`QQAPICommand`。

        未知值返回 :attr:`INIT`。
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError:
                return cls.INIT
        if isinstance(value, str):
            if value.isdigit():
                try:
                    return cls(int(value))
                except ValueError:
                    return cls.INIT
            try:
                return cls[value.upper()]
            except KeyError:
                return cls.INIT
        return cls.INIT


# ====================================================================== #
#  Hook 层原始消息数据类
# ====================================================================== #
@dataclass
class QQMessage:
    """Hook 层原始消息数据类。

    表示由内存 Hook 拦截到的 QQ 原始消息，字段尽量贴近 QQ NT 内部结构，
    尚未标准化为业务层使用的 :class:`MessageData`。可通过
    :meth:`to_message_data` 转换。

    QQ 以 ``uin``（QQ号数字字符串）标识用户，转换时填入 ``sender_wxid``
    等字段以复用 :class:`MessageData`。

    Attributes:
        msg_id: QQ 消息ID。
        sender_uin: 发送者 uin（QQ号）。
        receiver_uin: 接收者 uin（通常为登录账号自身或群号）。
        content: 消息内容（文本/解析后文本）。
        msg_type: QQ 原始消息类型代码（1=文本, 2=图片, 3=文件...）。
        is_group: 是否群消息。
        group_uin: 群号（仅群消息）。
        at_users: 被@用户 uin 列表。
        raw_data: 原始数据内容（部分消息类型含）。
        timestamp: 消息时间戳（秒）。
        extra: 扩展字段（图片路径/文件名/链接标题等），按消息类型填充。
    """

    msg_id: str = ""
    sender_uin: str = ""
    receiver_uin: str = ""
    content: str = ""
    msg_type: int = 1
    is_group: bool = False
    group_uin: Optional[str] = None
    at_users: list[str] = field(default_factory=list)
    raw_data: str = ""
    timestamp: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_message_data(self) -> MessageData:
        """转换为业务层标准 :class:`MessageData`。

        将 QQ 原始 ``msg_type`` 代码映射为 :class:`MessageType` 枚举，
        uin 填入 wxid 字段以复用统一消息模型。
        """
        return MessageData(
            msg_id=self.msg_id,
            sender_wxid=self.sender_uin,
            receiver_wxid=self.receiver_uin,
            content=self.content,
            msg_type=self.msg_type,
            raw_xml=self.raw_data,
            timestamp=self.timestamp,
            is_group=self.is_group,
            group_wxid=self.group_uin,
            at_users=list(self.at_users),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QQMessage":
        """从 Hook 推送的消息字典构造。

        兼容 ``sender_uin``/``sender_wxid`` 两种字段名，
        缺失字段使用默认值，未知字段归入 ``extra``。
        """
        known = {
            f for f in (
                "msg_id", "sender_uin", "sender_wxid", "receiver_uin",
                "receiver_wxid", "content", "msg_type", "is_group",
                "group_uin", "group_wxid", "at_users", "raw_data",
                "raw_xml", "timestamp",
            )
        }
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for k, v in data.items():
            if k in known:
                # 兼容 wxid 字段名 -> uin 字段
                if k == "sender_wxid":
                    kwargs.setdefault("sender_uin", v)
                elif k == "receiver_wxid":
                    kwargs.setdefault("receiver_uin", v)
                elif k == "group_wxid":
                    kwargs.setdefault("group_uin", v)
                elif k == "raw_xml":
                    kwargs.setdefault("raw_data", v)
                else:
                    kwargs[k] = v
            else:
                extra[k] = v
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)


# ====================================================================== #
#  QQ Hook 抽象接口
# ====================================================================== #
class QQHookInterface(ABC):
    """QQ Hook 抽象基类。

    所有 QQ 客户端实现（模拟/真实）均需继承并实现以下方法。
    业务模块通过本接口操作 QQ，不直接依赖具体实现。

    与 :class:`wechat.hook_interface.WeChatHookInterface` 接口语义一致，
    仅 ``api`` 的 ``cmd`` 参数类型为 ``int``（对应 :class:`QQAPICommand`）。
    """

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def init(self, instance_id: str) -> bool:
        """初始化 Hook。

        Args:
            instance_id: 机器人实例ID。

        Returns:
            是否初始化成功。
        """
        raise NotImplementedError

    @abstractmethod
    async def load_window(self) -> bool:
        """查找并绑定 QQ 主窗口。

        真实模式下需 QQ 已启动并登录。

        Returns:
            是否成功绑定窗口。
        """
        raise NotImplementedError

    @abstractmethod
    async def uninstall(self) -> bool:
        """卸载 Hook，释放注入资源。

        Returns:
            是否卸载成功。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 核心 API 入口
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def api(self, cmd: int, data: dict) -> dict:
        """核心 API 入口。

        对应原软件 ``qq.dll`` 的 ``api(command, params_json)`` 调用，
        所有功能均经此转发。

        Args:
            cmd: 命令编号（见 :class:`QQAPICommand`）。
            data: 命令参数字典。

        Returns:
            Hook 返回的结果字典，通常含 ``code``/``data`` 字段。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 消息发送
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def send_text(self, uin: str, text: str) -> SendResult:
        """发送文本消息。

        Args:
            uin: 接收者 uin（QQ号或群号）。
            text: 文本内容。

        Returns:
            发送结果。
        """
        raise NotImplementedError

    @abstractmethod
    async def send_image(self, uin: str, path: str) -> SendResult:
        """发送图片消息。

        Args:
            uin: 接收者 uin。
            path: 图片本地绝对路径。

        Returns:
            发送结果。
        """
        raise NotImplementedError

    @abstractmethod
    async def send_file(self, uin: str, path: str) -> SendResult:
        """发送文件消息。

        Args:
            uin: 接收者 uin。
            path: 文件本地绝对路径。

        Returns:
            发送结果。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 联系人 / 群查询
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def get_contacts(self) -> list[dict[str, Any]]:
        """获取好友/联系人列表。

        Returns:
            联系人字典列表，每项含 uin/nickname/remark 等。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_groups(self) -> list[dict[str, Any]]:
        """获取群聊列表。

        Returns:
            群字典列表，每项含 group_uin/group_name 等。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_group_members(self, group_uin: str) -> list[dict[str, Any]]:
        """获取指定群成员列表。

        Args:
            group_uin: 群号。

        Returns:
            成员字典列表，每项含 uin/nickname/display_name 等。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_login_info(self) -> dict[str, Any]:
        """获取当前登录账号信息。

        Returns:
            含 uin/nickname/account 等的字典。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 消息回调
    # ------------------------------------------------------------------ #
    @abstractmethod
    def set_message_callback(self, callback: QQMessageCallback) -> None:
        """注册消息接收回调。

        当收到新消息时，实现应异步调用 ``callback(message)``。

        Args:
            callback: 异步消息回调函数。
        """
        raise NotImplementedError


# ====================================================================== #
#  QQ 消息 Hook
# ====================================================================== #
class QQMessageHook:
    """QQ 消息接收 Hook。

    负责安装/卸载 Hook 并管理消息回调。安装 Hook 后，QQ 收到的每条
    消息都会触发已注册的回调。

    与 :class:`wechat.memory_hook.MessageHook` 类似，但接口为异步
    （``async def``），便于在异步业务层直接 ``await``。

    真实 Hook 安装依赖 Windows 平台与已注入的 ``qq.dll``；非 Windows
    平台 ``install_hook`` 返回 False 而非抛异常（优雅降级）。

    典型用法::

        hook = QQMessageHook(injector=injector, dll_handle=handle)
        await hook.register_callback(QQAPICommand.RECV_MSG, on_msg)
        await hook.install_hook(qq_pid)
        ...
        await hook.uninstall_hook()

    Args:
        injector: 已注入 qq.dll 的 :class:`DLLInjector` 实例（可选）。
        dll_handle: qq.dll 在 QQ 进程中的模块句柄（由 inject_dll 返回）。
        window_class_name: 消息接收窗口的类名（用于接收 WM_COPYDATA）。
    """

    def __init__(
        self,
        injector: Optional[Any] = None,
        dll_handle: int = 0,
        window_class_name: str = "QQHookMsgWnd",
    ) -> None:
        self._injector = injector
        self._dll_handle: int = dll_handle
        self._window_class_name: str = window_class_name

        # 消息回调：{msg_type_or_"*": [callbacks]}
        self._callbacks: dict[str, list[QQHookCallback]] = {}
        self._callbacks_lock_added = False  # 标记是否有回调

        # Hook 状态
        self._installed: bool = False
        self._qq_pid: Optional[int] = None

    # ------------------------------------------------------------------ #
    #  属性
    # ------------------------------------------------------------------ #
    @property
    def is_installed(self) -> bool:
        """Hook 是否已安装。"""
        return self._installed

    @property
    def qq_pid(self) -> Optional[int]:
        """已安装 Hook 的 QQ 进程 PID。"""
        return self._qq_pid

    # ------------------------------------------------------------------ #
    #  回调注册
    # ------------------------------------------------------------------ #
    async def register_callback(
        self,
        msg_type: Union[int, str],
        callback: QQHookCallback,
    ) -> None:
        """注册消息回调。

        Args:
            msg_type: 消息类型（int / "*"）。
                使用 ``"*"`` 注册全局回调，接收所有类型。
            callback: 回调函数，签名为 ``callback(msg_dict) -> None``。
        """
        key = self._normalize_msg_type(msg_type)
        if key not in self._callbacks:
            self._callbacks[key] = []
        if callback not in self._callbacks[key]:
            self._callbacks[key].append(callback)
        self._callbacks_lock_added = True

    async def unregister_callback(
        self,
        msg_type: Union[int, str],
        callback: QQHookCallback,
    ) -> None:
        """取消注册消息回调。"""
        key = self._normalize_msg_type(msg_type)
        if key in self._callbacks and callback in self._callbacks[key]:
            self._callbacks[key].remove(callback)

    def clear_callbacks(self) -> None:
        """清空所有回调。"""
        self._callbacks.clear()
        self._callbacks_lock_added = False

    @staticmethod
    def _normalize_msg_type(msg_type: Union[int, str]) -> str:
        """归一化消息类型为字符串键。"""
        if msg_type == "*" or msg_type == -1:
            return "*"
        if isinstance(msg_type, int):
            return str(msg_type)
        return str(msg_type)

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        """将消息分发给匹配的回调。

        先调用精确类型回调，再调用通配回调。
        """
        msg_type_code = msg.get("msg_type")
        type_key = str(msg_type_code) if msg_type_code is not None else ""

        cbs_exact = self._callbacks.get(type_key, [])
        cbs_all = self._callbacks.get("*", [])

        for cb in list(cbs_exact) + list(cbs_all):
            try:
                cb(msg)
            except Exception as e:  # noqa: BLE001
                from loguru import logger
                logger.exception(f"QQ 消息回调执行异常: {e}")

    # ------------------------------------------------------------------ #
    #  Hook 安装 / 卸载
    # ------------------------------------------------------------------ #
    async def install_hook(self, qq_pid: int) -> bool:
        """安装消息接收 Hook。

        流程：
        1. 校验偏移量是否已配置（真实模式）；
        2. 调用注入 qq.dll 的 ``installHook`` 导出函数（Windows）；
        3. 非 Windows 平台直接返回 False（优雅降级）。

        Args:
            qq_pid: QQ 进程 PID。

        Returns:
            安装成功返回 True。
        """
        from loguru import logger

        if self._installed:
            logger.warning("QQ Hook 已安装，请先卸载")
            return True

        # 非 Windows 平台优雅降级
        try:
            from wechat.dll_injector import IS_WINDOWS  # noqa: F401
            if not IS_WINDOWS:
                logger.debug("非 Windows 平台，QQ Hook 安装跳过（优雅降级）")
                return False
        except Exception:  # noqa: BLE001
            return False

        # 校验偏移量
        from qq.qq_offsets import is_offset_available
        if not is_offset_available("RecvMsg"):
            logger.error(
                "QQ RecvMsg 偏移量未配置（仍为占位符 0x0），无法安装 Hook。"
            )
            return False

        self._qq_pid = qq_pid
        logger.info(f"开始安装 QQ 消息 Hook: pid={qq_pid}")

        # 通过注入 DLL 的 installHook 导出函数安装
        ok = False
        if self._injector is not None and self._dll_handle:
            ok = self._install_via_dll()
        if not ok:
            logger.warning("QQ Hook 安装失败（DLL 不可用或调用失败）")
            return False

        self._installed = True
        logger.info(f"QQ 消息 Hook 安装成功: pid={qq_pid}")
        return True

    async def uninstall_hook(self) -> None:
        """卸载消息 Hook，恢复 QQ 原始函数。"""
        from loguru import logger

        if not self._installed:
            logger.debug("QQ Hook 未安装，无需卸载")
            return

        logger.info(f"开始卸载 QQ 消息 Hook: pid={self._qq_pid}")

        # 通过注入 DLL 的 uninstallHook 导出函数卸载
        if self._injector is not None and self._dll_handle and self._qq_pid:
            self._uninstall_via_dll()

        self._installed = False
        self._qq_pid = None
        logger.info("QQ 消息 Hook 卸载完成")

    def _install_via_dll(self) -> bool:
        """通过注入 qq.dll 的 installHook 导出函数安装 Hook。"""
        from loguru import logger
        if self._injector is None or not self._dll_handle or not self._qq_pid:
            return False
        try:
            exit_code = self._injector.call_remote_function(
                self._qq_pid,
                self._dll_handle,
                "installHook",
                args=0,
                timeout_ms=10000,
            )
            if exit_code:
                logger.info(f"QQ DLL installHook 成功 ret={exit_code}")
                return True
            logger.warning("QQ DLL installHook 返回 0")
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QQ DLL installHook 调用失败: {e}")
            return False

    def _uninstall_via_dll(self) -> bool:
        """通过注入 qq.dll 的 uninstallHook 导出函数卸载 Hook。"""
        from loguru import logger
        if self._injector is None or not self._dll_handle or not self._qq_pid:
            return False
        try:
            self._injector.call_remote_function(
                self._qq_pid,
                self._dll_handle,
                "uninstallHook",
                args=None,
                timeout_ms=10000,
            )
            logger.info("QQ DLL uninstallHook 完成")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QQ DLL uninstallHook 调用失败: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  手动注入消息（测试/模拟用）
    # ------------------------------------------------------------------ #
    def feed_message(self, msg: dict[str, Any]) -> None:
        """手动注入一条消息并分发给回调（用于测试或模拟模式）。

        Args:
            msg: 消息字典。
        """
        msg.setdefault("timestamp", time.time())
        self._dispatch_message(msg)
