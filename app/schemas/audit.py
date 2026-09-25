from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, field_serializer


class ChangeEventResponse(BaseModel):
    """变更时间线条目；普通调用方只能看到脱敏后的差异。"""

    model_config = ConfigDict(from_attributes=True)

    seq: int
    correlation_id: str
    operation_data_id: Optional[int]
    action: str
    entity: str
    entity_id: Optional[int] = None

    happened_at: datetime
    recorded_at: Optional[datetime] = None

    operator_id: Optional[str] = None
    operator_role: str
    request_source: str
    client_request_id: Optional[str] = None

    # False 表示请求被拒绝，此时 changes 为空，只有拒绝原因
    success: bool
    reason_code: Optional[str] = None
    reason_detail: Optional[str] = None

    # 字段级差异，敏感载荷已脱敏（{"redacted": true, "present": ...}）
    changes: Optional[Dict[str, Any]] = None

    @field_serializer("happened_at", "recorded_at")
    def _serialize_datetime(self, value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()


class AdminChangeEventResponse(ChangeEventResponse):
    """审计管理员视角，额外包含完整前后载荷。"""

    raw_before: Optional[Dict[str, Any]] = None
    raw_after: Optional[Dict[str, Any]] = None


class ChangeTimelineResponse(BaseModel):
    items: List[ChangeEventResponse]
    next_cursor: Optional[str] = None
    has_more: bool
    limit: int
