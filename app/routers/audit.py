"""不可变变更时间线查询接口。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ChangeEvent
from app.schemas.audit import (
    AdminChangeEventResponse,
    ChangeEventResponse,
)
from app.services.audit import AuditContext, get_audit_context
from app.services.windowing import WindowError, decode_cursor, encode_cursor

router = APIRouter()

MAX_LIMIT = 200


def _to_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite 以 UTC 墙钟时间存储，统一去掉时区后再比较。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _query_fingerprint(
    operation_data_id: Optional[int],
    start: Optional[datetime],
    end: Optional[datetime],
    action: Optional[str],
    only_success: Optional[bool],
    correlation_id: Optional[str],
) -> dict[str, Any]:
    return {
        "operation_data_id": operation_data_id,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "action": action,
        "success": only_success,
        "correlation_id": correlation_id,
    }


def _build_query(
    db: Session,
    *,
    operation_data_id: Optional[int],
    start: Optional[datetime],
    end: Optional[datetime],
    action: Optional[str],
    only_success: Optional[bool],
    correlation_id: Optional[str],
    cursor: Optional[str],
):
    query = db.query(ChangeEvent)
    if operation_data_id is not None:
        query = query.filter(ChangeEvent.operation_data_id == operation_data_id)
    start_naive = _to_utc_naive(start)
    end_naive = _to_utc_naive(end)
    if start_naive is not None:
        query = query.filter(ChangeEvent.happened_at >= start_naive)
    if end_naive is not None:
        # 时间区间为闭区间，便于按自然日排查
        query = query.filter(ChangeEvent.happened_at <= end_naive)
    if action:
        query = query.filter(ChangeEvent.action == action)
    if correlation_id:
        query = query.filter(ChangeEvent.correlation_id == correlation_id)
    if only_success is True:
        query = query.filter(ChangeEvent.success.is_(True))
    elif only_success is False:
        query = query.filter(ChangeEvent.success.is_(False))

    fingerprint = _query_fingerprint(
        operation_data_id, start, end, action, only_success, correlation_id
    )
    if cursor:
        at, seq_text = decode_cursor(cursor, fingerprint)
        query = query.filter(
            tuple_(ChangeEvent.happened_at, ChangeEvent.seq)
            > tuple_(_to_utc_naive(at), int(seq_text))
        )
    return (
        query.order_by(ChangeEvent.happened_at.asc(), ChangeEvent.seq.asc()),
        fingerprint,
    )


def _query_timeline(
    db: Session,
    context: AuditContext,
    *,
    operation_data_id: Optional[int],
    start: Optional[datetime],
    end: Optional[datetime],
    action: Optional[str],
    only_success: Optional[bool],
    correlation_id: Optional[str],
    cursor: Optional[str],
    limit: int,
) -> dict[str, Any]:
    try:
        ordered, fingerprint = _build_query(
            db,
            operation_data_id=operation_data_id,
            start=start,
            end=end,
            action=action,
            only_success=only_success,
            correlation_id=correlation_id,
            cursor=cursor,
        )
    except WindowError as exc:
        raise HTTPException(status_code=400, detail=f"分页游标无效：{exc}") from exc

    # 多取一条判断是否还有后续
    rows = ordered.limit(limit + 1).all()
    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor = None
    if has_more and page:
        last = page[-1]
        last_at = last.happened_at
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=timezone.utc)
        next_cursor = encode_cursor(last_at, str(last.seq), fingerprint)

    schema_cls = AdminChangeEventResponse if context.is_admin else ChangeEventResponse
    items = [
        schema_cls.model_validate(row).model_dump(mode="json")
        for row in page
    ]
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "limit": limit,
    }


def _timeline_params(
    start: Optional[datetime] = Query(None, description="业务发生时间起（含）"),
    end: Optional[datetime] = Query(None, description="业务发生时间止（含）"),
    action: Optional[str] = Query(
        None,
        description="动作：operation.create/update/delete、annotation.create/update/delete",
    ),
    only_success: Optional[bool] = Query(None, description="true 只看成功变更，false 只看被拒绝请求"),
    correlation_id: Optional[str] = Query(None, description="按一次请求的关联标识过滤"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(None, description="上一页返回的 next_cursor"),
):
    return {
        "start": start,
        "end": end,
        "action": action,
        "only_success": only_success,
        "correlation_id": correlation_id,
        "limit": limit,
        "cursor": cursor,
    }


@router.get(
    "/operations/{operation_id}/timeline",
    tags=["变更时间线"],
    summary="查询单条作业的不可变变更时间线",
)
def get_operation_timeline(
    operation_id: int,
    params: dict = Depends(_timeline_params),
    db: Session = Depends(get_db),
    context: AuditContext = Depends(get_audit_context),
):
    return _query_timeline(
        db, context, operation_data_id=operation_id, **params
    )


@router.get(
    "/change-events",
    tags=["变更时间线"],
    summary="跨作业查询变更时间线（普通调用方只见脱敏差异）",
)
def list_change_events(
    params: dict = Depends(_timeline_params),
    db: Session = Depends(get_db),
    context: AuditContext = Depends(get_audit_context),
):
    return _query_timeline(
        db, context, operation_data_id=None, **params
    )
