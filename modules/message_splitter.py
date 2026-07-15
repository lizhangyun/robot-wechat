"""
消息分片器 - 对应原软件 msg_split 功能

原软件 config.ini 中的消息分片配置：
  [msg_split]
  status=1          ; 1=启用分片, 0=禁用
  [msg 消息最多行数]
  消息最多行数=70   ; 单条消息最多 70 行
  [sleep_time]
  sec=1             ; 消息发送间隔 1 秒

超过最大行数的消息自动分片发送，每片之间间隔 sleep_sec 秒，
防止微信风控（短时间内发送大量消息会被限制）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

# 独立运行支持：将项目根目录加入 sys.path
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from loguru import logger

if TYPE_CHECKING:
    from wechat.hook_interface import WeChatHookInterface
    from wechat.message_types import SendResult


class MessageSplitter:
    """消息分片发送，对应原软件 msg_split 功能。

    将超长文本按行数分片，每片独立发送，片间间隔 ``sleep_sec`` 秒。

    Args:
        max_lines: 单条消息最多行数（默认 70，与原软件一致）。
        sleep_sec: 分片发送间隔秒数（默认 1.0，与原软件一致）。
    """

    def __init__(
        self, max_lines: int = 70, sleep_sec: float = 1.0
    ) -> None:
        if max_lines < 1:
            raise ValueError("max_lines 必须 >= 1")
        if sleep_sec < 0:
            raise ValueError("sleep_sec 必须 >= 0")
        self.max_lines: int = max_lines
        self.sleep_sec: float = sleep_sec

    # ------------------------------------------------------------------ #
    #  分片
    # ------------------------------------------------------------------ #
    async def split(self, text: str) -> list[str]:
        """将长文本按行数分片。

        分片策略：
          - 按 ``\\n`` 拆分为行；
          - 每 ``max_lines`` 行组成一片；
          - 若单片不足 max_lines 行则保持原样；
          - 空文本返回 ``[""]``。

        Args:
            text: 待分片的文本。

        Returns:
            分片后的文本列表。
        """
        if not text:
            return [""]

        lines = text.split("\n")
        chunks: list[str] = []
        for i in range(0, len(lines), self.max_lines):
            chunk_lines = lines[i : i + self.max_lines]
            chunks.append("\n".join(chunk_lines))

        logger.debug(
            f"消息分片: 原始 {len(lines)} 行 -> {len(chunks)} 片 "
            f"(每片最多 {self.max_lines} 行)"
        )
        return chunks

    def split_sync(self, text: str) -> list[str]:
        """同步分片接口（供非异步场景使用）。"""
        if not text:
            return [""]
        lines = text.split("\n")
        chunks: list[str] = []
        for i in range(0, len(lines), self.max_lines):
            chunk_lines = lines[i : i + self.max_lines]
            chunks.append("\n".join(chunk_lines))
        return chunks

    # ------------------------------------------------------------------ #
    #  分片发送
    # ------------------------------------------------------------------ #
    async def send_split(
        self,
        client: "WeChatHookInterface",
        wxid: str,
        text: str,
    ) -> list[str]:
        """分片发送消息，每片间隔 ``sleep_sec`` 秒。

        Args:
            client: 微信客户端接口（需实现 ``send_text``）。
            wxid: 接收者 wxid。
            text: 待发送的文本（可能超长）。

        Returns:
            每片发送成功后的消息 ID 列表（发送失败的消息 ID 为 None）。
        """
        chunks = await self.split(text)
        msg_ids: list[str] = []

        for i, chunk in enumerate(chunks):
            # 第一片不等待，后续片间间隔 sleep_sec 秒（防风控）
            if i > 0 and self.sleep_sec > 0:
                logger.debug(f"分片发送等待 {self.sleep_sec}s (第 {i+1}/{len(chunks)} 片)")
                await asyncio.sleep(self.sleep_sec)

            try:
                result = await client.send_text(wxid, chunk)
                if result.success and result.msg_id:
                    msg_ids.append(result.msg_id)
                else:
                    msg_ids.append("")
                    logger.warning(
                        f"分片 {i+1}/{len(chunks)} 发送失败: {result.error}"
                    )
            except Exception as exc:  # noqa: BLE001
                msg_ids.append("")
                logger.error(f"分片 {i+1}/{len(chunks)} 发送异常: {exc}")

        logger.info(
            f"分片发送完成: {len(msg_ids)} 片 -> {wxid} "
            f"(成功 {sum(1 for m in msg_ids if m)} 片)"
        )
        return msg_ids

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    def needs_split(self, text: str) -> bool:
        """判断文本是否需要分片（行数超过 max_lines）。"""
        if not text:
            return False
        return text.count("\n") + 1 > self.max_lines

    async def send(
        self,
        client: "WeChatHookInterface",
        wxid: str,
        text: str,
        *,
        force_split: bool = False,
    ) -> list[str]:
        """智能发送：需要分片时分片发送，否则直接发送。

        Args:
            client: 微信客户端接口。
            wxid: 接收者 wxid。
            text: 待发送文本。
            force_split: 是否强制分片（即使未超过行数也走分片流程）。

        Returns:
            消息 ID 列表。
        """
        if force_split or self.needs_split(text):
            return await self.send_split(client, wxid, text)
        # 无需分片，直接发送
        result = await client.send_text(wxid, text)
        return [result.msg_id or ""]
