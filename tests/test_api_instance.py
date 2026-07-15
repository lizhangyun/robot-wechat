"""
实例管理 API 测试

测试端点:
  - GET  /api/instance/list                             实例列表
  - POST /api/instance/create                           创建实例
  - POST /api/instance/{instance_id}/start              启动实例
  - POST /api/instance/{instance_id}/stop               停止实例
  - GET  /api/instance/{instance_id}/status             实例状态
  - PUT  /api/instance/{instance_id}/config             更新配置
  - GET  /api/instance/{instance_id}/bookkeeping/records 记账记录
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# GET /api/instance/list
# ---------------------------------------------------------------------------

def test_list_instances(app_client):
    """GET /api/instance/list - 初始状态返回空列表"""
    resp = app_client.get("/api/instance/list")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "count" in data
    assert "data" in data
    assert isinstance(data["data"], list)
    # 初始状态应无实例
    assert data["count"] == 0


def test_list_instances_after_create(app_client, create_instance):
    """GET /api/instance/list - 创建实例后列表包含该实例"""
    create_instance("inst-list-1", "实例一", "wxid_1")
    create_instance("inst-list-2", "实例二", "wxid_2")

    resp = app_client.get("/api/instance/list")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["count"] == 2

    # 验证列表中包含创建的实例 ID
    instance_ids = [item["instance_id"] for item in data["data"]]
    assert "inst-list-1" in instance_ids
    assert "inst-list-2" in instance_ids


# ---------------------------------------------------------------------------
# POST /api/instance/create
# ---------------------------------------------------------------------------

def test_create_instance(app_client):
    """POST /api/instance/create - 创建新实例"""
    resp = app_client.post("/api/instance/create", json={
        "instance_id": "test-create",
        "display_name": "测试创建实例",
        "wxid": "wxid_create",
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "data" in data

    instance = data["data"]
    assert instance["instance_id"] == "test-create"
    assert instance["display_name"] == "测试创建实例"
    assert instance["wxid"] == "wxid_create"
    assert instance["status"] == "stopped"
    assert instance["exists"] is True


def test_create_instance_with_config(app_client):
    """POST /api/instance/create - 创建实例并携带配置"""
    resp = app_client.post("/api/instance/create", json={
        "instance_id": "test-cfg-create",
        "display_name": "带配置的实例",
        "wxid": "wxid_cfg",
        "config": {
            "msg_split_enabled": False,
            "msg_max_lines": 50,
            "jizhang_enabled": False,
        },
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    config = data["data"]["config"]
    assert config["msg_split_enabled"] is False
    assert config["msg_max_lines"] == 50
    assert config["jizhang_enabled"] is False


# ---------------------------------------------------------------------------
# POST /api/instance/{instance_id}/start
# ---------------------------------------------------------------------------

def test_start_instance(app_client, create_instance):
    """POST /api/instance/{id}/start - 启动实例"""
    create_instance("test-start", "启动测试", "wxid_start")

    resp = app_client.post("/api/instance/test-start/start")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["data"]["status"] == "running"
    assert data["data"]["started_at"] is not None


def test_start_instance_idempotent(app_client, create_instance):
    """POST /api/instance/{id}/start - 重复启动返回当前状态"""
    create_instance("test-start-idem", "幂等启动", "wxid_idem")

    # 第一次启动
    resp1 = app_client.post("/api/instance/test-start-idem/start")
    assert resp1.json()["data"]["status"] == "running"

    # 第二次启动 (幂等, 返回 running 状态)
    resp2 = app_client.post("/api/instance/test-start-idem/start")
    assert resp2.status_code == 200
    assert resp2.json()["success"] is True
    assert resp2.json()["data"]["status"] == "running"


# ---------------------------------------------------------------------------
# POST /api/instance/{instance_id}/stop
# ---------------------------------------------------------------------------

def test_stop_instance(app_client, create_instance):
    """POST /api/instance/{id}/stop - 停止运行中的实例"""
    create_instance("test-stop", "停止测试", "wxid_stop")
    app_client.post("/api/instance/test-stop/start")

    resp = app_client.post("/api/instance/test-stop/stop")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["data"]["status"] == "stopped"
    assert data["data"]["started_at"] is None


def test_stop_not_running(app_client, create_instance):
    """POST /api/instance/{id}/stop - 停止未启动的实例"""
    create_instance("test-stop-idle", "未启动实例", "wxid_idle")

    resp = app_client.post("/api/instance/test-stop-idle/stop")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["data"]["status"] == "stopped"


# ---------------------------------------------------------------------------
# GET /api/instance/{instance_id}/status
# ---------------------------------------------------------------------------

def test_instance_status(app_client, create_instance):
    """GET /api/instance/{id}/status - 获取已创建实例的状态"""
    create_instance("test-status", "状态测试", "wxid_status")

    resp = app_client.get("/api/instance/test-status/status")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["data"]["instance_id"] == "test-status"
    assert data["data"]["status"] == "stopped"
    assert data["data"]["exists"] is True
    assert data["data"]["wxid"] == "wxid_status"


def test_instance_status_nonexistent(app_client):
    """GET /api/instance/{id}/status - 查询不存在的实例"""
    resp = app_client.get("/api/instance/nonexistent/status")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert data["data"]["status"] == "not_found"
    assert data["data"]["exists"] is False


# ---------------------------------------------------------------------------
# PUT /api/instance/{instance_id}/config
# ---------------------------------------------------------------------------

def test_update_config(app_client, create_instance):
    """PUT /api/instance/{id}/config - 更新实例配置"""
    create_instance("test-config", "配置测试", "wxid_config")

    resp = app_client.put("/api/instance/test-config/config", json={
        "config": {
            "display_name": "更新后的名称",
            "msg_split_enabled": False,
            "msg_max_lines": 100,
            "msg_sleep_sec": 2.5,
        }
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True

    instance = data["data"]
    # display_name 在顶层和 config 中都应更新
    assert instance["display_name"] == "更新后的名称"
    assert instance["config"]["display_name"] == "更新后的名称"
    assert instance["config"]["msg_split_enabled"] is False
    assert instance["config"]["msg_max_lines"] == 100
    assert instance["config"]["msg_sleep_sec"] == 2.5


def test_update_config_nonexistent(app_client):
    """PUT /api/instance/{id}/config - 更新不存在实例的配置"""
    resp = app_client.put("/api/instance/nonexistent/config", json={
        "config": {"display_name": "新名称"}
    })
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is False
    assert "error" in data


# ---------------------------------------------------------------------------
# GET /api/instance/{instance_id}/bookkeeping/records
# ---------------------------------------------------------------------------

def test_bookkeeping_records(app_client, create_instance):
    """GET /api/instance/{id}/bookkeeping/records - 获取记账记录"""
    create_instance("test-book", "记账测试", "wxid_book")

    resp = app_client.get("/api/instance/test-book/bookkeeping/records")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is True
    assert "count" in data
    assert "stats" in data
    assert "data" in data
    assert isinstance(data["data"], list)
    # 初始无记账记录
    assert data["count"] == 0
    # 统计字段应包含收支信息
    assert "income" in data["stats"]
    assert "expense" in data["stats"]
    assert "balance" in data["stats"]


def test_bookkeeping_records_with_limit(app_client, create_instance):
    """GET /api/instance/{id}/bookkeeping/records - limit 参数生效"""
    create_instance("test-book-limit", "记账限制", "wxid_bl")

    resp = app_client.get("/api/instance/test-book-limit/bookkeeping/records?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True


# ---------------------------------------------------------------------------
# 边界情况
# ---------------------------------------------------------------------------

def test_create_duplicate(app_client):
    """重复创建实例 - 当前实现为 upsert 行为 (更新而非报错)。

    引擎使用 INSERT ON CONFLICT DO UPDATE, 重复创建会更新已有实例。
    """
    # 第一次创建
    resp1 = app_client.post("/api/instance/create", json={
        "instance_id": "test-dup",
        "display_name": "原始名称",
        "wxid": "wxid_dup_1",
    })
    assert resp1.status_code == 200
    assert resp1.json()["success"] is True

    # 重复创建相同 ID (当前实现: upsert 更新, 返回成功)
    resp2 = app_client.post("/api/instance/create", json={
        "instance_id": "test-dup",
        "display_name": "更新名称",
        "wxid": "wxid_dup_2",
    })
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["success"] is True

    # 验证实例仍可访问且信息已更新
    status_resp = app_client.get("/api/instance/test-dup/status")
    assert status_resp.json()["success"] is True
    assert status_resp.json()["data"]["display_name"] == "更新名称"
    assert status_resp.json()["data"]["wxid"] == "wxid_dup_2"


def test_start_nonexistent(app_client):
    """POST /api/instance/{id}/start - 启动不存在的实例返回错误"""
    resp = app_client.post("/api/instance/nonexistent-instance/start")
    assert resp.status_code == 200

    data = resp.json()
    assert data["success"] is False
    assert "error" in data
    assert "不存在" in data["error"]
