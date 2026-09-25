"""作业变更时间线（审计）服务。

职责：
- 从请求头解析操作者、角色、来源和关联标识；
- 计算字段级前后差异并对敏感载荷脱敏；
- 以仅追加方式写入 change_events，成功变更与业务改动同事务提交，
  拒绝事件独立事务落库，绝不伪装成成功变更。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ChangeEvent

# ---- 角色与权限 -----------------------------------------------------------

ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
KNOWN_ROLES = {ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER}

# viewer 仅可读；editor 可写；admin 在 editor 之上可读完整载荷
WRITE_ROLES = {ROLE_EDITOR, ROLE_ADMIN}

# ---- 被审计字段与敏感字段 -------------------------------------------------

OPERATION_FIELDS: tuple[str, ...] = (
    "robot_model_id",
    "scene_id",
    "skill_id",
    "robot_serial",
    "motion_trajectory",
    "perception_records",
    "grasp_result",
    "timestamp_start",
    "timestamp_end",
    "duration_ms",
    "environment_conditions",
    "hardware_status",
    "quality_score",
    "completeness_score",
    "data_grade",
)

OPERATION_SENSITIVE: frozenset[str] = frozenset(
    {"motion_trajectory", "perception_records", "grasp_result",
     "environment_conditions", "hardware_status"}
)

ANNOTATION_FIELDS: tuple[str, ...] = (
    "is_success",
    "failure_category",
    "failure_subcategory",
    "failure_description",
    "annotator",
    "review_status",
    "reviewer",
    "review_notes",
    "annotation_quality_score",
)

ANNOTATION_SENSITIVE: frozenset[str] = frozenset(
    {"failure_description", "review_notes"}
)

def _action_from_request(request: Request) -> str:
    """根据方法和路径推断权限拒绝对应的动作名。"""
    path = request.url.path.rstrip("/")
    method = request.method
    if path.endswith("/operations/batch"):
        entity, suffix = "operation", "create"
    elif path.endswith("/operations") or path.endswith("/annotations"):
        entity = "annotation" if path.endswith("/annotations") else "operation"
        suffix = {"POST": "create", "GET": "read"}.get(method, "access")
    elif "/operations/" in path:
        entity = "operation"
        suffix = {"PUT": "update", "DELETE": "delete"}.get(method, "access")
    elif "/annotations/" in path:
        entity = "annotation"
        suffix = {"PUT": "update", "DELETE": "delete"}.get(method, "access")
    else:
        entity, suffix = path.rsplit("/", 1)[-1], "access"
    return f"{entity}.{suffix}"


@dataclass(frozen=True)
class AuditContext:
    operator_id: str
    operator_role: str
    request_source: str
    correlation_id: str
    client_request_id: str | None

    @property
    def is_admin(self) -> bool:
        return self.operator_role == ROLE_ADMIN


def _utc_now_naive() -> datetime:
    # SQLite 以 UTC 墙钟时间存储；成功事件在拿到写锁后打点，
    # 因此 happened_at 顺序与提交（seq）顺序一致，同秒并发也能稳定排序
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _header(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def get_audit_context(request: Request, response: Response) -> AuditContext:
    """每个 HTTP 请求构造一次上下文，同一次请求内的事件共享关联标识。"""
    role = (_header(request, "X-Operator-Role") or ROLE_VIEWER).lower()
    if role not in KNOWN_ROLES:
        # 未知角色按最小权限处理，不赋予写权限
        role = ROLE_VIEWER

    correlation_id = _header(request, "X-Correlation-ID") or uuid.uuid4().hex
    context = AuditContext(
        operator_id=_header(request, "X-Operator-Id") or "anonymous",
        operator_role=role,
        request_source=_header(request, "X-Request-Source") or "unknown",
        correlation_id=correlation_id[:64],
        client_request_id=(_header(request, "X-Request-Id") or None),
    )
    # 便于调用方与支持方按号排查
    response.headers["X-Correlation-ID"] = context.correlation_id
    return context


def require_writer_role(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AuditContext:
    """写接口的权限依赖：越权请求同样记录拒绝事实（success=False）。"""
    context = get_audit_context(request, response)
    if context.operator_role not in WRITE_ROLES:
        record_denial(
            db,
            context,
            action=_action_from_request(request),
            reason_code="forbidden",
            reason_detail=f"角色 {context.operator_role} 没有变更权限，仅 {sorted(WRITE_ROLES)} 可写",
        )
        raise HTTPException(
            status_code=403,
            detail={
                "reason_code": "forbidden",
                "message": "没有变更权限",
                "correlation_id": context.correlation_id,
            },
        )
    return context


# ---- 序列化、脱敏与差异 ---------------------------------------------------

def _json_safe(value: Any) -> Any:
    """递归转换，使差异与完整快照可以落入 JSON 列。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def normalize_for_storage(value: Any) -> Any:
    """入参时间统一转为 UTC 墙钟时间，与 SQLite 存储形式保持一致。"""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def mask_value(field: str, value: Any, sensitive: frozenset[str]) -> Any:
    """敏感载荷只暴露“是否存在”，不暴露内容、长度或键名。"""
    if field in sensitive:
        return {"redacted": True, "present": value is not None}
    return _json_safe(value)


