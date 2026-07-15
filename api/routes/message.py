"""
消息 API 路由

端点:
  - POST /api/message/send-text   发送文本消息
  - POST /api/message/send-image  发送图片
  - POST /api/message/send-file   发送文件
  - GET  /api/message/history     获取消息历史
  - GET  /api/message/received    获取最近收到的消息
  - WS   /ws/message/{instance_id}  实时消息推送
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, Field

from api.deps import get_engine
from core.engine import CoreEngine
from core.websocket_manager import WebSocketManager, ws_manager as _global_ws

router = APIRouter(prefix="/api/message", tags=["消息"])


# ======================== 请求模型 ========================

class SendTextRequest(BaseModel):
    instance_id: str = Field(..., description="实例ID")
    wxid: str = Field(..., description="目标微信ID")
    text: str = Field(..., min_length=1, description="文本内容")


class SendImageRequest(BaseModel):
    instance_id: str
    wxid: str
    file_path: str = Field(..., description="图片路径/URL")
    text: str = ""


class SendFileRequest(BaseModel):
    instance_id: str
    wxid: str
    file_path: str
    file_name: str = ""


# ======================== 路由 ========================

@router.post("/send-text", summary="发送文本消息")
async def send_text(req: SendTextRequest, engine: CoreEngine = Depends(get_engine)) -> dict:
    try:
        return await engine.send_text(req.instance_id, req.wxid, req.text)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"发送文本消息异常: {exc}")
        return {"success": False, "error": f"内部错误: {exc}"}


@router.post("/send-image", summary="发送图片消息")
async def send_image(req: SendImageRequest, engine: CoreEngine = Depends(get_engine)) -> dict:
    try:
        return await engine.send_image(req.instance_id, req.wxid, req.file_path, req.text)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"发送图片消息异常: {exc}")
        return {"success": False, "error": f"内部错误: {exc}"}


@router.post("/send-file", summary="发送文件消息")
async def send_file(req: SendFileRequest, engine: CoreEngine = Depends(get_engine)) -> dict:
    try:
        return await engine.send_file(req.instance_id, req.wxid, req.file_path, req.file_name)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"发送文件消息异常: {exc}")
        return {"success": False, "error": f"内部错误: {exc}"}


@router.get("/history", summary="获取消息历史")
async def message_history(
    instance_id: str = Query(..., description="实例ID"),
    wxid: str = Query(..., description="对方微信ID"),
    limit: int = Query(50, ge=1, le=1000),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    rows = await engine.get_message_history(instance_id, wxid, limit)
    return {"success": True, "count": len(rows), "data": rows}


@router.get("/received", summary="获取最近收到的消息")
async def received_messages(
    limit: int = Query(100, ge=1, le=1000),
    direction: Optional[str] = Query(None, description="in/out, 默认全部"),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    rows = await engine.get_recent_messages(limit, direction)
    return {"success": True, "count": len(rows), "data": rows}


# ======================== WebSocket ========================

@router.websocket("/ws/message/{instance_id}")
async def message_ws(
    websocket: WebSocket,
    instance_id: str,
) -> None:
    """
    实时消息推送 WebSocket

    连接后接收该实例的所有消息事件 (in/out)
    """
    # WebSocket 路由无法使用 Depends(get_ws_manager) (缺少 HTTP Request),
    # 直接从 app.state 获取, 回退到全局单例
    ws_manager = getattr(websocket.app.state, "ws_manager", None) or _global_ws
    client = await ws_manager.connect(websocket, instance_id=instance_id)
    try:
        while True:
            # 接收客户端消息 (心跳/命令)
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            # 处理 ping 心跳
            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        logger.debug(f"WebSocket 客户端断开: {instance_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"WebSocket 异常: {exc}")
    finally:
        ws_manager.disconnect(client)
