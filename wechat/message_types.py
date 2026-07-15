"""
微信消息类型定义

定义消息类型枚举、接收消息数据模型(MessageData)与发送结果模型(SendResult)。
使用 Pydantic 做数据验证，所有字段带完整类型标注。
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class MessageType(str, Enum):
    """微信消息类型枚举。

    继承 ``str`` 便于序列化与字典取值。
    """

    TEXT = "text"            # 文本消息
    IMAGE = "image"          # 图片消息
    FILE = "file"            # 文件消息
    VIDEO = "video"          # 视频消息
    VOICE = "voice"          # 语音消息
    CARD = "card"            # 名片消息
    LINK = "link"            # 链接/公众号文章消息
    SYSTEM = "system"        # 系统消息（入群、撤回等）
    EMOJI = "emoji"          # 自定义表情消息
    LOCATION = "location"    # 位置消息

    @classmethod
    def from_code(cls, code: int | str) -> "MessageType":
        """根据微信原始消息类型代码转换为枚举。

        微信原始 type 为整数，这里做常见映射，未知类型默认按文本处理。
        """
        mapping: dict[int, MessageType] = {
            1: cls.TEXT,
            3: cls.IMAGE,
            34: cls.VOICE,
            42: cls.CARD,
            43: cls.VIDEO,
            48: cls.LOCATION,
            49: cls.FILE,   # 49 为 AppMsg，含文件/链接等
            10000: cls.SYSTEM,
            10002: cls.EMOJI,
        }
        if isinstance(code, str):
            code = int(code) if code.isdigit() else 1
        return mapping.get(code, cls.TEXT)


class MessageData(BaseModel):
    """接收到的微信消息数据模型。

    覆盖私聊与群聊场景，群消息额外记录群ID与被@用户列表。
    """

    msg_id: str = Field(..., description="消息ID")
    sender_wxid: str = Field(..., description="发送者wxid")
    receiver_wxid: str = Field(..., description="接收者wxid")
    content: str = Field("", description="消息内容(文本/解析后文本)")
    msg_type: MessageType = Field(MessageType.TEXT, description="消息类型")
    raw_xml: str = Field("", description="原始XML内容")
    timestamp: float = Field(default_factory=time.time, description="消息时间戳(秒)")
    is_group: bool = Field(False, description="是否群消息")
    group_wxid: Optional[str] = Field(None, description="群wxid(仅群消息)")
    at_users: list[str] = Field(default_factory=list, description="被@用户wxid列表")

    # 群消息的"实际发送人"在 content 中可能以 wxid:\n内容 形式出现，
    # 这里提供 content_body 取实际正文。
    @property
    def content_body(self) -> str:
        """获取消息正文。

        群聊文本消息中，原始内容形如 ``wxid_xxx:\n实际内容``，
        本属性返回 ``\\n`` 之后的部分；非该格式则原样返回。
        """
        if self.is_group and ":\n" in self.content:
            return self.content.split(":\n", 1)[1]
        return self.content

    @property
    def actual_sender_in_group(self) -> Optional[str]:
        """群消息中，content 前缀里的实际发送人 wxid（若存在）。"""
        if self.is_group and ":\n" in self.content:
            return self.content.split(":\n", 1)[0]
        return None

    def is_at(self, self_wxid: str) -> bool:
        """判断本条消息是否 @ 了指定 wxid（自己）。"""
        return self_wxid in self.at_users if self.at_users else False

    @field_validator("msg_type", mode="before")
    @classmethod
    def _validate_msg_type(cls, v: Any) -> MessageType:
        """允许传入整数代码或字符串自动转换。"""
        if isinstance(v, MessageType):
            return v
        if isinstance(v, int):
            return MessageType.from_code(v)
        if isinstance(v, str):
            # 优先按枚举值匹配，否则按数字代码解析
            try:
                return MessageType(v)
            except ValueError:
                return MessageType.from_code(v)
        return MessageType.TEXT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MessageData":
        """从字典构造 MessageData，兼容字段缺失。"""
        return cls.model_validate(data)

    def to_dict(self) -> dict[str, Any]:
        """转换为普通字典（便于序列化/日志）。"""
        return self.model_dump()


class SendResult(BaseModel):
    """消息发送结果模型。"""

    success: bool = Field(..., description="是否发送成功")
    msg_id: Optional[str] = Field(None, description="发送成功的消息ID")
    error: Optional[str] = Field(None, description="失败原因(成功时为None)")

    @classmethod
    def ok(cls, msg_id: Optional[str] = None) -> "SendResult":
        """构造成功结果。"""
        return cls(success=True, msg_id=msg_id, error=None)

    @classmethod
    def fail(cls, error: str) -> "SendResult":
        """构造失败结果。"""
        return cls(success=False, msg_id=None, error=error)