def snapshot(obj: Any, fields: Sequence[str]) -> dict[str, Any]:
    return {field: _json_safe(getattr(obj, field, None)) for field in fields}


def field_changes(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
                  fields: Sequence[str], sensitive: frozenset[str]) -> dict[str, dict[str, Any]]:
    """生成脱敏后的字段级差异；调用方传入的应是变更前后的完整快照，
    只有取值真正变化的字段才会出现在结果中。
    """
    before = before or {}
    after = after or {}
    changes: dict[str, dict[str, Any]] = {}
    for field in fields:
        if field not in before and field not in after:
            continue
        old_value = before.get(field)
        new_value = after.get(field)
        if old_value == new_value:
            continue
        changes[field] = {
            "before": mask_value(field, old_value, sensitive),
            "after": mask_value(field, new_value, sensitive),
        }
    return changes


def record_change(
    db: Session,
    context: AuditContext,
    *,
    action: str,
    operation_data_id: int | None,
    entity: str = "operation_data",
    entity_id: int | None = None,
    changes: Mapping[str, Any] | None = None,
    raw_before: Mapping[str, Any] | None = None,
    raw_after: Mapping[str, Any] | None = None,
) -> ChangeEvent:
    """成功变更：必须与业务改动在同一事务内提交。

    happened_at 在调用时（业务写入已 flush、即将取得提交锁）打点，
    保证同秒并发请求的业务时间顺序与其落库顺序一致。
    """
    event = ChangeEvent(
        correlation_id=context.correlation_id,
        operation_data_id=operation_data_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        happened_at=_utc_now_naive(),
        operator_id=context.operator_id,
        operator_role=context.operator_role,
        request_source=context.request_source,
        client_request_id=context.client_request_id,
        success=True,
        changes=dict(changes) if changes is not None else None,
        raw_before=dict(raw_before) if raw_before is not None else None,
        raw_after=dict(raw_after) if raw_after is not None else None,
    )
    db.add(event)
    db.flush()
    return event


def record_denial(
    db: Session,
    context: AuditContext,
    *,
    action: str,
    reason_code: str,
    reason_detail: str | None = None,
    operation_data_id: int | None = None,
    entity: str = "operation_data",
    entity_id: int | None = None,
) -> ChangeEvent:
    """拒绝事实：丢弃任何半成品业务写入，以独立事务只记录拒绝。

    成功标志恒为 False，不携带前后差异或完整载荷，因此不可能被误读为
    一次成功变更。
    """
    db.rollback()
    event = ChangeEvent(
        correlation_id=context.correlation_id,
        operation_data_id=operation_data_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        happened_at=_utc_now_naive(),
        operator_id=context.operator_id,
        operator_role=context.operator_role,
        request_source=context.request_source,
        client_request_id=context.client_request_id,
        success=False,
        reason_code=reason_code,
        reason_detail=reason_detail,
        changes=None,
        raw_before=None,
        raw_after=None,
    )
    db.add(event)
    db.commit()
    return event
