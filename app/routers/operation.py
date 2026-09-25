from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OperationData, RobotModel, Scene, Skill, Annotation, DatasetItem
from app.schemas.operation import (
    OperationDataCreate, OperationDataUpdate, OperationDataResponse,
    OperationDataListResponse, BatchOperationResponse, BatchOperationResultItem,
    AnnotationCreate, AnnotationUpdate, AnnotationResponse
)
from app.services.audit import (
    AuditContext,
    ANNOTATION_FIELDS,
    ANNOTATION_SENSITIVE,
    OPERATION_FIELDS,
    OPERATION_SENSITIVE,
    field_changes,
    require_writer_role,
    normalize_for_storage,
    record_change,
    record_denial,
    snapshot,
)

router = APIRouter()


def _deny(
    db: Session,
    context: AuditContext,
    *,
    action: str,
    status_code: int,
    reason_code: str,
    message: str,
    operation_data_id: Optional[int] = None,
    entity: str = "operation_data",
    entity_id: Optional[int] = None,
):
    """记录拒绝事实并返回错误响应；拒绝事件独立事务落库。"""
    record_denial(
        db,
        context,
        action=action,
        reason_code=reason_code,
        reason_detail=message,
        operation_data_id=operation_data_id,
        entity=entity,
        entity_id=entity_id,
    )
    raise HTTPException(
        status_code=status_code,
        detail={
            "reason_code": reason_code,
            "message": message,
            "correlation_id": context.correlation_id,
        },
    )


def _normalize_payload(payload: dict) -> dict:
    return {key: normalize_for_storage(value) for key, value in payload.items()}


def _commit_or_deny(
    db: Session,
    context: AuditContext,
    *,
    action: str,
    message: str,
    operation_data_id: Optional[int] = None,
    entity: str = "operation_data",
    entity_id: Optional[int] = None,
):
    """提交业务事务；若数据库层拒绝（如外键约束导致删除失败），
    丢弃半成品写入并只记录一条拒绝事实，绝不返回伪装成功。"""
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        record_denial(
            db,
            context,
            action=action,
            reason_code="commit_rejected",
            reason_detail=f"{message}: {exc.__class__.__name__}",
            operation_data_id=operation_data_id,
            entity=entity,
            entity_id=entity_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "commit_rejected",
                "message": message,
                "correlation_id": context.correlation_id,
            },
        ) from exc


@router.get("/operations", response_model=OperationDataListResponse, tags=["作业数据"])
def list_operation_data(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    robot_model_id: Optional[int] = Query(None, description="机型ID过滤"),
    scene_id: Optional[int] = Query(None, description="场景ID过滤"),
    skill_id: Optional[int] = Query(None, description="技能ID过滤"),
    robot_serial: Optional[str] = Query(None, description="机器人序列号过滤"),
    data_grade: Optional[str] = Query(None, description="数据等级过滤"),
    is_annotated: Optional[bool] = Query(None, description="是否已标注"),
    is_success: Optional[bool] = Query(None, description="标注成功/失败"),
    failure_category: Optional[str] = Query(None, description="失败大类过滤"),
    db: Session = Depends(get_db)
):
    skip = (page - 1) * page_size

    query = db.query(OperationData)

    if robot_model_id:
        query = query.filter(OperationData.robot_model_id == robot_model_id)
    if scene_id:
        query = query.filter(OperationData.scene_id == scene_id)
    if skill_id:
        query = query.filter(OperationData.skill_id == skill_id)
    if robot_serial:
        query = query.filter(OperationData.robot_serial == robot_serial)
    if data_grade:
        query = query.filter(OperationData.data_grade == data_grade)

    if is_annotated is not None or is_success is not None or failure_category:
        query = query.outerjoin(Annotation, OperationData.id == Annotation.operation_data_id)
        if is_annotated is True:
            query = query.filter(Annotation.id.isnot(None))
        elif is_annotated is False:
            query = query.filter(Annotation.id.is_(None))
        if is_success is not None:
            query = query.filter(Annotation.is_success == is_success)
        if failure_category:
            query = query.filter(Annotation.failure_category == failure_category)

    total = query.count()
    items = query.order_by(OperationData.created_at.desc()).offset(skip).limit(page_size).all()

    return OperationDataListResponse(
        total=total,
        items=items,
        page=page,
        page_size=page_size
    )


