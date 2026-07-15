"""
实例管理 API 路由

端点:
  - GET  /api/instance/list                      实例列表
  - POST /api/instance/create                    创建实例
  - POST /api/instance/{instance_id}/start        启动实例
  - POST /api/instance/{instance_id}/stop        停止实例
  - GET  /api/instance/{instance_id}/status       实例状态
  - PUT  /api/instance/{instance_id}/config      更新配置
  - GET  /api/instance/{instance_id}/bookkeeping/records  记账记录
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Path, Query
from loguru import logger
from pydantic import BaseModel, Field

from api.deps import get_engine
from core.engine import CoreEngine

router = APIRouter(prefix="/api/instance", tags=["实例管理"])


class CreateInstanceRequest(BaseModel):
    instance_id: str = Field(..., description="实例ID, 如 c6801")
    display_name: str = ""
    wxid: str = ""
    config: Optional[dict[str, Any]] = Field(default_factory=dict, description="实例配置")


class UpdateConfigRequest(BaseModel):
    config: dict[str, Any] = Field(..., description="配置字段")


@router.get("/list", summary="实例列表")
async def instance_list(engine: CoreEngine = Depends(get_engine)) -> dict:
    rows = await engine.list_instances()
    return {"success": True, "count": len(rows), "data": rows}


@router.post("/create", summary="创建实例")
async def create_instance(
    req: CreateInstanceRequest,
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        return {"success": True, "data": await engine.create_instance(
            req.instance_id, req.display_name, req.wxid, req.config
        )}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"创建实例异常: {exc}")
        return {"success": False, "error": str(exc)}


@router.post("/{instance_id}/start", summary="启动实例")
async def start_instance(
    instance_id: str = Path(..., description="实例ID"),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        return {"success": True, "data": await engine.start_instance(instance_id)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"启动实例异常: {exc}")
        return {"success": False, "error": str(exc)}


@router.post("/{instance_id}/stop", summary="停止实例")
async def stop_instance(
    instance_id: str = Path(..., description="实例ID"),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        return {"success": True, "data": await engine.stop_instance(instance_id)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"停止实例异常: {exc}")
        return {"success": False, "error": str(exc)}


@router.get("/{instance_id}/status", summary="实例状态")
async def instance_status(
    instance_id: str = Path(..., description="实例ID"),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    return {"success": True, "data": await engine.get_instance_status(instance_id)}


@router.put("/{instance_id}/config", summary="更新实例配置")
async def update_config(
    req: UpdateConfigRequest,
    instance_id: str = Path(..., description="实例ID"),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        return {"success": True, "data": await engine.update_instance_config(instance_id, req.config)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"更新配置异常: {exc}")
        return {"success": False, "error": str(exc)}


@router.get("/{instance_id}/bookkeeping/records", summary="记账记录")
async def bookkeeping_records(
    instance_id: str = Path(..., description="实例ID"),
    limit: int = Query(100, ge=1, le=1000),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    rows = await engine.list_bookkeeping(instance_id, limit)
    stats = await engine.bookkeeping_stats(instance_id)
    return {"success": True, "count": len(rows), "stats": stats, "data": rows}
