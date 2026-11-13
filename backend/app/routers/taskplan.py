"""采集任务编排接口：启动计划前读取合规限制，读不到就拒绝启动并给出补录项。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.taskplan import TaskplanService

router = APIRouter(prefix="/api/taskplan", tags=["采集任务编排"])

service = TaskplanService()

LIST_FIELDS = ["计划编号", "目标站点", "采集频率", "数据范围", "优先级", "资源配额", "预计用时", "计划状态"]
STATUSES = ["草稿", "运行中", "已暂停", "已完成"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集任务编排清单：返回当前全部数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "taskplan", "total": total, "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按计划编号与状态过滤采集任务编排列表；没有数据时返回空页，不报错。"""
    if page < 1:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")
    if size < 1 or size > 200:
        raise HTTPException(status_code=400, detail="每页条数需在 1 到 200 之间，请调整分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}/limits", response_model=dict)
def get_limits(entry_id: int) -> dict[str, Any]:
    """读取采集计划实际会使用的合规限制；排班端据此判断能否启动。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集计划 {entry_id} 不存在或已归档")
    limits, message, supplement = service.check_limits(entry)
    if limits is None:
        raise HTTPException(status_code=409, detail=f"{message}；待补录项：{'、'.join(supplement)}")
    return {"entry_id": entry_id, "limits": limits, "supplement": []}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集计划明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集计划 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采集计划，缺字段时说明原因而不是静默丢弃。"""
    entry, message = service.create_entry(payload.values)
    if entry is None:
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message="采集计划已登记，当前为草稿", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, response: Response) -> ActionResult:
    """执行启动、暂停、归档；启动前必须先读到合规限制。"""
    action = str(payload.values.get("action") or payload.action or "").strip()
    values = dict(payload.values)
    values.pop("action", None)
    entry, message, supplement = service.run_action(entry_id, action, values)
    payload_out = {"待补录项": supplement} if supplement else None
    if entry is None:
        # 拒绝同样带可读原因；有缺口时把待补录项结构化返回给排班端。
        response.status_code = 409
        return ActionResult(ok=False, message=message, supplement=payload_out)
    return ActionResult(ok=True, message=message, entry=entry, supplement=payload_out)
