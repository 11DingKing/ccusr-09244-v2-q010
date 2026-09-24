"""作业记录变更时间线：脱敏差异、事件记录与稳定分页查询。

时间线事件为只追加（append-only），排序键为 (occurred_at, id)，
id 单调递增，服务重启后顺序不变。一次请求内的多个事件共享
correlation_id；被拒绝的请求只记录拒绝事实（outcome=rejected，
无前后差异），绝不伪装成成功变更。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable, Optional

from fastapi import HTTPException, Request
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import OperationChangeEvent
from app.services.windowing import WindowError, decode_cursor, encode_cursor

# ---- 事件类型与结果 ----

EVENT_CREATE = "create"
EVENT_UPDATE = "update"
EVENT_ANNOTATION_LINK = "annotation_link"
EVENT_DELETE_ATTEMPT = "delete_attempt"
EVENT_TYPES = {EVENT_CREATE, EVENT_UPDATE, EVENT_ANNOTATION_LINK, EVENT_DELETE_ATTEMPT}

OUTCOME_APPLIED = "applied"
OUTCOME_REJECTED = "rejected"
OUTCOMES = {OUTCOME_APPLIED, OUTCOME_REJECTED}

# 可查看脱敏差异（敏感载荷）的角色；其余调用方只能看到事件元数据。
PRIVILEGED_ROLES = {"auditor", "admin"}
# 允许执行删除的角色。
DELETE_ROLES = {"admin"}

# ---- 字段脱敏规则 ----

# 大体量/敏感载荷：不落地原文，只保留类型与指纹，供审核比对是否一致。
PAYLOAD_FIELDS = {
    "motion_trajectory",
    "perception_records",
    "grasp_result",
    "environment_conditions",
    "hardware_status",
}
# 标识类/自由文本：部分遮蔽。
TEXT_MASK_FIELDS = {
    "robot_serial",
    "failure_description",
    "review_notes",
}

# 参与差异记录的作业字段（与 OperationDataUpdate 对齐）。
OPERATION_TRACKED_FIELDS = (
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

# 参与差异记录的标注字段。
ANNOTATION_TRACKED_FIELDS = (
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

# 删除成功时留存的标量快照字段（不含大载荷）。
DELETE_SNAPSHOT_FIELDS = (
    "robot_model_id",
    "scene_id",
    "skill_id",
    "robot_serial",
    "timestamp_start",
    "timestamp_end",
    "duration_ms",
    "quality_score",
    "completeness_score",
    "data_grade",
)


# ---- 请求上下文 ----


@dataclass(frozen=True)
class RequestContext:
    """一次请求的审计上下文；同一请求内所有事件共享 correlation_id。"""

    operator: str
    role: str
    occurred_at: datetime  # 业务发生时间，tz-aware UTC
    request_source: str
    correlation_id: str

    @property
    def can_view_sensitive(self) -> bool:
        return self.role in PRIVILEGED_ROLES


def _clip(value: str, limit: int) -> str:
    return value[:limit] if value else value


def get_request_context(request: Request) -> RequestContext:
    """从请求头构建审计上下文。

    - X-Operator：操作者，缺省 anonymous
    - X-Role：operator（默认）/ auditor / admin，未知角色按普通调用方处理
    - X-Occurred-At：业务发生时间（ISO 8601），缺省为服务器当前时间
    - X-Source-System：请求来源系统，缺省取客户端地址
    - X-Correlation-Id：上游关联标识，缺省生成新的
    """
    headers = request.headers

    occurred_raw = headers.get("x-occurred-at")
    if occurred_raw:
        try:
            occurred_at = datetime.fromisoformat(occurred_raw.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="X-Occurred-At 时间格式无效，需为 ISO 8601")
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        occurred_at = occurred_at.astimezone(timezone.utc)
    else:
        occurred_at = datetime.now(timezone.utc)

    role = headers.get("x-role", "operator").strip().lower() or "operator"
    if role not in PRIVILEGED_ROLES and role != "operator":
        role = "operator"

    source = headers.get("x-source-system")
    if not source:
        client = request.client.host if request.client else None
        source = client or "unknown"

    correlation_id = headers.get("x-correlation-id") or uuid.uuid4().hex

    return RequestContext(
        operator=_clip(headers.get("x-operator", "anonymous").strip() or "anonymous", 100),
        role=role,
        occurred_at=occurred_at,
        request_source=_clip(source.strip(), 200),
        correlation_id=_clip(correlation_id.strip(), 64),
    )


# ---- 脱敏 ----


def mask_text(value: str) -> str:
    """标识/文本部分遮蔽：保留首尾各两位，短串全遮蔽。"""
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def mask_payload(value: Any) -> dict:
    """大载荷脱敏：只保留类型与内容指纹，不落地原文。"""
    raw = json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, default=str).encode()
    return {
        "__masked__": True,
        "type": type(value).__name__,
        "sha256": sha256(raw).hexdigest()[:16],
    }


def mask_field(field_name: str, value: Any) -> Any:
    if value is None:
        return None
    if field_name in PAYLOAD_FIELDS:
        return mask_payload(value)
    if field_name in TEXT_MASK_FIELDS and isinstance(value, str):
        return mask_text(value)
    return _jsonable(value)


def _normalize(value: Any) -> Any:
    """递归归一化：datetime 统一为 UTC ISO 字符串，避免朴素/带时区时间造成假差异。"""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _jsonable(value: Any) -> Any:
    """转成可 JSON 序列化的值（datetime -> UTC ISO 字符串等）。"""
    return json.loads(json.dumps(_normalize(value), ensure_ascii=False, default=str))


# ---- 差异构建与事件记录 ----


def build_changes(field_names: Iterable[str], before: dict, after: dict) -> list[dict]:
    """构建脱敏后的前后差异；仅保留值真正发生变化的字段。"""
    changes = []
    for name in field_names:
        before_value = _jsonable(before.get(name))
        after_value = _jsonable(after.get(name))
        if before_value == after_value:
            continue
        changes.append(
            {
                "field": name,
                "before": mask_field(name, before.get(name)),
                "after": mask_field(name, after.get(name)),
            }
        )
    return changes


def record_event(
    db: Session,
    ctx: RequestContext,
    operation_id: int,
    event_type: str,
    outcome: str = OUTCOME_APPLIED,
    changes: Optional[list[dict]] = None,
    reason: Optional[str] = None,
) -> OperationChangeEvent:
    """追加一条时间线事件（随调用方事务一起提交）。

    拒绝事件强制清空差异，避免失败请求伪装成成功变更。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知事件类型: {event_type}")
    if outcome not in OUTCOMES:
        raise ValueError(f"未知事件结果: {outcome}")
    if outcome == OUTCOME_REJECTED:
        changes = None
    event = OperationChangeEvent(
        operation_id=operation_id,
        event_type=event_type,
        outcome=outcome,
        operator=ctx.operator,
        occurred_at=_to_store(ctx.occurred_at),
        request_source=ctx.request_source,
        correlation_id=ctx.correlation_id,
        changes=changes,
        reason=_clip(reason, 500) if reason else None,
    )
    db.add(event)
    return event


