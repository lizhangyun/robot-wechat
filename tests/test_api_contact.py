"""
联系人 API 测试

测试端点:
  - GET  /api/contact/list     联系人列表
  - GET  /api/contact/search   搜索联系人
  - PUT  /api/contact/remark   修改备注
  - POST /api/contact/sync     同步联系人
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# GET /api/contact/list
# ---------------------------------------------------------------------------

def test_contact_list(app_client, create_instance):
    """GET /api/contact/list - 同步后返回联系人列表"""
    create_instance("test-contact", "联系人测试", "wxid_contact")

    # mock 模式同步联系人以生成示例数据
    app_client.post("/api/contact/sync", json={"instance_id": "test-contact"})

    resp = app_client.get("/api/contact/list", params={"instance_id": "test-contact"})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "count" in data
    assert "data" in data
    # mock 同步后应有联系人数据
    assert data["count"] > 0

    # 验证联系人字段结构
    if data["data"]:
        contact = data["data"][0]
        assert "instance_id" in contact
        assert "wxid" in contact
        assert "nickname" in contact


def test_contact_list_empty(app_client, create_instance):
    """GET /api/contact/list - 未同步时返回空列表"""
    create_instance("test-contact-empty", "空联系人", "wxid_ce")

    resp = app_client.get("/api/contact/list", params={"instance_id": "test-contact-empty"})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 0
    assert data["data"] == []


def test_contact_list_empty_instance(app_client):
    """GET /api/contact/list - 不存在的实例返回空列表"""
    resp = app_client.get("/api/contact/list", params={"instance_id": "nonexistent"})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 0
    assert data["data"] == []


# ---------------------------------------------------------------------------
# GET /api/contact/search
# ---------------------------------------------------------------------------

def test_contact_search(app_client, create_instance):
    """GET /api/contact/search - 搜索联系人 (按昵称)"""
    create_instance("test-search", "搜索测试", "wxid_search")

    # 先同步联系人 (mock 模式会生成 "张三", "李四", "王五" 等示例)
    app_client.post("/api/contact/sync", json={"instance_id": "test-search"})

    # 搜索 "张" (应匹配 "张三")
    resp = app_client.get("/api/contact/search", params={
        "instance_id": "test-search",
        "keyword": "张",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] >= 1

    # 验证搜索结果包含 "张三"
    nicknames = [c["nickname"] for c in data["data"]]
    assert "张三" in nicknames


def test_contact_search_no_result(app_client, create_instance):
    """GET /api/contact/search - 搜索无结果"""
    create_instance("test-search-none", "无结果搜索", "wxid_sn")

    app_client.post("/api/contact/sync", json={"instance_id": "test-search-none"})

    resp = app_client.get("/api/contact/search", params={
        "instance_id": "test-search-none",
        "keyword": "不存在的联系人xyz",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 0
    assert data["data"] == []


def test_contact_search_by_wxid(app_client, create_instance):
    """GET /api/contact/search - 按 wxid 搜索"""
    create_instance("test-search-wxid", "wxid搜索", "wxid_sw")

    app_client.post("/api/contact/sync", json={"instance_id": "test-search-wxid"})

    # 搜索 wxid_sample1 (mock 数据中的示例 wxid)
    resp = app_client.get("/api/contact/search", params={
        "instance_id": "test-search-wxid",
        "keyword": "wxid_sample1",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] >= 1
    assert data["data"][0]["wxid"] == "wxid_sample1"


# ---------------------------------------------------------------------------
# POST /api/contact/sync
# ---------------------------------------------------------------------------

def test_contact_sync(app_client, create_instance):
    """POST /api/contact/sync - 同步联系人 (mock 模式生成示例数据)"""
    create_instance("test-sync", "同步测试", "wxid_sync")

    resp = app_client.post("/api/contact/sync", json={"instance_id": "test-sync"})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["synced"] is True

    # 验证同步后联系人列表有数据
    list_resp = app_client.get("/api/contact/list", params={"instance_id": "test-sync"})
    list_data = list_resp.json()
    assert list_data["count"] > 0


def test_contact_sync_nonexistent(app_client):
    """POST /api/contact/sync - 同步不存在的实例返回错误"""
    resp = app_client.post("/api/contact/sync", json={"instance_id": "nonexistent"})
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is False
    assert "error" in data


# ---------------------------------------------------------------------------
# PUT /api/contact/remark
# ---------------------------------------------------------------------------

def test_contact_remark(app_client, create_instance):
    """PUT /api/contact/remark - 修改联系人备注"""
    create_instance("test-remark", "备注测试", "wxid_remark")

    # 先同步联系人
    app_client.post("/api/contact/sync", json={"instance_id": "test-remark"})

    # 修改备注
    resp = app_client.put("/api/contact/remark", json={
        "instance_id": "test-remark",
        "wxid": "wxid_sample1",
        "remark": "这是新备注",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["updated"] is True

    # 验证备注已更新 (通过列表查看)
    list_resp = app_client.get("/api/contact/list", params={"instance_id": "test-remark"})
    contacts = list_resp.json()["data"]
    target = [c for c in contacts if c["wxid"] == "wxid_sample1"]
    assert len(target) == 1
    assert target[0]["remark"] == "这是新备注"


def test_contact_remark_nonexistent(app_client, create_instance):
    """PUT /api/contact/remark - 修改不存在联系人的备注"""
    create_instance("test-remark-none", "备注不存在", "wxid_rn")

    resp = app_client.put("/api/contact/remark", json={
        "instance_id": "test-remark-none",
        "wxid": "nonexistent_wxid",
        "remark": "备注",
    })
    assert resp.status_code == 200

    data = resp.json()
    # 联系人不存在, 更新行数为 0
    assert data["success"] is False
    assert data["updated"] is False
