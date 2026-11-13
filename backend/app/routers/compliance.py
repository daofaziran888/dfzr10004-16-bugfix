"""采集合规配置接口：登记、审核、发布、撤销与计划读取限制。

路由顺序刻意把 ``/export``、``/limits`` 这类静态路径放在 ``/{entry_id}``
之前，否则 ``GET /api/compliance/export`` 会先命中详情路由，被当成非法的
条目编号而返回 422，页面上的导出/读取按钮就会表现为“点不动”。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.compliance import ComplianceService, is_conflict

router = APIRouter(prefix="/api/compliance", tags=["采集合规配置"])

service = ComplianceService()

LIST_FIELDS = ["规则编号", "站点域名", "协议标识", "访问间隔", "并发上限", "请求头模板", "合规状态", "最近审查"]
STATUSES = ["草稿", "待审批", "已生效", "已废弃"]


def _message_from_error(detail: Any) -> str:
    return str(detail) if detail is not None else "采集合规配置操作失败"


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集合规配置清单：返回当前全部数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "compliance", "total": total, "items": items}


@router.get("/limits/{entry_id}", response_model=dict)
def get_limits(entry_id: int) -> dict[str, Any]:
    """采集计划读取合规限制：只能读已生效规则的审批版本快照。"""
    limits, message = service.get_limits(entry_id)
    if limits is None:
        raise HTTPException(status_code=409, detail=message)
    return limits


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、待审批、已生效、已废弃"),
    site: str | None = Query(default=None, description="按站点域名检索"),
    protocol: str | None = Query(default=None, description="按协议标识检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号、站点、协议与状态过滤采集合规配置列表；没有数据时返回空页。"""
    if page < 1:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")
    if size < 1 or size > 200:
        raise HTTPException(status_code=400, detail="每页条数需在 1 到 200 之间，请调整分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, site=site, protocol=protocol, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}/history", response_model=dict)
def get_history(entry_id: int) -> dict[str, Any]:
    """读取审批与发布历史：撤回、重复提交等动作都留痕。"""
    history, message = service.history(entry_id)
    if history is None:
        raise HTTPException(status_code=404, detail=message)
    return {"entry_id": entry_id, "history": history}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条合规规则明细，与列表使用同一份状态与限制字段。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"合规规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条合规规则，访问间隔、并发上限、请求头模板一并入库。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"缺少必填字段：{'、'.join(missing)}；规则未登记，请补齐后重试",
        )
    return ActionResult(ok=True, message="合规规则已登记，当前为草稿", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改尚未生效的合规规则；已生效规则需先撤销。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        raise HTTPException(status_code=409, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交审批、审核通过、发布规则、撤回审核、废弃规则。

    拒绝一律返回可读原因；发布冲突返回 409，后到的并发请求不会产生第二次写入。
    """
    action = str(payload.values.get("action") or payload.action or "").strip()
    values = dict(payload.values)
    values.pop("action", None)
    expected = values.get("expected_version", values.get("expected_revision"))
    entry, message = service.run_action(
        entry_id, action, values, expected_version=expected
    )
    if entry is None:
        raise HTTPException(status_code=409 if is_conflict(message) else 400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/review", response_model=ActionResult)
def review_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """审核入口：审核通过只登记一次结论，重复提交只回放原结论。"""
    entry, message = service.run_action(entry_id, "审核通过", dict(payload.values), operator="审批人")
    if entry is None:
        raise HTTPException(status_code=400, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)