def _to_store(dt: datetime) -> datetime:
    """统一按 UTC 朴素时间落库（SQLite 不保留时区）。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def to_api_time(dt: Optional[datetime]) -> Optional[datetime]:
    """读取时还原为 UTC 时区时间。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---- 稳定分页查询 ----


def query_timeline(
    db: Session,
    operation_id: int,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    event_type: Optional[str] = None,
    outcome: Optional[str] = None,
    page_size: int = 50,
    cursor: Optional[str] = None,
) -> tuple[list[OperationChangeEvent], Optional[str]]:
    """按作业与时间区间做键集分页，排序 (occurred_at, id) 升序。

    游标绑定查询条件（防篡改、防跨查询复用），新写入的事件
    不会影响已发出游标的后续翻页。
    """
    if event_type and event_type not in EVENT_TYPES:
        raise WindowError(f"未知事件类型: {event_type}")
    if outcome and outcome not in OUTCOMES:
        raise WindowError(f"未知事件结果: {outcome}")

    query_fingerprint = {
        "operation_id": operation_id,
        "since": to_api_time(since).isoformat() if since else None,
        "until": to_api_time(until).isoformat() if until else None,
        "event_type": event_type,
        "outcome": outcome,
    }

    conditions = [OperationChangeEvent.operation_id == operation_id]
    if since:
        conditions.append(OperationChangeEvent.occurred_at >= _to_store(since))
    if until:
        conditions.append(OperationChangeEvent.occurred_at <= _to_store(until))
    if event_type:
        conditions.append(OperationChangeEvent.event_type == event_type)
    if outcome:
        conditions.append(OperationChangeEvent.outcome == outcome)

    if cursor:
        at, identity = decode_cursor(cursor, query_fingerprint)
        try:
            last_id = int(identity)
        except ValueError as exc:
            raise WindowError("游标格式无效") from exc
        conditions.append(
            or_(
                OperationChangeEvent.occurred_at > _to_store(at),
                and_(
                    OperationChangeEvent.occurred_at == _to_store(at),
                    OperationChangeEvent.id > last_id,
                ),
            )
        )

    rows = (
        db.query(OperationChangeEvent)
        .filter(*conditions)
        .order_by(OperationChangeEvent.occurred_at.asc(), OperationChangeEvent.id.asc())
        .limit(page_size + 1)
        .all()
    )

    has_more = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(to_api_time(last.occurred_at), str(last.id), query_fingerprint)
    return page, next_cursor
