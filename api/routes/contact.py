"""
联系人 API 路由

端点:
  - GET /api/contact/list      联系人列表
  - GET /api/contact/search    搜索联系人
  - PUT /api/contact/remark    修改备注
  - POST /api/contact/sync     同步联系人
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from loguru import logger
from pydantic import BaseModel, Field

from api.deps import get_engine
from core.engine import CoreEngine

router = APIRouter(prefix="/api/contact", tags=["联系人"])


class UpdateRemarkRequest(BaseModel):
    instance_id: str
    wxid: str
    remark: str = Field(..., description="新备注")


class SyncRequest(BaseModel):
    instance_id: str


@router.get("/list", summary="联系人列表")
async def contact_list(
    instance_id: str = Query(..., description="实例ID"),
    limit: int = Query(500, ge=1, le=5000),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    rows = await engine.list_contacts(instance_id, limit)
    return {"success": True, "count": len(rows), "data": rows}


@router.get("/search", summary="搜索联系人")
async def contact_search(
    instance_id: str = Query(...),
    keyword: str = Query(..., min_length=1, description="搜索关键词"),
    limit: int = Query(50, ge=1, le=500),
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    rows = await engine.search_contacts(instance_id, keyword, limit)
    return {"success": True, "count": len(rows), "data": rows}


@router.put("/remark", summary="修改备注")
async def update_remark(
    req: UpdateRemarkRequest,
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        ok = await engine.update_contact_remark(req.instance_id, req.wxid, req.remark)
        return {"success": ok, "updated": ok}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"修改备注异常: {exc}")
        return {"success": False, "error": str(exc)}


@router.post("/sync", summary="同步联系人")
async def sync_contacts(
    req: SyncRequest,
    engine: CoreEngine = Depends(get_engine),
) -> dict:
    try:
        return await engine.sync_contacts(req.instance_id)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"同步联系人异常: {exc}")
        return {"success": False, "error": str(exc)}
