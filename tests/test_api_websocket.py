"""
WebSocket API 测试

测试端点:
  - WS /ws                       全局 WebSocket (所有实例消息推送)
  - WS /ws/message/{instance_id} 实例特定 WebSocket (该实例消息推送)

使用 TestClient 的 websocket_connect 进行同步 WebSocket 测试。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# WS /ws - 全局 WebSocket
# ---------------------------------------------------------------------------

def test_ws_connect(app_client):
    """WS /ws - 全局 WebSocket 连接建立"""
    with app_client.websocket_connect("/ws") as ws:
        # 连接成功后发送 ping 验证连通性
        ws.send_text(json.dumps({"type": "ping"}))
        resp = ws.receive_text()
        data = json.loads(resp)
        assert data["type"] == "pong"


def test_ws_ping_pong(app_client):
    """WS /ws - 心跳 ping/pong 多次往返"""
    with app_client.websocket_connect("/ws") as ws:
        # 连续进行 3 次心跳
        for i in range(3):
            ws.send_text(json.dumps({"type": "ping"}))
            resp = ws.receive_text()
            data = json.loads(resp)
            assert data["type"] == "pong"


def test_ws_invalid_json(app_client):
    """WS /ws - 发送非法 JSON 不影响连接 (服务端静默忽略)"""
    with app_client.websocket_connect("/ws") as ws:
        # 发送非法 JSON, 服务端应忽略而非断开
        ws.send_text("not a json string")
        # 再发送正常 ping, 验证连接仍可用
        ws.send_text(json.dumps({"type": "ping"}))
        resp = ws.receive_text()
        data = json.loads(resp)
        assert data["type"] == "pong"


# ---------------------------------------------------------------------------
# WS /ws - 消息广播
# ---------------------------------------------------------------------------

def test_ws_message_broadcast(app_client, create_instance):
    """WS /ws - 实例启动事件通过全局 WebSocket 广播

    启动实例时, 引擎调用 ws_manager.broadcast_global("instance.started", ...)
    全局 WebSocket 连接应收到该广播事件。
    """
    create_instance("test-ws-bcast", "广播测试", "wxid_bcast")

    with app_client.websocket_connect("/ws") as ws:
        # 先发送 ping 确认连接正常
        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["type"] == "pong"

        # 启动实例, 触发 broadcast_global("instance.started", ...)
        app_client.post("/api/instance/test-ws-bcast/start")

        # 接收广播消息
        msg = ws.receive_json()
        assert msg["event"] == "instance.started"
        assert msg["data"]["instance_id"] == "test-ws-bcast"
        assert "ts" in msg


def test_ws_message_broadcast_via_send_text(app_client, create_instance):
    """WS /ws - 发送文本消息后通过 WebSocket 接收消息广播

    发送文本消息时, 引擎通过消息队列发布, 回调函数调用
    ws_manager.broadcast_global("message", payload) 广播到所有全局连接。
    """
    create_instance("test-ws-msg", "消息广播测试", "wxid_wm")

    with app_client.websocket_connect("/ws") as ws:
        # 消耗初始 pong (确认连接)
        ws.send_json({"type": "ping"})
        ws.receive_json()  # pong

        # 通过 API 发送文本消息
        app_client.post("/api/message/send-text", json={
            "instance_id": "test-ws-msg",
            "wxid": "target_wxid",
            "text": "WebSocket广播测试",
        })

        # 接收消息广播 (消息队列异步分发, receive_json 会阻塞直到收到)
        msg = ws.receive_json()
        assert msg["event"] == "message"
        assert msg["data"]["content"] == "WebSocket广播测试"
        assert msg["data"]["instance_id"] == "test-ws-msg"
        assert msg["data"]["direction"] == "out"
        assert msg["data"]["type"] == "text"


# ---------------------------------------------------------------------------
# WS /ws/message/{instance_id} - 实例特定 WebSocket
# ---------------------------------------------------------------------------

def test_ws_instance_specific(app_client, create_instance):
    """WS /api/message/ws/message/{instance_id} - 实例特定 WebSocket 连接

    注意: 该 WebSocket 定义在 message 路由 (prefix=/api/message) 上,
    实际路径为 /api/message/ws/message/{instance_id}。
    """
    create_instance("test-ws-inst", "实例WS测试", "wxid_ws_inst")

    with app_client.websocket_connect("/api/message/ws/message/test-ws-inst") as ws:
        # 验证连接成功, 发送 ping/pong
        ws.send_text(json.dumps({"type": "ping"}))
        resp = ws.receive_text()
        data = json.loads(resp)
        assert data["type"] == "pong"


def test_ws_instance_specific_message(app_client, create_instance):
    """WS /api/message/ws/message/{instance_id} - 实例特定 WebSocket 接收该实例的消息

    发送文本消息时, 引擎通过 broadcast_to_instance(instance_id, ...) 推送
    到该实例的 WebSocket 连接。
    """
    create_instance("test-ws-inst-msg", "实例消息WS", "wxid_wim")

    with app_client.websocket_connect("/api/message/ws/message/test-ws-inst-msg") as ws:
        # 确认连接
        ws.send_json({"type": "ping"})
        ws.receive_json()  # pong

        # 发送文本消息到该实例
        app_client.post("/api/message/send-text", json={
            "instance_id": "test-ws-inst-msg",
            "wxid": "friend_wxid",
            "text": "实例特定推送测试",
        })

        # 接收该实例的消息推送
        msg = ws.receive_json()
        assert msg["event"] == "message.out"
        assert msg["instance_id"] == "test-ws-inst-msg"
        assert msg["data"]["content"] == "实例特定推送测试"
        assert msg["data"]["instance_id"] == "test-ws-inst-msg"


def test_ws_instance_ping_pong(app_client, create_instance):
    """WS /api/message/ws/message/{instance_id} - 实例 WebSocket 心跳"""
    create_instance("test-ws-heartbeat", "心跳测试", "wxid_hb")

    with app_client.websocket_connect("/api/message/ws/message/test-ws-heartbeat") as ws:
        for i in range(3):
            ws.send_json({"type": "ping"})
            resp = ws.receive_json()
            assert resp["type"] == "pong"
