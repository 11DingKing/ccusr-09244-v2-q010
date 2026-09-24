from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OperationChangeEvent
from app.schemas.timeline import (
    OperationChangeEventResponse,
    OperationTimelineResponse,
)
from app.services.change_timeline import (
    RequestContext,
    get_request_context,
    query_timeline,
    to_api_time,
)
from app.services.windowing import WindowError

router = APIRouter()


def _to_response(event: OperationChangeEvent, include_changes: bool) -> OperationChangeEventResponse:
    return OperationChangeEventResponse(
        seq=event.id,
        operation_id=event.operation_id,
        event_type=event.event_type,
        outcome=event.outcome,
        operator=event.operator,
        occurred_at=to_api_time(event.occurred_at),
        request_source=event.request_source,
        correlation_id=event.correlation_id,
        reason=event.reason,
        changes=event.changes if include_changes else None,
        recorded_at=to_api_time(event.created_at),
    )


@router.get(
    "/operations/{operation_id}/timeline",
    response_model=OperationTimelineResponse,
    tags=["变更时间线"],
)
def get_operation_timeline(
    operation_id: int,
    since: Optional[datetime] = Query(None, description="业务发生时间下界（ISO 8601，含）"),
    until: Optional[datetime] = Query(None, description="业务发生时间上界（ISO 8601，含）"),
    event_type: Optional[str] = Query(None, description="事件类型过滤：create/update/annotation_link/delete_attempt"),
    outcome: Optional[str] = Query(None, description="结果过滤：applied/rejected"),
    page_size: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="上一页返回的翻页游标"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
):
    """查询作业记录的不可变变更时间线。

    按 (业务发生时间, 序号) 升序稳定翻页；普通调用方看不到脱敏差异，
    审核角色（X-Role: auditor/admin）可查看脱敏后的前后差异。
    作业被删除后时间线仍然可查。
    """
    if since and until and to_api_time(since) > to_api_time(until):
        raise HTTPException(status_code=400, detail="时间区间下界不能大于上界")
    try:
        events, next_cursor = query_timeline(
            db,
            operation_id,
            since=since,
            until=until,
            event_type=event_type,
            outcome=outcome,
            page_size=page_size,
            cursor=cursor,
        )
    except WindowError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    include_changes = ctx.can_view_sensitive
    return OperationTimelineResponse(
        operation_id=operation_id,
        items=[_to_response(event, include_changes) for event in events],
        next_cursor=next_cursor,
        page_size=page_size,
    )
