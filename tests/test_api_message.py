"""
消息 API 测试

测试端点:
  - POST /api/message/send-text    发送文本消息
  - POST /api/message/send-image   发送图片消息
  - POST /api/message/send-file    发送文件消息
  - GET  /api/message/history      获取消息历史
  - GET  /api/message/received     获取最近收到的消息
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# POST /api/message/send-text
# ---------------------------------------------------------------------------

def test_send_text(app_client, create_instance):
    """POST /api/message/send-text - 发送文本消息"""
    create_instance("test-msg", "消息测试", "wxid_msg")

    resp = app_client.post("/api/message/send-text", json={
        "instance_id": "test-msg",
        "wxid": "target_wxid",
        "text": "你好, 这是一条测试消息",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "message_id" in data
    assert isinstance(data["message_id"], int)
    assert data["message_id"] > 0
    # mock 模式标志
    assert data["mock"] is True


def test_send_text_no_instance(app_client):
    """POST /api/message/send-text - 缺少 instance_id 字段触发验证错误"""
    resp = app_client.post("/api/message/send-text", json={
        "wxid": "target_wxid",
        "text": "测试消息",
    })
    # Pydantic 校验失败返回 422
    assert resp.status_code == 422


def test_send_text_nonexistent_instance(app_client):
    """POST /api/message/send-text - 向不存在的实例发送消息"""
    resp = app_client.post("/api/message/send-text", json={
        "instance_id": "nonexistent",
        "wxid": "target_wxid",
        "text": "测试消息",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is False
    assert "error" in data
    assert "不存在" in data["error"]


def test_send_text_empty_text(app_client, create_instance):
    """POST /api/message/send-text - 空文本触发验证错误 (min_length=1)"""
    create_instance("test-empty", "空文本测试", "wxid_empty")

    resp = app_client.post("/api/message/send-text", json={
        "instance_id": "test-empty",
        "wxid": "target_wxid",
        "text": "",
    })
    # text 字段有 min_length=1 约束, 空字符串返回 422
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/message/send-image
# ---------------------------------------------------------------------------

def test_send_image(app_client, create_instance):
    """POST /api/message/send-image - 发送图片消息"""
    create_instance("test-img", "图片测试", "wxid_img")

    resp = app_client.post("/api/message/send-image", json={
        "instance_id": "test-img",
        "wxid": "target_wxid",
        "file_path": "/tmp/test_image.png",
        "text": "看这张图片",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "message_id" in data
    assert data["message_id"] > 0
    assert data["mock"] is True


def test_send_image_without_text(app_client, create_instance):
    """POST /api/message/send-image - 不附带文字说明"""
    create_instance("test-img-notext", "纯图片", "wxid_img2")

    resp = app_client.post("/api/message/send-image", json={
        "instance_id": "test-img-notext",
        "wxid": "target_wxid",
        "file_path": "/tmp/photo.jpg",
    })
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ---------------------------------------------------------------------------
# POST /api/message/send-file
# ---------------------------------------------------------------------------

def test_send_file(app_client, create_instance):
    """POST /api/message/send-file - 发送文件消息"""
    create_instance("test-file", "文件测试", "wxid_file")

    resp = app_client.post("/api/message/send-file", json={
        "instance_id": "test-file",
        "wxid": "target_wxid",
        "file_path": "/tmp/report.pdf",
        "file_name": "月度报告.pdf",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "message_id" in data
    assert data["message_id"] > 0
    assert data["mock"] is True


def test_send_file_without_name(app_client, create_instance):
    """POST /api/message/send-file - 不指定文件名"""
    create_instance("test-file-noname", "无名文件", "wxid_file2")

    resp = app_client.post("/api/message/send-file", json={
        "instance_id": "test-file-noname",
        "wxid": "target_wxid",
        "file_path": "/tmp/data.csv",
    })
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ---------------------------------------------------------------------------
# GET /api/message/history
# ---------------------------------------------------------------------------

def test_message_history(app_client, create_instance):
    """GET /api/message/history - 获取消息历史记录"""
    create_instance("test-hist", "历史测试", "wxid_hist")

    # 发送多条消息
    for i in range(3):
        app_client.post("/api/message/send-text", json={
            "instance_id": "test-hist",
            "wxid": "friend_wxid",
            "text": f"历史消息 {i}",
        })

    resp = app_client.get("/api/message/history", params={
        "instance_id": "test-hist",
        "wxid": "friend_wxid",
        "limit": 10,
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 3
    assert len(data["data"]) == 3

    # 验证消息内容
    contents = [msg["content"] for msg in data["data"]]
    assert "历史消息 0" in contents
    assert "历史消息 1" in contents
    assert "历史消息 2" in contents

    # 验证消息方向 (发送的消息为 out)
    for msg in data["data"]:
        assert msg["direction"] == "out"
        assert msg["msg_type"] == "text"


def test_message_history_empty(app_client, create_instance):
    """GET /api/message/history - 无消息记录时返回空列表"""
    create_instance("test-hist-empty", "空历史", "wxid_hist_e")

    resp = app_client.get("/api/message/history", params={
        "instance_id": "test-hist-empty",
        "wxid": "anyone",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 0
    assert data["data"] == []


def test_message_history_with_limit(app_client, create_instance):
    """GET /api/message/history - limit 参数限制返回数量"""
    create_instance("test-hist-limit", "限制历史", "wxid_hl")

    # 发送5条消息
    for i in range(5):
        app_client.post("/api/message/send-text", json={
            "instance_id": "test-hist-limit",
            "wxid": "friend",
            "text": f"消息 {i}",
        })

    # 限制返回2条
    resp = app_client.get("/api/message/history", params={
        "instance_id": "test-hist-limit",
        "wxid": "friend",
        "limit": 2,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert len(data["data"]) == 2


# ---------------------------------------------------------------------------
# GET /api/message/received
# ---------------------------------------------------------------------------

def test_message_received(app_client, create_instance):
    """GET /api/message/received - 获取最近收到的消息"""
    create_instance("test-recv", "接收测试", "wxid_recv")

    # 发送几条消息
    for i in range(3):
        app_client.post("/api/message/send-text", json={
            "instance_id": "test-recv",
            "wxid": f"friend_{i}",
            "text": f"消息 {i}",
        })

    resp = app_client.get("/api/message/received", params={"limit": 10})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] >= 3
    assert isinstance(data["data"], list)


def test_message_received_with_direction(app_client, create_instance):
    """GET /api/message/received - 按 direction 过滤 (仅出站)"""
    create_instance("test-recv-dir", "方向测试", "wxid_rd")

    app_client.post("/api/message/send-text", json={
        "instance_id": "test-recv-dir",
        "wxid": "friend",
        "text": "出站消息",
    })

    resp = app_client.get("/api/message/received", params={
        "limit": 10,
        "direction": "out",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] >= 1
    # 所有返回的消息方向都应为 out
    for msg in data["data"]:
        assert msg["direction"] == "out"


# ---------------------------------------------------------------------------
# 超长文本
# ---------------------------------------------------------------------------

def test_send_long_text(app_client, create_instance):
    """发送超长文本 - 验证 API 接受长文本并完整记录到历史。

    注意: 实际的消息分片逻辑在 MessagePipeline.send() 中实现,
    API 层的 send_text 会完整存储文本, 不做分片。
    """
    create_instance("test-long", "长文本测试", "wxid_long")

    # 生成超长文本 (200行, 远超默认 msg_max_lines=70)
    long_text = "\n".join([f"第 {i + 1} 行: 这是一段很长的测试内容, 用于验证超长消息处理" for i in range(200)])

    resp = app_client.post("/api/message/send-text", json={
        "instance_id": "test-long",
        "wxid": "target_wxid",
        "text": long_text,
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "message_id" in data
    assert data["message_id"] > 0

    # 验证消息历史中记录了完整文本 (未截断)
    hist_resp = app_client.get("/api/message/history", params={
        "instance_id": "test-long",
        "wxid": "target_wxid",
        "limit": 1,
    })
    assert hist_resp.status_code == 200

    hist_data = hist_resp.json()
    assert hist_data["count"] == 1
    assert hist_data["data"][0]["content"] == long_text
    # 验证文本长度未变化
    assert len(hist_data["data"][0]["content"]) == len(long_text)