@router.get("/operations/{operation_id}", response_model=OperationDataResponse, tags=["作业数据"])
def get_operation_data(operation_id: int, db: Session = Depends(get_db)):
    data = db.query(OperationData).filter(OperationData.id == operation_id).first()
    if not data:
        raise HTTPException(status_code=404, detail="作业数据不存在")
    return data


@router.post("/operations", response_model=OperationDataResponse, tags=["作业数据"])
def create_operation_data(
    data: OperationDataCreate,
    db: Session = Depends(get_db),
    context: AuditContext = Depends(require_writer_role),
):
    robot_model = db.query(RobotModel).filter(RobotModel.id == data.robot_model_id).first()
    if not robot_model:
        _deny(db, context, action="operation.create", status_code=400,
              reason_code="invalid_reference", message=f"机型ID {data.robot_model_id} 不存在")
    scene = db.query(Scene).filter(Scene.id == data.scene_id).first()
    if not scene:
        _deny(db, context, action="operation.create", status_code=400,
              reason_code="invalid_reference", message=f"场景ID {data.scene_id} 不存在")
    skill = db.query(Skill).filter(Skill.id == data.skill_id).first()
    if not skill:
        _deny(db, context, action="operation.create", status_code=400,
              reason_code="invalid_reference", message=f"技能ID {data.skill_id} 不存在")

    operation = OperationData(**_normalize_payload(data.model_dump()))
    db.add(operation)
    db.flush()
    db.refresh(operation)

    after = snapshot(operation, OPERATION_FIELDS)
    record_change(
        db,
        context,
        action="operation.create",
        operation_data_id=operation.id,
        changes=field_changes({}, after, OPERATION_FIELDS, OPERATION_SENSITIVE),
        raw_after=after,
    )
    _commit_or_deny(db, context, action="operation.create",
                    message="作业创建被数据库拒绝", operation_data_id=operation.id)
    db.refresh(operation)
    return operation


@router.post("/operations/batch", response_model=BatchOperationResponse, tags=["作业数据"])
def create_operation_data_batch(
    data_list: List[OperationDataCreate],
    db: Session = Depends(get_db),
    context: AuditContext = Depends(require_writer_role),
):
    total = len(data_list)
    results: List[BatchOperationResultItem] = []
    success_count = 0
    failure_count = 0

    robot_model_ids = {data.robot_model_id for data in data_list}
    scene_ids = {data.scene_id for data in data_list}
    skill_ids = {data.skill_id for data in data_list}

    valid_robot_models = {
        m.id for m in db.query(RobotModel).filter(RobotModel.id.in_(robot_model_ids)).all()
    }
    valid_scenes = {
        s.id for s in db.query(Scene).filter(Scene.id.in_(scene_ids)).all()
    }
    valid_skills = {
        s.id for s in db.query(Skill).filter(Skill.id.in_(skill_ids)).all()
    }

    for index, data in enumerate(data_list):
        errors = []
        if data.robot_model_id not in valid_robot_models:
            errors.append(f"机型ID {data.robot_model_id} 不存在")
        if data.scene_id not in valid_scenes:
            errors.append(f"场景ID {data.scene_id} 不存在")
        if data.skill_id not in valid_skills:
            errors.append(f"技能ID {data.skill_id} 不存在")

        if errors:
            # 批量内的失败项同样以拒绝事件留痕，整批事件共享同一关联标识
            record_denial(
                db,
                context,
                action="operation.create",
                reason_code="invalid_reference",
                reason_detail=f"第{index}项: {'; '.join(errors)}",
                operation_data_id=None,
            )
            failure_count += 1
            results.append(BatchOperationResultItem(
                index=index,
                success=False,
                error="; ".join(errors)
            ))
            continue

        try:
            operation = OperationData(**_normalize_payload(data.model_dump()))
            db.add(operation)
            db.flush()
            after = snapshot(operation, OPERATION_FIELDS)
            record_change(
                db,
                context,
                action="operation.create",
                operation_data_id=operation.id,
                changes=field_changes({}, after, OPERATION_FIELDS, OPERATION_SENSITIVE),
                raw_after=after,
            )
            db.commit()
            success_count += 1
            results.append(BatchOperationResultItem(
                index=index,
                success=True,
                data=operation
            ))
        except Exception as e:
            db.rollback()
            record_denial(
                db,
                context,
                action="operation.create",
                reason_code="persistence_error",
                reason_detail=f"第{index}项: {e}",
                operation_data_id=None,
            )
            failure_count += 1
            results.append(BatchOperationResultItem(
                index=index,
                success=False,
                error=str(e)
            ))

    return BatchOperationResponse(
        total=total,
        success_count=success_count,
        failure_count=failure_count,
        results=results
    )


