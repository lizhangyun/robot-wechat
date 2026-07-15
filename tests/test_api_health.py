"""
健康检查和仪表盘 API 测试

测试端点:
  - GET /api/health       健康检查
  - GET /api/dashboard    仪表盘统计
  - GET /                 根路径应用信息
  - GET /docs             API 文档 (Swagger UI)
  - GET /web/index.html   Web 管理界面
"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------

def test_health_endpoint(app_client):
    """GET /api/health - 返回200和正确的健康状态字段"""
    resp = app_client.get("/api/health")
    assert resp.status_code == 200

    data = resp.json()
    # 验证必需字段
    assert data["status"] == "ok"
    assert "app" in data
    assert "version" in data
    # mock 模式下 mock 字段应为 True
    assert data["mock"] is True


# ---------------------------------------------------------------------------
# GET /api/dashboard
# ---------------------------------------------------------------------------

def test_dashboard_endpoint(app_client):
    """GET /api/dashboard - 返回仪表盘统计数据"""
    resp = app_client.get("/api/dashboard")
    assert resp.status_code == 200

    data = resp.json()
    # 验证统计字段存在
    assert "instance_total" in data
    assert "instance_running" in data
    assert "today_messages" in data
    assert "ws_connections" in data
    assert "mock_mode" in data
    assert "license_status" in data
    assert "mq_dead_letter" in data

    # mock 模式标志应为 True
    assert data["mock_mode"] is True
    # 初始状态: 无实例, 无运行中实例
    assert data["instance_total"] == 0
    assert data["instance_running"] == 0
    assert isinstance(data["today_messages"], int)
    assert isinstance(data["ws_connections"], int)


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------

def test_root_endpoint(app_client):
    """GET / - 返回应用基本信息"""
    resp = app_client.get("/")
    assert resp.status_code == 200

    data = resp.json()
    # 根路径返回应用运行状态和可用端点
    assert "message" in data
    assert "docs" in data
    assert "health" in data
    # web 目录存在时应包含 web 字段
    assert "web" in data


# ---------------------------------------------------------------------------
# GET /docs
# ---------------------------------------------------------------------------

def test_docs_endpoint(app_client):
    """GET /docs - 返回Swagger API文档页面"""
    resp = app_client.get("/docs")
    assert resp.status_code == 200
    # Swagger UI 返回 HTML 内容
    assert "text/html" in resp.headers.get("content-type", "")
    assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()


# ---------------------------------------------------------------------------
# GET /web/index.html
# ---------------------------------------------------------------------------

def test_web_ui(app_client):
    """GET /web/index.html - 返回Web管理界面"""
    resp = app_client.get("/web/index.html")
    assert resp.status_code == 200
    # 静态 HTML 文件
    assert "text/html" in resp.headers.get("content-type", "")
    # 内容不应为空
    assert len(resp.text) > 0
