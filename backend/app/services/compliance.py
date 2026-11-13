"""采集合规配置业务规则：登记、审核、发布、撤销与计划读取口径都收在这里。

数据流约定：

    草稿 --提交审批--> 待审批 --审核通过--> 待审批（已审核，审批快照冻结）
                                        --发布规则--> 已生效 --废弃规则--> 已废弃

- 发布必须基于审核通过的快照；发布后访问间隔、并发上限、请求头模板永远
  从审批记录复制，审核后另改的字段不会覆盖审核稿。
- 只有已生效（可用）的规则才允许撤销/废弃。
- 审核结论只登记一次，重复的审核动作只返回原结论，不追加历史、不重复写库。
- 发布动作在仓库锁内先判定状态与期望版本；后到的并发发布只得到冲突提示，
  不会产生第二次写入。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "compliance"
IDENTITY_FIELDS = ["规则编号", "站点域名", "协议标识"]
LIMIT_FIELDS = ["访问间隔", "并发上限", "请求头模板"]
EDITABLE_FIELDS = IDENTITY_FIELDS + LIMIT_FIELDS
REQUIRED_FIELDS = IDENTITY_FIELDS
STATUS_ORDER = ["草稿", "待审批", "已生效", "已废弃"]
STATUS_DRAFT, STATUS_PENDING, STATUS_ACTIVE, STATUS_DISCARDED = STATUS_ORDER
NEGATIVE_ACTIONS = ["废弃规则", "撤销规则", "撤回审核", "撤回审批"]

# 兼容字段名：接口既接受中文业务字段，也接受 schema 里的 field_3/field_4/field_5。
FIELD_ALIASES = {
    "field_0": "规则编号",
    "field_1": "站点域名",
    "field_2": "协议标识",
    "field_3": "访问间隔",
    "field_4": "并发上限",
    "field_5": "请求头模板",
    "field_6": "合规状态",
    "field_7": "最近审查",
    "access_interval": "访问间隔",
    "concurrency": "并发上限",
    "request_headers": "请求头模板",
    "site": "站点域名",
    "protocol": "协议标识",
}

ACTION_ALIASES = {
    "提交审批": "提交审批",
    "提交审核": "提交审批",
    "送审": "提交审批",
    "审核通过": "审核通过",
    "审批通过": "审核通过",
    "通过审核": "审核通过",
    "审核": "审核通过",
    "发布规则": "发布规则",
    "发布": "发布规则",
    "生效规则": "发布规则",
    "废弃规则": "废弃规则",
    "废弃": "废弃规则",
    "撤销规则": "废弃规则",
    "撤销": "废弃规则",
    "撤回审核": "撤回审核",
    "撤回审批": "撤回审核",
    "撤回": "撤回审核",
}

# 每个动作允许的起始状态。撤回审核只用于尚未发布的待审批规则。
ACTION_FROM_STATES = {
    "提交审批": {STATUS_DRAFT},
    "审核通过": {STATUS_PENDING},
    "发布规则": {STATUS_PENDING},
    "废弃规则": {STATUS_ACTIVE},
    "撤回审核": {STATUS_PENDING},
}

_action_lock = threading.RLock()

CONFLICT_MARKERS = ("版本冲突", "已生效（发布版本", "发布请求与已生效版本冲突")


def is_conflict(message: str) -> bool:
    """判断拒绝原因是否属于并发/重复发布冲突，路由层据此返回 409。"""
    return any(marker in message for marker in CONFLICT_MARKERS)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _missing_limits(record: dict[str, Any]) -> list[str]:
    return [field for field in LIMIT_FIELDS if not _text(record.get(field))]


def _apply_values(entry: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """把提交的字段写进记录，返回实际发生变化的字段名。

    只接受本模块认识的字段，避免把无关字段混进合规记录；空字符串视为清空，
    不会留下 ``None`` 与 ``""`` 两种“空值”并存的残留数据。
    """
    changed: list[str] = []
    for raw_key, raw_value in values.items():
        field = FIELD_ALIASES.get(str(raw_key), str(raw_key))
        if field not in EDITABLE_FIELDS:
            continue
        value = _text(raw_value)
        if _text(entry.get(field)) != value:
            entry[field] = value
            changed.append(field)
    return changed


def _snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    return {field: copy.deepcopy(entry.get(field)) for field in EDITABLE_FIELDS}


def _approval_times(entry: dict[str, Any]) -> dict[str, str]:
    times: dict[str, str] = {}
    for item in entry.get("历史", []):
        action = str(item.get("动作", ""))
        when = str(item.get("操作时间", ""))
        if action and when:
            times[action] = when
    return times


def _append_history(entry: dict[str, Any], action: str, conclusion: str, operator: str, **extra: Any) -> None:
    history = entry.setdefault("历史", [])
    history.append({
        "动作": action,
        "结论": conclusion,
        "操作人": operator,
        "操作时间": _now(),
        **extra,
    })


def _ensure_state(entry: dict[str, Any]) -> dict[str, Any]:
    """给示例数据/历史数据补齐审批与发布快照，保证列表和详情读同一份字段。"""
    entry.setdefault("revision", 1)
    entry.setdefault("历史", [])
    entry.setdefault("reviewed", False)
    entry.setdefault("publish_version", 0)

    status = entry.get("status", STATUS_DRAFT)
    if status == STATUS_ACTIVE and "approved_snapshot" not in entry:
        snapshot = _snapshot(entry)
        entry["approved_snapshot"] = snapshot
        entry["published_snapshot"] = copy.deepcopy(snapshot)
        entry["reviewed"] = True
        entry["approval"] = {
            "结论": "审核通过",
            "审批人": "示例数据",
            "审批时间": entry.get("最近审查") or _now(),
            "审批版本": entry.get("revision", 1),
            "快照": copy.deepcopy(snapshot),
            "来源": "示例数据初始化",
        }
        entry["publish_version"] = entry.get("publish_version") or 1

    # 「合规状态」是列表与详情共用的展示字段，必须跟内部状态一致，
    # 不能继续保留示例数据里那个像状态的旧值。
    entry["合规状态"] = status
    entry.setdefault("最近审查", entry.get("最近审查") or "")
    return entry


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    """返回给接口的记录副本，避免调用方改到仓库里的原始数据。"""
    result = copy.deepcopy(entry)
    # 「合规状态」是列表与详情共用的展示字段，必须跟内部状态一致，
    # 不能继续保留示例数据里那个像状态的旧值。
    result["合规状态"] = result.get("status")
    if result.get("status") == STATUS_DISCARDED and result.get("pending"):
        result["pending"] = False
    return result


def _limit_source(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """计划读取限制的唯一来源：已发布版本快照，其次才是审批快照。

    已生效但没有任何快照的数据会被视为“读取不到必需限制”，由调用方拒绝启动，
    不会退回去读当前字段，避免出现发布后又被临时编辑值覆盖的错位。
    """
    published = entry.get("published_snapshot")
    if isinstance(published, dict) and published:
        return published, "已发布版本"
    approved = entry.get("approved_snapshot")
    if isinstance(approved, dict) and approved:
        return approved, "审核通过版本"
    return None, ""


def _action_error(record_id: int, action: str, status: str, detail: str) -> str:
    return f"合规规则 {record_id}（当前状态：{status}）不能执行「{action}」：{detail}"


class ComplianceService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        site: str | None = None,
        protocol: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with _action_lock:
            rows = store.rows(MODULE)
            for row in rows:
                _ensure_state(row)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            if site:
                rows = [row for row in rows if site in str(row.get("站点域名", ""))]
            if protocol:
                rows = [row for row in rows if protocol in str(row.get("协议标识", ""))]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [_public(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with _action_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            _ensure_state(entry)
            return _public(entry)

    def get_limits(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """供采集计划读取的合规限制；未生效或字段缺失时返回可读原因。"""
        with _action_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"合规规则 {entry_id} 不存在或已归档"
            _ensure_state(entry)
            if entry.get("status") != STATUS_ACTIVE:
                return None, (
                    f"合规规则 {entry_id} 当前状态为「{entry.get('status')}」，"
                    "尚未完成审核发布，不能作为采集计划的限制条件"
                )
            source, source_name = _limit_source(entry)
            if source is None:
                return None, f"合规规则 {entry_id} 没有可用的{source_name or '审批版本'}快照"
            missing = _missing_limits(source)
            if missing:
                return None, f"合规规则 {entry_id} 缺少必需限制：{'、'.join(missing)}"
            limits = {field: source.get(field) for field in LIMIT_FIELDS}
            limits.update({
                "合规规则ID": entry.get("id"),
                "合规规则编号": entry.get("规则编号"),
                "合规版本": entry.get("publish_version", 0),
                "限制来源": source_name,
            })
            return limits, ""

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not _text(values.get(field))]
        if missing:
            # 同时接受别名，避免调用方按 field_0/field_1/field_2 提交时被误判缺字段。
            missing = [
                field for field in REQUIRED_FIELDS
                if not any(_text(values.get(alias)) for alias, target in FIELD_ALIASES.items() if target == field)
            ]
        if missing:
            return None, missing

        with _action_lock:
            rows = store.rows(MODULE)
            entry: dict[str, Any] = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "status": STATUS_DRAFT,
                "pending": True,
                "abnormal": False,
                "revision": 1,
                "reviewed": False,
                "publish_version": 0,
                "历史": [],
            }
            # 登记时填写的全部限制字段都要落库，不能只保留三个必填项，
            # 否则发布后访问间隔/并发上限/请求头模板会凭空丢失。
            for field in EDITABLE_FIELDS:
                entry[field] = ""
            _apply_values(entry, values)
            entry["合规状态"] = STATUS_DRAFT
            entry["最近审查"] = ""
            _append_history(entry, "登记规则", "登记成功", "值班员")
            rows.append(entry)
            return _public(entry), []

    def update_entry(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """修改规则内容；已生效的规则必须先撤销才能改，避免发布稿被暗改。"""
        with _action_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"合规规则 {entry_id} 不存在或已归档"
            _ensure_state(entry)
            if entry.get("status") == STATUS_ACTIVE:
                return None, f"合规规则 {entry_id} 已生效，不能直接修改；请先撤销规则再补录"
            changed = _apply_values(entry, values)
            if not changed:
                return _public(entry), "合规规则内容未变化，没有重复写入"
            entry["revision"] = int(entry.get("revision", 1)) + 1
            _append_history(entry, "修改规则", f"变更字段：{'、'.join(changed)}", "值班员")
            return _public(entry), "合规规则已更新"

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
        *,
        operator: str = "值班员",
        expected_version: Any = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        canonical_action = ACTION_ALIASES.get(str(action or "").strip(), str(action or "").strip())
        # 动作也可以放在 values 里，兼容 {"values": {"action": "发布规则"}}。
        if not canonical_action:
            canonical_action = ACTION_ALIASES.get(_text(values.get("action")), _text(values.get("action")))

        with _action_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"合规规则 {entry_id} 不存在或已归档"
            _ensure_state(entry)

            if canonical_action == "发布规则":
                return self._publish(entry, values, operator, expected_version)
            if canonical_action == "审核通过":
                return self._approve(entry, values, operator)
            if canonical_action == "提交审批":
                return self._submit(entry, values, operator)
            if canonical_action == "废弃规则":
                return self._revoke(entry, operator, action_name="废弃规则")
            if canonical_action == "撤回审核":
                return self._withdraw(entry, operator)

            if canonical_action not in ACTION_FROM_STATES:
                return None, (
                    f"动作「{action}」不属于采集合规配置可执行范围；"
                    "可用动作：提交审批、审核通过、发布规则、撤回审核、废弃规则"
                )
            status = entry.get("status")
            return None, _action_error(entry_id, canonical_action, status, "动作前置状态不满足")

    # ---------------------------------------------------------------- 各动作实现

    def _submit(self, entry: dict[str, Any], values: dict[str, Any], operator: str):
        status = entry.get("status")
        if status == STATUS_PENDING:
            if entry.get("reviewed"):
                return _public(entry), "该规则已审核通过，无需重复提交审批"
            return _public(entry), "该规则已在待审批队列中，未重复提交"
        if status != STATUS_DRAFT:
            return None, _action_error(entry["id"], "提交审批", status, "只有草稿状态可以提交审批")

        changed = _apply_values(entry, values)
        if changed:
            entry["revision"] = int(entry.get("revision", 1)) + 1
        entry["status"] = STATUS_PENDING
        entry["pending"] = True
        entry["abnormal"] = False
        entry["reviewed"] = False
        entry["合规状态"] = STATUS_PENDING
        entry.pop("approval", None)
        entry.pop("approved_snapshot", None)
        entry.pop("published_snapshot", None)
        entry["publish_version"] = 0
        _append_history(
            entry,
            "提交审批",
            "已提交，等待审核" if not changed else f"已提交，变更字段：{'、'.join(changed)}",
            operator,
        )
        return _public(entry), "合规规则已提交审批"

    def _approve(self, entry: dict[str, Any], values: dict[str, Any], operator: str):
        status = entry.get("status")
        if status != STATUS_PENDING:
            return None, _action_error(entry["id"], "审核通过", status, "只有待审批的规则可以审核")
        existing = entry.get("approval")
        if entry.get("reviewed") and isinstance(existing, dict) and existing.get("结论") == "审核通过":
            # 同一审批动作重复提交：只回放原结论，不再追加历史，也不产生第二次写入。
            approval_time = existing.get("审批时间", "")
            return _public(entry), f"该规则已于 {approval_time} 审核通过，本次未重复记录审批结论"

        changed = _apply_values(entry, values)
        if changed:
            entry["revision"] = int(entry.get("revision", 1)) + 1
        # 审核快照冻结：以审核这一刻的字段为准。之后即使有人改字段，
        # 发布也只能读取这份快照，审核稿优先于发布稿。
        snapshot = _snapshot(entry)
        entry["reviewed"] = True
        entry["pending"] = True
        entry["abnormal"] = False
        entry["approval"] = {
            "结论": "审核通过",
            "审批人": operator,
            "审批时间": _now(),
            "审批版本": entry.get("revision", 1),
            "快照": copy.deepcopy(snapshot),
        }
        entry["approved_snapshot"] = snapshot
        entry["最近审查"] = entry["approval"]["审批时间"]
        entry["合规状态"] = entry.get("status")
        extra = {"变更字段": changed} if changed else {}
        _append_history(entry, "审核通过", "通过", operator, **extra)
        missing = _missing_limits(snapshot)
        if missing:
            return _public(entry), (
                "合规规则已审核通过，但审批版本缺少必需限制："
                f"{'、'.join(missing)}；补齐前不能发布，采集计划也不能启动"
            )
        return _public(entry), "合规规则已审核通过"

    def _publish(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        operator: str,
        expected_version: Any,
    ):
        status = entry.get("status")
        if status == STATUS_ACTIVE:
            return None, (
                f"合规规则 {entry['id']} 已生效（发布版本 v{entry.get('publish_version', 0)}），"
                "本次发布请求与已生效版本冲突，已拒绝且未产生第二次写入"
            )
        if status == STATUS_DISCARDED:
            return None, _action_error(entry["id"], "发布规则", status, "规则已撤销，不能再发布")
        if status == STATUS_DRAFT:
            return None, _action_error(entry["id"], "发布规则", status, "规则尚未提交审批，不能发布")
        if status != STATUS_PENDING:
            return None, _action_error(entry["id"], "发布规则", status, "当前状态不允许发布")
        if not entry.get("reviewed") or (entry.get("approval") or {}).get("结论") != "审核通过":
            return None, _action_error(
                entry["id"], "发布规则", status, "规则未完成审核，不能发布；请先审核通过"
            )

        expected = expected_version
        if expected is None:
            expected = values.get("expected_version", values.get("expected_revision"))
        if expected not in (None, ""):
            current = int(entry.get("revision", 1))
            try:
                expected_int = int(str(expected).lstrip("vV"))
            except ValueError:
                return None, f"期望版本「{expected}」格式不可读，应为整数版本号"
            if expected_int != current:
                return None, (
                    f"版本冲突：合规规则 {entry['id']} 期望 v{expected_int}，当前 v{current}，"
                    "本次发布被拒绝且未产生写入"
                )

        approved = entry.get("approved_snapshot")
        if not isinstance(approved, dict) or not approved:
            return None, f"合规规则 {entry['id']} 没有审核通过版本快照，不能发布"
        missing = _missing_limits(approved)
        if missing:
            return None, (
                f"合规规则 {entry['id']} 的审核通过版本缺少必需限制：{'、'.join(missing)}，"
                "补齐并通过审核前不能发布"
            )

        # 审核稿与发布稿不一致时，明确以审核通过的版本为准：逐字段覆盖，
        # 不保留发布前临时修改的旧值，同时把冲突字段写进历史便于追踪。
        conflicts = [
            field for field in EDITABLE_FIELDS
            if _text(approved.get(field)) != _text(entry.get(field))
        ]
        for field in EDITABLE_FIELDS:
            entry[field] = approved.get(field)

        entry["status"] = STATUS_ACTIVE
        entry["pending"] = False
        entry["abnormal"] = False
        entry["合规状态"] = STATUS_ACTIVE
        entry["published_snapshot"] = copy.deepcopy(approved)
        entry["publish_version"] = int(entry.get("publish_version", 0)) + 1
        entry["published_at"] = _now()
        _append_history(
            entry,
            "发布规则",
            "已生效" if not conflicts else f"已生效，以审核通过版本为准，覆盖字段：{'、'.join(conflicts)}",
            operator,
            **({"发布版本": entry["publish_version"], "冲突字段": conflicts} if conflicts else {"发布版本": entry["publish_version"]}),
        )
        if conflicts:
            return _public(entry), (
                "合规规则已发布；发布稿与审核稿存在冲突，已按审核通过版本覆盖："
                f"{'、'.join(conflicts)}"
            )
        return _public(entry), "合规规则已发布并生效"

    def _revoke(self, entry: dict[str, Any], operator: str, *, action_name: str):
        status = entry.get("status")
        if status == STATUS_DISCARDED:
            return _public(entry), "合规规则已撤销/废弃，无需重复操作"
        if status != STATUS_ACTIVE:
            return None, _action_error(
                entry["id"], action_name, status, "只有已生效（可用）的规则才能撤销"
            )
        entry["status"] = STATUS_DISCARDED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["合规状态"] = STATUS_DISCARDED
        entry["revoked_at"] = _now()
        _append_history(
            entry,
            action_name,
            "已撤销，保留发布版本与历史记录",
            operator,
            **{"发布版本": entry.get("publish_version", 0)},
        )
        return _public(entry), "合规规则已撤销并废弃"

    def _withdraw(self, entry: dict[str, Any], operator: str):
        status = entry.get("status")
        if status != STATUS_PENDING:
            if status == STATUS_ACTIVE:
                detail = "规则已生效，不能撤回审核；如需停用请执行废弃规则"
            elif status == STATUS_DISCARDED:
                detail = "规则已废弃，没有可撤回的审核"
            else:
                detail = "只有待审批的规则可以撤回审核"
            return None, _action_error(entry["id"], "撤回审核", status, detail)
        had_approval = bool(entry.get("reviewed") or entry.get("approved_snapshot"))
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["reviewed"] = False
        entry["合规状态"] = STATUS_DRAFT
        entry.pop("approval", None)
        entry.pop("approved_snapshot", None)
        entry.pop("published_snapshot", None)
        entry["publish_version"] = 0
        _append_history(
            entry,
            "撤回审核",
            "已撤回审核，规则回到草稿" if not had_approval else "已撤回审核，原审核结论作废但保留在历史",
            operator,
        )
        return _public(entry), "已撤回审核，合规规则回到草稿状态"

    def history(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        with _action_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"合规规则 {entry_id} 不存在或已归档"
            _ensure_state(entry)
            return copy.deepcopy(entry.get("历史", [])), ""
