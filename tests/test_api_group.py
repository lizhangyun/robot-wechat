"""
群管理 API 测试

测试端点:
  - GET  /api/group/list                       群列表
  - GET  /api/group/{group_wxid}/members       群成员
  - POST /api/group/send-announcement           发送群公告
  - GET  /api/group/{group_wxid}/stats          群统计
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# GET /api/group/list
# ---------------------------------------------------------------------------

def test_group_list(app_client, create_instance):
    """GET /api/group/list - 获取群列表"""
    create_instance("test-group", "群测试", "wxid_group")

    resp = app_client.get("/api/group/list", params={"instance_id": "test-group"})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "count" in data
    assert "data" in data
    assert isinstance(data["data"], list)
    # 初始无群数据
    assert data["count"] == 0


def test_group_list_nonexistent(app_client):
    """GET /api/group/list - 不存在的实例返回空列表"""
    resp = app_client.get("/api/group/list", params={"instance_id": "nonexistent"})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 0
    assert data["data"] == []


# ---------------------------------------------------------------------------
# GET /api/group/{group_wxid}/members
# ---------------------------------------------------------------------------

def test_group_members(app_client, create_instance):
    """GET /api/group/{group_wxid}/members - 获取群成员列表"""
    create_instance("test-members", "群成员测试", "wxid_members")

    resp = app_client.get("/api/group/test_group_001/members")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "count" in data
    assert "data" in data
    assert isinstance(data["data"], list)
    # 初始无群成员
    assert data["count"] == 0


# ---------------------------------------------------------------------------
# GET /api/group/{group_wxid}/stats
# ---------------------------------------------------------------------------

def test_group_stats(app_client, create_instance):
    """GET /api/group/{group_wxid}/stats - 获取群统计信息"""
    create_instance("test-stats", "群统计测试", "wxid_stats")

    resp = app_client.get("/api/group/test_group_001/stats", params={
        "instance_id": "test-stats",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "data" in data
    # 统计数据包含群信息和成员数
    assert "member_count" in data["data"]
    assert "members" in data["data"]
    assert "group" in data["data"]


# ---------------------------------------------------------------------------
# POST /api/group/send-announcement
# ---------------------------------------------------------------------------

def test_group_announcement(app_client, create_instance):
    """POST /api/group/send-announcement - 发送群公告"""
    create_instance("test-ann", "群公告测试", "wxid_ann")

    resp = app_client.post("/api/group/send-announcement", json={
        "instance_id": "test-ann",
        "group_wxid": "test_group_001",
        "announcement": "这是群公告内容, 请大家注意查看",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True


def test_group_announcement_nonexistent_instance(app_client):
    """POST /api/group/send-announcement - 不存在的实例返回错误"""
    resp = app_client.post("/api/group/send-announcement", json={
        "instance_id": "nonexistent",
        "group_wxid": "test_group_001",
        "announcement": "公告内容",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is False
    assert "error" in data


def test_group_announcement_missing_fields(app_client):
    """POST /api/group/send-announcement - 缺少必填字段触发验证错误"""
    resp = app_client.post("/api/group/send-announcement", json={
        "instance_id": "test",
        # 缺少 group_wxid 和 announcement
    })
    assert resp.status_code == 422
