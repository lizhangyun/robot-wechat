"""
WebSocket 连接管理器 - 管理实时消息推送连接

支持:
  - 按 instance_id 分组的连接管理
  - 广播消息给指定实例的所有连接
  - 全局广播
  - 心跳/连接存活检测
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import WebSocket
from loguru import logger


@dataclass(eq=False)
class ClientConnection:
    """单个 WebSocket 连接 (eq=False 以支持按身份哈希, 便于放入 set)"""
    ws: WebSocket
    instance_id: Optional[str]  # None 表示全局连接
    connected_at: float = field(default_factory=time.time)
    is_alive: bool = True

    async def send_json(self, data: Any) -> bool:
        """向该连接发送 JSON, 成功返回 True"""
        try:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False, default=str))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"发送 WebSocket 消息失败: {exc}")
            self.is_alive = False
            return False


class WebSocketManager:
    """WebSocket 连接管理器 (单例)"""

    def __init__(self) -> None:
        # instance_id -> set[ClientConnection]; 特殊键 None 表示全局
        self._connections: dict[Optional[str], set[ClientConnection]] = {}

    async def connect(self, ws: WebSocket, instance_id: Optional[str] = None) -> ClientConnection:
        """接受连接并注册"""
        await ws.accept()
        client = ClientConnection(ws=ws, instance_id=instance_id)
        self._connections.setdefault(instance_id, set()).add(client)
        logger.info(
            f"WebSocket 已连接 instance={instance_id or 'global'}, "
            f"当前连接数={self.total_connections()}"
        )
        return client

    def disconnect(self, client: ClientConnection) -> None:
        """移除连接"""
        bucket = self._connections.get(client.instance_id)
        if bucket:
            bucket.discard(client)
            if not bucket:
                self._connections.pop(client.instance_id, None)
        logger.info(f"WebSocket 已断开 instance={client.instance_id or 'global'}")

    async def broadcast_to_instance(self, instance_id: str, event: str, data: Any) -> int:
        """
        广播消息给指定实例的所有连接

        返回成功送达的连接数
        """
        payload = {"event": event, "instance_id": instance_id,
                   "data": data, "ts": time.time()}
        return await self._send_to(instance_id, payload)

    async def broadcast_global(self, event: str, data: Any) -> int:
        """广播给所有连接"""
        payload = {"event": event, "data": data, "ts": time.time()}
        sent = 0
        for key in list(self._connections.keys()):
            sent += await self._send_to(key, payload)
        return sent

    async def _send_to(self, instance_id: Optional[str], payload: dict) -> int:
        bucket = self._connections.get(instance_id, set())
        if not bucket:
            return 0
        dead: list[ClientConnection] = []
        sent = 0
        for client in list(bucket):
            ok = await client.send_json(payload)
            if ok:
                sent += 1
            else:
                dead.append(client)
        for client in dead:
            self.disconnect(client)
        return sent

    def total_connections(self) -> int:
        return sum(len(s) for s in self._connections.values())

    def instance_connections(self, instance_id: str) -> int:
        return len(self._connections.get(instance_id, set()))


# 全局单例
ws_manager = WebSocketManager()
