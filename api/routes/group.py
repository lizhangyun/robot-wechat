"""
群管理 API 路由

端点:
  - GET  /api/group/list                           群列表
  - GET  /api/group/{group_wxid}/members           群成员
  - POST /api/group/send-announcement               发送群公告
  - GET  /api/group/{group_wxid}/stats              群统计
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from loguru import logger
from pydantic import BaseModel

from api.deps import get_engine
from core.engine import CoreEngine

router = APIRouter(prefix="/api/group", tags=["群管理"])


class AnnouncementRequest(BaseModel):
    instance_id: str
    group_wxid: str
    announcement: str


@router.get("/list", summary="群列表")
async def group_list(
    instance_id: str = Query(..., description="实例ID"),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    rows = await engine.list_groups(instance_id)
    return {"success": True, "count": len(rows), "data": rows}


@router.get("/{group_wxid}/members", summary="群成员")
async def group_members(
    group_wxid: str,
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    rows = await engine.list_group_members(group_wxid)
    return {"success": True, "count": len(rows), "data": rows}


@router.post("/send-announcement", summary="发送群公告")
async def send_announcement(
    req: AnnouncementRequest,
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        return await engine.send_group_announcement(req.instance_id, req.group_wxid,
                                                     req.announcement)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"发送群公告异常: {exc}")
        return {"success": False, "error": str(exc)}


@router.get("/{group_wxid}/stats", summary="群统计")
async def group_stats(
    group_wxid: str,
    instance_id: str = Query(..., description="实例ID"),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        data = await engine.group_stats(instance_id, group_wxid)
        return {"success": True, "data": data}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"群统计异常: {exc}")
        return {"success": False, "error": str(exc)}
