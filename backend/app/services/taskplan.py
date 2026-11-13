"""采集任务编排业务规则：启动计划前必须先读到可用的合规限制。

采集计划不保存一份自己的限制副本——那样一旦合规规则重新审核发布，计划里的
旧值就会和详情页对不上。启动时按计划登记的 ``合规规则ID`` 读取该规则**已发布
版本**的访问间隔、并发上限与请求头模板；读不到必需限制就拒绝启动，并把缺失项
返回给排班端补录。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.store import store
from app.services.compliance import ComplianceService

MODULE = "taskplan"
REQUIRED_FIELDS = ["计划编号", "目标站点", "采集频率"]
LIMIT_FIELDS = ["访问间隔", "并发上限", "请求头模板"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已完成"]
STATUS_DRAFT, STATUS_RUNNING, STATUS_PAUSED, STATUS_DONE = STATUS_ORDER
ACTION_RULES = {"启动计划": STATUS_RUNNING, "暂停计划": STATUS_PAUSED, "归档计划": STATUS_DONE}
ACTION_ALIASES = {
    "启动计划": "启动计划",
    "启动": "启动计划",
    "暂停计划": "暂停计划",
    "暂停": "暂停计划",
    "归档计划": "归档计划",
    "归档": "归档计划",
}
ACTION_FROM_STATES = {
    "启动计划": {STATUS_DRAFT, STATUS_PAUSED},
    "暂停计划": {STATUS_RUNNING},
    "归档计划": {STATUS_RUNNING, STATUS_PAUSED},
}
NEGATIVE_ACTIONS = []

_compliance = ComplianceService()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _rule_id(entry: dict[str, Any]) -> Any:
    return entry.get("合规规则ID", entry.get("合规规则Id", entry.get("compliance_id")))


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return dict(entry)


class TaskplanService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [_public(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return _public(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记采集计划；合规规则ID 可后补，但格式必须可读。"""
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}；采集计划未登记，请补齐后重试"
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": STATUS_DRAFT,
            "pending": True,
            "abnormal": False,
            "历史": [],
        }
        for field, value in values.items():
            entry[str(field)] = value
        for field in REQUIRED_FIELDS:
            entry[field] = _text(values.get(field))
        if values.get("合规规则ID") not in (None, ""):
            try:
                entry["合规规则ID"] = int(values["合规规则ID"])
            except (TypeError, ValueError):
                return None, f"合规规则ID「{values['合规规则ID']}」格式不可读，应为整数"
        entry["合规状态"] = STATUS_DRAFT
        entry["待补录项"] = []
        rows.append(entry)
        return _public(entry), ""

    def check_limits(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str, list[str]]:
        """读取规则限制；返回 (限制, 错误说明, 待补录项)。"""
        rule_id = _rule_id(entry)
        if rule_id in (None, ""):
            return None, "采集计划未登记合规规则ID，无法读取访问间隔、并发上限与请求头模板", list(LIMIT_FIELDS)
        try:
            rule_id_int = int(rule_id)
        except (TypeError, ValueError):
            return None, f"合规规则ID「{rule_id}」格式不可读，应为整数", list(LIMIT_FIELDS)
        limits, message = _compliance.get_limits(rule_id_int)
        if limits is None:
            missing = [field for field in LIMIT_FIELDS if field in message] or list(LIMIT_FIELDS)
            return None, message, missing
        missing = [field for field in LIMIT_FIELDS if not _text(limits.get(field))]
        if missing:
            return None, f"合规规则 {rule_id_int} 缺少必需限制：{'、'.join(missing)}", missing
        return limits, "", []

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str, list[str]]:
        values = values or {}
        canonical = ACTION_ALIASES.get(str(action or "").strip(), str(action or "").strip())
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集计划 {entry_id} 不存在或已归档", []
        if canonical not in ACTION_RULES:
            return None, (
                f"动作「{action}」不属于采集任务编排可执行范围；可用动作：启动计划、暂停计划、归档计划"
            ), []
        status = entry.get("status")
        allowed_from = ACTION_FROM_STATES[canonical]
        if status not in allowed_from:
            return None, (
                f"采集计划 {entry_id}（当前状态：{status}）不能执行「{canonical}」："
                f"仅允许在 {'、'.join(sorted(allowed_from))} 状态执行"
            ), []

        if canonical == "启动计划":
            limits, message, supplement = self.check_limits(entry)
            if limits is None:
                entry["待补录项"] = supplement
                entry["合规限制"] = None
                entry["abnormal"] = True
                return None, (
                    f"采集计划 {entry_id} 不能启动：{message}；"
                    f"待补录项：{'、'.join(supplement)}"
                ), supplement
            entry["合规限制"] = limits
            entry["合规版本"] = limits.get("合规版本")
            entry["合规规则编号"] = limits.get("合规规则编号")
            entry["待补录项"] = []
        elif canonical in ("暂停计划", "归档计划"):
            if not entry.get("合规限制"):
                return None, (
                    f"采集计划 {entry_id} 尚无有效的合规限制记录，不能执行「{canonical}」；"
                    "请先补齐合规规则并启动计划"
                ), []

        entry["status"] = ACTION_RULES[canonical]
        entry["合规状态"] = entry["status"]
        entry["pending"] = entry["status"] != STATUS_DONE
        entry["abnormal"] = False
        entry.setdefault("历史", []).append({
            "动作": canonical,
            "结论": entry["status"],
            "操作时间": _now(),
        })
        return _public(entry), f"采集计划已{canonical}", []
