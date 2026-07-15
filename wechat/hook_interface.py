"""
微信 Hook 抽象接口

定义所有微信客户端实现必须遵循的统一接口(WeChatHookInterface)，
以及对应原软件 API[0]~API[24] 的 25 个命令槽位常量。

抽象接口使业务模块与具体 Hook 实现解耦：
- 模拟模式(MockMode)与真实 Hook 模式(HookMode)均实现此接口；
- 业务层只依赖接口，便于单测与切换底层实现。

类型补充
========
- :data:`MessageCallback`   业务层异步消息回调（接收标准 ``MessageData``）
- :data:`MessageHookCallback` Hook 层同步消息回调（接收原始消息字典）
- :class:`WeChatMessage`   Hook 层原始消息数据类，可转换为标准 ``MessageData``
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from wechat.message_types import MessageData, MessageType, SendResult

# 消息回调类型：接收一条 MessageData，无返回值
MessageCallback = Callable[[MessageData], Awaitable[None]]

# Hook 层消息回调类型：接收原始消息字典，无返回值（同步）
# 字典字段见 WeChatMessage；用于 RealWeChatClient 把 Hook 推送的消息
# 转发给业务模块前的中间层处理。
MessageHookCallback = Callable[[dict[str, Any]], None]


@dataclass
class WeChatMessage:
    """Hook 层原始消息数据类。

    表示由内存 Hook 拦截到的微信原始消息，字段尽量贴近微信内部结构，
    尚未标准化为业务层使用的 :class:`MessageData`。可通过
    :meth:`to_message_data` 转换。

    Attributes:
        msg_id: 微信消息ID。
        sender_wxid: 发送者 wxid。
        receiver_wxid: 接收者 wxid（通常为登录账号自身或群 wxid）。
        content: 消息内容（文本/解析后文本；群消息含 ``wxid:\\n`` 前缀）。
        msg_type: 微信原始消息类型代码（1=文本, 3=图片, 49=文件...）。
        is_group: 是否群消息。
        group_wxid: 群 wxid（仅群消息）。
        at_users: 被@用户 wxid 列表。
        raw_xml: 原始 XML 内容（部分消息类型含）。
        timestamp: 消息时间戳（秒）。
        extra: 扩展字段（图片路径/文件名/链接标题等），按消息类型填充。
    """

    msg_id: str = ""
    sender_wxid: str = ""
    receiver_wxid: str = ""
    content: str = ""
    msg_type: int = 1
    is_group: bool = False
    group_wxid: Optional[str] = None
    at_users: list[str] = field(default_factory=list)
    raw_xml: str = ""
    timestamp: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_message_data(self) -> MessageData:
        """转换为业务层标准 :class:`MessageData`。

        将微信原始 ``msg_type`` 代码映射为 :class:`MessageType` 枚举，
        其余字段直接搬移。
        """
        return MessageData(
            msg_id=self.msg_id,
            sender_wxid=self.sender_wxid,
            receiver_wxid=self.receiver_wxid,
            content=self.content,
            msg_type=self.msg_type,
            raw_xml=self.raw_xml,
            timestamp=self.timestamp,
            is_group=self.is_group,
            group_wxid=self.group_wxid,
            at_users=list(self.at_users),
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeChatMessage":
        """从 Hook 推送的消息字典构造。

        缺失字段使用默认值，未知字段归入 ``extra``。
        """
        known = {
            f for f in (
                "msg_id", "sender_wxid", "receiver_wxid", "content",
                "msg_type", "is_group", "group_wxid", "at_users",
                "raw_xml", "timestamp",
            )
        }
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for k, v in data.items():
            if k in known:
                kwargs[k] = v
            else:
                extra[k] = v
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)


class APICommand:
    """API 命令常量。

    对应原软件 DLL 导出的 ``api(command, params)`` 中的 command 编号，
    共 25 个槽位 (0~24)。每个槽位对应一类微信操作。

    .. note::
       本枚举是 *复刻版自身* 的统一命令编号方案，用于在模拟模式与
       真实 Hook 模式间保持调用语义一致。它与 *原易语言软件* 逆向得到
       的 API[0]~API[24] 函数指针表是两套编号体系（原软件的编号语义
       见 :data:`wechat.wechat_offsets.ORIGINAL_API_TABLE`）。真实 Hook
       模式下，:class:`wechat.wechat_client.RealWeChatClient` 会负责把
       本枚举的命令映射为注入 DLL 的 ``api(cmd_id, json)`` 调用。
    """

    # === 发送类 ===
    SEND_TEXT = 0            # API[0]  发送文本消息
    SEND_IMAGE = 1           # API[1]  发送图片消息
    SEND_FILE = 2            # API[2]  发送文件
    SEND_CARD = 3            # API[3]  发送名片
    SEND_LINK = 4            # API[4]  发送链接/公众号文章
    SEND_GIF = 5             # API[5]  发送 GIF 表情
    SEND_AT = 6              # API[6]  发送群@消息
    SEND_PATPAT = 7          # API[7]  拍一拍
    REVOKE_MSG = 8           # API[8]  撤回消息

    # === 查询类 ===
    GET_CONTACTS = 9         # API[9]  获取联系人列表
    GET_GROUPS = 10          # API[10] 获取群聊列表
    GET_GROUP_MEMBERS = 11   # API[11] 获取群成员列表
    GET_CONTACT_DETAIL = 12  # API[12] 获取单个联系人详情
    GET_PUBLIC_CONTENT = 13  # API[13] 获取公众号文章内容
    GET_LOGIN_INFO = 14      # API[14] 获取登录账号信息

    # === 好友管理类 ===
    ADD_FRIEND = 15          # API[15] 添加好友
    DEL_FRIEND = 16          # API[16] 删除好友
    ACCEPT_FRIEND = 17       # API[17] 接受好友请求
    EDIT_REMARK = 18         # API[18] 修改好友备注
    BLACKLIST = 19           # API[19] 拉黑/取消拉黑

    # === 群管理类 ===
    GROUP_CREATE = 20        # API[20] 创建群聊
    GROUP_INVITE = 21        # API[21] 邀请好友入群
    GROUP_KICK = 22          # API[22] 踢出群成员
    GROUP_ANNOUNCEMENT = 23  # API[23] 发布/修改群公告

    # === 其他 ===
    OCR_IMAGE = 24           # API[24] 图片 OCR / 转发图片

    @classmethod
    def all_commands(cls) -> dict[int, str]:
        """返回 {命令编号: 名称} 映射，便于日志与调试。"""
        return {
            cls.SEND_TEXT: "SEND_TEXT",
            cls.SEND_IMAGE: "SEND_IMAGE",
            cls.SEND_FILE: "SEND_FILE",
            cls.SEND_CARD: "SEND_CARD",
            cls.SEND_LINK: "SEND_LINK",
            cls.SEND_GIF: "SEND_GIF",
            cls.SEND_AT: "SEND_AT",
            cls.SEND_PATPAT: "SEND_PATPAT",
            cls.REVOKE_MSG: "REVOKE_MSG",
            cls.GET_CONTACTS: "GET_CONTACTS",
            cls.GET_GROUPS: "GET_GROUPS",
            cls.GET_GROUP_MEMBERS: "GET_GROUP_MEMBERS",
            cls.GET_CONTACT_DETAIL: "GET_CONTACT_DETAIL",
            cls.GET_PUBLIC_CONTENT: "GET_PUBLIC_CONTENT",
            cls.GET_LOGIN_INFO: "GET_LOGIN_INFO",
            cls.ADD_FRIEND: "ADD_FRIEND",
            cls.DEL_FRIEND: "DEL_FRIEND",
            cls.ACCEPT_FRIEND: "ACCEPT_FRIEND",
            cls.EDIT_REMARK: "EDIT_REMARK",
            cls.BLACKLIST: "BLACKLIST",
            cls.GROUP_CREATE: "GROUP_CREATE",
            cls.GROUP_INVITE: "GROUP_INVITE",
            cls.GROUP_KICK: "GROUP_KICK",
            cls.GROUP_ANNOUNCEMENT: "GROUP_ANNOUNCEMENT",
            cls.OCR_IMAGE: "OCR_IMAGE",
        }


class WeChatHookInterface(ABC):
    """微信 Hook 抽象基类。

    所有微信客户端实现（模拟/真实）均需继承并实现以下方法。
    业务模块通过本接口操作微信，不直接依赖具体实现。
    """

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def init(self, instance_id: str) -> bool:
        """初始化 Hook。

        Args:
            instance_id: 机器人实例ID（对应原软件 data/app/{instance_id}）。

        Returns:
            是否初始化成功。
        """
        raise NotImplementedError

    @abstractmethod
    async def load_window(self) -> bool:
        """查找并绑定微信主窗口。

        真实模式下需微信已启动并登录。

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
    async def api(self, command: str, params: dict) -> dict:
        """核心 API 入口。

        对应原软件 ``api(command, params_json)`` 调用，所有功能均经此转发。

        Args:
            command: 命令编号或名称（见 :class:`APICommand`）。
            params: 命令参数字典。

        Returns:
            Hook 返回的结果字典，通常含 ``code``/``data`` 字段。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 消息发送
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def send_text(self, wxid: str, text: str) -> SendResult:
        """发送文本消息。

        Args:
            wxid: 接收者 wxid（个人或群）。
            text: 文本内容。

        Returns:
            发送结果。
        """
        raise NotImplementedError

    @abstractmethod
    async def send_image(self, wxid: str, path: str) -> SendResult:
        """发送图片消息。

        Args:
            wxid: 接收者 wxid。
            path: 图片本地绝对路径。

        Returns:
            发送结果。
        """
        raise NotImplementedError

    @abstractmethod
    async def send_file(self, wxid: str, path: str) -> SendResult:
        """发送文件消息。

        Args:
            wxid: 接收者 wxid。
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
        """获取联系人列表。

        Returns:
            联系人字典列表，每项含 wxid/nickname/remark 等。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_groups(self) -> list[dict[str, Any]]:
        """获取群聊列表。

        Returns:
            群字典列表，每项含 group_wxid/group_name 等。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_group_members(self, group_wxid: str) -> list[dict[str, Any]]:
        """获取指定群成员列表。

        Args:
            group_wxid: 群 wxid。

        Returns:
            成员字典列表，每项含 wxid/nickname/display_name 等。
        """
        raise NotImplementedError

    @abstractmethod
    async def get_login_info(self) -> dict[str, Any]:
        """获取当前登录账号信息。

        Returns:
            含 wxid/nickname/alias/account 等的字典。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 消息回调
    # ------------------------------------------------------------------ #
    @abstractmethod
    def set_message_callback(self, callback: MessageCallback) -> None:
        """注册消息接收回调。

        当收到新消息时，实现应异步调用 ``callback(message)``。

        Args:
            callback: 异步消息回调函数。
        """
        raise NotImplementedError