@router.put("/operations/{operation_id}", response_model=OperationDataResponse, tags=["作业数据"])
def update_operation_data(
    operation_id: int,
    data: OperationDataUpdate,
    db: Session = Depends(get_db),
    context: AuditContext = Depends(require_writer_role),
):
    operation = db.query(OperationData).filter(OperationData.id == operation_id).first()
    if not operation:
        _deny(db, context, action="operation.update", status_code=404,
              reason_code="not_found", message="作业数据不存在",
              operation_data_id=operation_id)

    before = snapshot(operation, OPERATION_FIELDS)
    update_data = _normalize_payload(data.model_dump(exclude_unset=True))
    for field, value in update_data.items():
        setattr(operation, field, value)
    db.flush()
    db.refresh(operation)
    after = snapshot(operation, OPERATION_FIELDS)

    changes = field_changes(before, after, OPERATION_FIELDS, OPERATION_SENSITIVE)
    if not changes:
        # 无变化请求不算变更：丢弃写入，只记录被拒绝的事实
        _deny(db, context, action="operation.update", status_code=400,
              reason_code="no_change", message="请求没有改变任何字段",
              operation_data_id=operation_id)

    record_change(
        db,
        context,
        action="operation.update",
        operation_data_id=operation.id,
        changes=changes,
        raw_before=before,
        raw_after=after,
    )
    _commit_or_deny(db, context, action="operation.create",
                    message="作业创建被数据库拒绝", operation_data_id=operation.id)
    db.refresh(operation)
    return operation


@router.delete("/operations/{operation_id}", tags=["作业数据"])
def delete_operation_data(
    operation_id: int,
    db: Session = Depends(get_db),
    context: AuditContext = Depends(require_writer_role),
):
    operation = db.query(OperationData).filter(OperationData.id == operation_id).first()
    if not operation:
        # 删除尝试同样必须留痕：这是一次失败的删除，而不是静默的 404
        _deny(db, context, action="operation.delete", status_code=404,
              reason_code="not_found", message="作业数据不存在",
              operation_data_id=operation_id)

    before = snapshot(operation, OPERATION_FIELDS)
    annotation = db.query(Annotation).filter(
        Annotation.operation_data_id == operation_id
    ).first()
    annotation_before = snapshot(annotation, ANNOTATION_FIELDS) if annotation else None

    # 被数据集引用的作业不能静默级联删除引用关系，拒绝删除并记录失败尝试
    in_use = db.query(DatasetItem.id).filter(
        DatasetItem.operation_data_id == operation_id
    ).first()
    if in_use:
        _deny(db, context, action="operation.delete", status_code=409,
              reason_code="in_use_by_dataset",
              message="作业已被数据集条目引用，无法删除，请先从数据集移除",
              operation_data_id=operation_id)

    # 级联删除标注属于同一次请求产生的多项变化，与作业删除共享关联标识
    if annotation is not None:
        record_change(
            db,
            context,
            action="annotation.delete",
            operation_data_id=operation_id,
            entity="annotation",
            entity_id=annotation.id,
            changes=field_changes(annotation_before, {}, ANNOTATION_FIELDS, ANNOTATION_SENSITIVE),
            raw_before=annotation_before,
        )
    record_change(
        db,
        context,
        action="operation.delete",
        operation_data_id=operation_id,
        changes={"existence": {"before": "present", "after": "deleted"}},
        raw_before=before,
    )

    db.delete(operation)
    _commit_or_deny(db, context, action="operation.delete",
                    message="作业删除被数据库拒绝，可能仍被数据集引用",
                    operation_data_id=operation_id)
    return {"message": "删除成功"}


