from pydantic import BaseModel, Field
from typing import Optional, List, Any
from datetime import datetime


class ChangeItem(BaseModel):
    field: str = Field(..., description="发生变化的字段名")
    before: Optional[Any] = Field(None, description="变更前的值（已脱敏）")
    after: Optional[Any] = Field(None, description="变更后的值（已脱敏）")


class OperationChangeEventResponse(BaseModel):
    seq: int = Field(..., description="全局单调序号，同一业务时刻内按此排序")
    operation_id: int = Field(..., description="作业数据ID")
    event_type: str = Field(..., description="事件类型：create/update/annotation_link/delete_attempt")
    outcome: str = Field(..., description="结果：applied/rejected")
    operator: str = Field(..., description="操作者")
    occurred_at: datetime = Field(..., description="业务发生时间")
    request_source: str = Field(..., description="请求来源")
    correlation_id: str = Field(..., description="关联标识，同一请求产生的多个事件共享")
    reason: Optional[str] = Field(None, description="拒绝原因（仅拒绝事件）")
    changes: Optional[List[ChangeItem]] = Field(
        None, description="脱敏后的前后差异，仅审核角色可见；拒绝事件不携带差异"
    )
    recorded_at: Optional[datetime] = Field(None, description="事件落库时间")


class OperationTimelineResponse(BaseModel):
    operation_id: int
    items: List[OperationChangeEventResponse]
    next_cursor: Optional[str] = Field(None, description="下一页游标，为空表示已到最后")
    page_size: int