@router.get("/operations/{operation_id}/annotation", response_model=AnnotationResponse, tags=["标注管理"])
def get_annotation_by_operation(operation_id: int, db: Session = Depends(get_db)):
    annotation = db.query(Annotation).filter(Annotation.operation_data_id == operation_id).first()
    if not annotation:
        raise HTTPException(status_code=404, detail="该作业数据尚未标注")
    return annotation


@router.get("/annotations", response_model=List[AnnotationResponse], tags=["标注管理"])
def list_annotations(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    is_success: Optional[bool] = Query(None, description="是否成功"),
    failure_category: Optional[str] = Query(None, description="失败大类"),
    review_status: Optional[str] = Query(None, description="审核状态"),
    annotator: Optional[str] = Query(None, description="标注人"),
    db: Session = Depends(get_db)
):
    query = db.query(Annotation)
    if is_success is not None:
        query = query.filter(Annotation.is_success == is_success)
    if failure_category:
        query = query.filter(Annotation.failure_category == failure_category)
    if review_status:
        query = query.filter(Annotation.review_status == review_status)
    if annotator:
        query = query.filter(Annotation.annotator == annotator)
    return query.order_by(Annotation.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/annotations/{annotation_id}", response_model=AnnotationResponse, tags=["标注管理"])
def get_annotation(annotation_id: int, db: Session = Depends(get_db)):
    annotation = db.query(Annotation).filter(Annotation.id == annotation_id).first()
    if not annotation:
        raise HTTPException(status_code=404, detail="标注记录不存在")
    return annotation


@router.post("/annotations", response_model=AnnotationResponse, tags=["标注管理"])
def create_annotation(
    data: AnnotationCreate,
    db: Session = Depends(get_db),
    context: AuditContext = Depends(require_writer_role),
):
    operation = db.query(OperationData).filter(OperationData.id == data.operation_data_id).first()
    if not operation:
        _deny(db, context, action="annotation.create", status_code=400,
              reason_code="not_found", message="作业数据不存在",
              operation_data_id=data.operation_data_id, entity="annotation")
    existing = db.query(Annotation).filter(Annotation.operation_data_id == data.operation_data_id).first()
    if existing:
        _deny(db, context, action="annotation.create", status_code=400,
              reason_code="already_annotated",
              message="该作业数据已存在标注记录，请使用更新接口",
              operation_data_id=data.operation_data_id, entity="annotation",
              entity_id=existing.id)

    if not data.is_success and not data.failure_category:
        _deny(db, context, action="annotation.create", status_code=400,
              reason_code="invalid_payload", message="标注失败时必须指定失败大类",
              operation_data_id=data.operation_data_id, entity="annotation")

    annotation = Annotation(**_normalize_payload(data.model_dump()))
    db.add(annotation)
    db.flush()
    db.refresh(annotation)
    after = snapshot(annotation, ANNOTATION_FIELDS)
    record_change(
        db,
        context,
        action="annotation.create",
        operation_data_id=annotation.operation_data_id,
        entity="annotation",
        entity_id=annotation.id,
        changes=field_changes({}, after, ANNOTATION_FIELDS, ANNOTATION_SENSITIVE),
        raw_after=after,
    )
    _commit_or_deny(db, context, action="annotation.create",
                    message="标注创建被数据库拒绝",
                    operation_data_id=annotation.operation_data_id,
                    entity="annotation", entity_id=annotation.id)
    db.refresh(annotation)
    return annotation


@router.put("/annotations/{annotation_id}", response_model=AnnotationResponse, tags=["标注管理"])
def update_annotation(
    annotation_id: int,
    data: AnnotationUpdate,
    db: Session = Depends(get_db),
    context: AuditContext = Depends(require_writer_role),
):
    annotation = db.query(Annotation).filter(Annotation.id == annotation_id).first()
    if not annotation:
        _deny(db, context, action="annotation.update", status_code=404,
              reason_code="not_found", message="标注记录不存在",
              entity="annotation", entity_id=annotation_id)

    before = snapshot(annotation, ANNOTATION_FIELDS)
    update_data = _normalize_payload(data.model_dump(exclude_unset=True))
    for field, value in update_data.items():
        setattr(annotation, field, value)
    db.flush()
    db.refresh(annotation)
    after = snapshot(annotation, ANNOTATION_FIELDS)

    changes = field_changes(before, after, ANNOTATION_FIELDS, ANNOTATION_SENSITIVE)
    if not changes:
        _deny(db, context, action="annotation.update", status_code=400,
              reason_code="no_change", message="请求没有改变任何字段",
              operation_data_id=annotation.operation_data_id,
              entity="annotation", entity_id=annotation_id)

    record_change(
        db,
        context,
        action="annotation.update",
        operation_data_id=annotation.operation_data_id,
        entity="annotation",
        entity_id=annotation.id,
        changes=changes,
        raw_before=before,
        raw_after=after,
    )
    _commit_or_deny(db, context, action="annotation.create",
                    message="标注创建被数据库拒绝",
                    operation_data_id=annotation.operation_data_id,
                    entity="annotation", entity_id=annotation.id)
    db.refresh(annotation)
    return annotation


@router.delete("/annotations/{annotation_id}", tags=["标注管理"])
def delete_annotation(
    annotation_id: int,
    db: Session = Depends(get_db),
    context: AuditContext = Depends(require_writer_role),
):
    annotation = db.query(Annotation).filter(Annotation.id == annotation_id).first()
    if not annotation:
        _deny(db, context, action="annotation.delete", status_code=404,
              reason_code="not_found", message="标注记录不存在",
              entity="annotation", entity_id=annotation_id)

    before = snapshot(annotation, ANNOTATION_FIELDS)
    operation_data_id = annotation.operation_data_id
    record_change(
        db,
        context,
        action="annotation.delete",
        operation_data_id=operation_data_id,
        entity="annotation",
        entity_id=annotation.id,
        changes={"existence": {"before": "present", "after": "deleted"}},
        raw_before=before,
    )
    db.delete(annotation)
    _commit_or_deny(db, context, action="annotation.delete",
                    message="标注删除被数据库拒绝",
                    operation_data_id=operation_data_id,
                    entity="annotation", entity_id=annotation.id)
    return {"message": "删除成功"}


FAILURE_CATEGORIES = [
    {"category": "感知异常", "subcategories": ["视觉识别失败", "深度传感器异常", "目标丢失", "光照不足"]},
    {"category": "运动控制异常", "subcategories": ["轨迹偏差超限", "关节超限", "碰撞检测触发", "速度异常"]},
    {"category": "抓取异常", "subcategories": ["抓取力不足", "物体滑脱", "姿态错误", "真空吸盘失效"]},
    {"category": "环境干扰", "subcategories": ["粉尘干扰", "温度异常", "振动干扰", "电磁干扰"]},
    {"category": "硬件故障", "subcategories": ["电机故障", "编码器异常", "通信中断", "电源异常"]},
    {"category": "其他", "subcategories": ["未知错误", "人为干预"]}
]


@router.get("/failure-categories", tags=["标注管理"])
def get_failure_categories():
    return FAILURE_CATEGORIES
