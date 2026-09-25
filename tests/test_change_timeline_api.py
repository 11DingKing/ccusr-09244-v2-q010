"""变更时间线接口验证。

覆盖：
- 创建 / 局部更新 / 标注关联 / 删除（成功与失败）的时间线记录；
- 操作者、业务发生时间、请求来源、关联标识与脱敏前后差异；
- 同秒并发更新的稳定顺序、无变化请求、权限差异；
- 按作业与时间区间的稳定翻页、普通调用方看不到敏感载荷；
- 服务重启后顺序还原与底层不可变约束。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func as sa_func
from sqlalchemy.orm import Session

from main import app
from app.models import ChangeEvent

API = "/api/v1"


def uuid4() -> str:
    return uuid.uuid4().hex

EDITOR = {
    "X-Operator-Id": "editor-li",
    "X-Operator-Role": "editor",
    "X-Request-Source": "label-console",
}
ADMIN = {
    "X-Operator-Id": "auditor-wang",
    "X-Operator-Role": "admin",
    "X-Request-Source": "audit-desk",
}
VIEWER = {
    "X-Operator-Id": "viewer-zhao",
    "X-Operator-Role": "viewer",
    "X-Request-Source": "dashboard",
}


def operation_payload(ts: datetime | None = None, **overrides):
    ts = ts or datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
    payload = {
        "robot_model_id": 1,
        "scene_id": 1,
        "skill_id": 1,
        "robot_serial": "SN-0001",
        "motion_trajectory": {"points": [[0, 0], [1, 1]], "secret_plan": "抓取A点"},
        "perception_records": {"camera": "cam-7", "frames": ["f1", "f2"]},
        "grasp_result": {"force_n": 12.5},
        "timestamp_start": ts.isoformat(),
        "timestamp_end": (ts + timedelta(seconds=12)).isoformat(),
        "duration_ms": 12000,
        "environment_conditions": {"temperature_c": 24.1},
        "hardware_status": {"battery": 0.92},
    }
    payload.update(overrides)
    return payload


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def base_resources(client):
    for path, body in (
        ("/robot-models", {"name": "RM-AUDIT-X1", "manufacturer": "RealMotion"}),
        ("/scenes", {"name": "质检工位", "category": "制造"}),
        ("/skills", {"name": "精密抓取", "category": "抓取"}),
    ):
        resp = client.post(f"{API}{path}", json=body)
        assert resp.status_code == 200, resp.text


def create_operation(client, headers=None, **overrides):
    headers = headers or EDITOR
    resp = client.post(
        f"{API}/operations",
        json=operation_payload(**overrides),
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp


def timeline(client, op_id, headers=None, **params):
    resp = client.get(
        f"{API}/operations/{op_id}/timeline", params=params, headers=headers or VIEWER
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def walk_all(client, url, headers=None, page_size=2, **params):
    """沿 next_cursor 走完整个结果集。"""
    headers = headers or VIEWER
    seen = []
    cursor = None
    while True:
        query = dict(params, limit=page_size)
        if cursor:
            query["cursor"] = cursor
        resp = client.get(url, params=query, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        seen.extend(data["items"])
        cursor = data["next_cursor"]
        if not cursor:
            assert data["has_more"] is False
            break
    return seen


# ---- 创建 ----------------------------------------------------------------

def test_create_records_immutable_event_with_operator_source_and_masking(client):
    corr = f"corr-create-{uuid4()}"
    headers = dict(EDITOR, **{"X-Correlation-ID": corr, "X-Request-Id": "req-1"})
    resp = create_operation(client, headers=headers)
    op_id = resp.json()["id"]
    assert resp.headers["X-Correlation-ID"] == corr

    data = timeline(client, op_id)
    assert len(data["items"]) == 1
    event = data["items"][0]
    assert event["action"] == "operation.create"
    assert event["success"] is True
    assert event["operator_id"] == "editor-li"
    assert event["operator_role"] == "editor"
    assert event["request_source"] == "label-console"
    assert event["correlation_id"] == corr
    assert event["client_request_id"] == "req-1"
    assert event["happened_at"]
    assert event["seq"] >= 1
    assert event["operation_data_id"] == op_id

    changes = event["changes"]
    # 普通字段直接可见
    assert changes["robot_serial"]["after"] == "SN-0001"
    assert changes["duration_ms"]["after"] == 12000
    # 敏感载荷被脱敏，看不到键名或内容
    trajectory = changes["motion_trajectory"]
    assert trajectory["after"] == {"redacted": True, "present": True}
    assert "secret_plan" not in str(trajectory)
    # 普通调用方拿不到完整载荷
    assert "raw_after" not in event and "raw_before" not in event


# ---- 局部更新 ------------------------------------------------------------

def test_partial_update_records_only_changed_fields(client):
    op_id = create_operation(client).json()["id"]
    corr = f"corr-update-{uuid4()}"
    resp = client.put(
        f"{API}/operations/{op_id}",
        json={"duration_ms": 9999, "data_grade": "A"},
        headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
    )
    assert resp.status_code == 200, resp.text

    data = timeline(client, op_id)
    events = data["items"]
    update_event = next(e for e in events if e["action"] == "operation.update")
    assert update_event["correlation_id"] == corr
    assert set(update_event["changes"].keys()) == {"duration_ms", "data_grade"}
    assert update_event["changes"]["duration_ms"] == {"before": 12000, "after": 9999}
    assert update_event["changes"]["data_grade"] == {"before": None, "after": "A"}


def test_concurrent_same_second_updates_keep_stable_order(client):
    op_id = create_operation(client).json()["id"]
    barrier = threading.Barrier(5)
    results = []
    lock = threading.Lock()

    def update(grade):
        barrier.wait()  # 尽量让请求落在同一秒
        corr = f"corr-conc-{grade}"
        resp = client.put(
            f"{API}/operations/{op_id}",
            json={"data_grade": grade},
            headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
        )
        with lock:
            results.append((grade, resp))

    threads = [threading.Thread(target=update, args=(g,)) for g in "ABCDE"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 所有请求都成功或被识别为无变化，绝不出现服务器错误
    assert all(r.status_code in (200, 400) for _, r in results)
    succeeded = [r for _, r in results if r.status_code == 200]
    assert len(succeeded) >= 2

    data = timeline(client, op_id, action="operation.update", limit=200)
    events = data["items"]
    # (happened_at, seq) 严格递增，同秒事件靠 seq 区分且不丢不重
    keys = [(e["happened_at"], e["seq"]) for e in events]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    grades = [e["changes"]["data_grade"]["after"] for e in events]
    assert client.get(f"{API}/operations/{op_id}").json()["data_grade"] == grades[-1]
    assert len(events) == len(succeeded)


def test_no_change_request_is_rejection_not_a_change(client):
    op_id = create_operation(client).json()["id"]
    corr = f"corr-noop-{uuid4()}"
    resp = client.put(
        f"{API}/operations/{op_id}",
        json={"duration_ms": 12000, "robot_serial": "SN-0001"},
        headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "no_change"

    data = timeline(client, op_id)
    by_corr = [e for e in data["items"] if e["correlation_id"] == corr]
    assert len(by_corr) == 1
    event = by_corr[0]
    assert event["success"] is False
    assert event["reason_code"] == "no_change"
    assert event["changes"] is None
    assert event["raw_before"] is None if "raw_before" in event else True
    # 没有产生成功变更
    assert all(
        not (e["correlation_id"] == corr and e["success"]) for e in data["items"]
    )


# ---- 权限差异 ------------------------------------------------------------

def test_viewer_cannot_write_and_denial_is_recorded(client):
    op_id = create_operation(client).json()["id"]
    corr = f"corr-forbid-{uuid4()}"
    resp = client.put(
        f"{API}/operations/{op_id}",
        json={"data_grade": "D"},
        headers=dict(VIEWER, **{"X-Correlation-ID": corr}),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason_code"] == "forbidden"

    # 业务值未被修改
    assert client.get(f"{API}/operations/{op_id}").json()["data_grade"] is None

    # 拒绝事实可以通过关联标识在全局事件流中查到
    denials = walk_all(
        client, f"{API}/change-events",
        correlation_id=corr, only_success=False,
    )
    assert len(denials) == 1
    assert denials[0]["success"] is False
    assert denials[0]["reason_code"] == "forbidden"
    assert denials[0]["operator_role"] == "viewer"
    assert denials[0]["changes"] is None


def test_unknown_role_defaults_to_viewer(client):
    op_id = create_operation(client).json()["id"]
    resp = client.delete(
        f"{API}/operations/{op_id}",
        headers={"X-Operator-Id": "x", "X-Operator-Role": "superuser"},
    )
    assert resp.status_code == 403


# ---- 标注关联 ------------------------------------------------------------

def test_annotation_lifecycle_is_timelined(client):
    op_id = create_operation(client).json()["id"]

    # 失败标注缺少失败大类：拒绝留痕
    bad = client.post(
        f"{API}/annotations",
        json={"operation_data_id": op_id, "is_success": False},
        headers=EDITOR,
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["reason_code"] == "invalid_payload"

    corr = f"corr-anno-{uuid4()}"
    ok = client.post(
        f"{API}/annotations",
        json={
            "operation_data_id": op_id,
            "is_success": False,
            "failure_category": "感知异常",
            "failure_subcategory": "光照不足",
            "failure_description": "现场强光导致特征点丢失，包含操作员备注",
            "annotator": "labeler-chen",
        },
        headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
    )
    assert ok.status_code == 200, ok.text
    annotation_id = ok.json()["id"]

    data = timeline(client, op_id)
    created = [
        e for e in data["items"]
        if e["correlation_id"] == corr and e["success"]
    ]
    assert len(created) == 1
    event = created[0]
    assert event["action"] == "annotation.create"
    assert event["entity"] == "annotation"
    assert event["entity_id"] == annotation_id
    assert event["changes"]["failure_category"]["after"] == "感知异常"
    # 敏感的文字描述被脱敏
    assert event["changes"]["failure_description"]["after"] == {
        "redacted": True, "present": True
    }
    assert "操作员备注" not in str(event)

    # 重复关联被拒绝
    dup = client.post(
        f"{API}/annotations",
        json={"operation_data_id": op_id, "is_success": True},
        headers=EDITOR,
    )
    assert dup.status_code == 400
    assert dup.json()["detail"]["reason_code"] == "already_annotated"

    # 标注更新同样进入该作业的时间线
    upd = client.put(
        f"{API}/annotations/{annotation_id}",
        json={"review_status": "approved", "reviewer": "reviewer-sun"},
        headers=ADMIN,
    )
    assert upd.status_code == 200, upd.text
    data = timeline(client, op_id, action="annotation.update")
    assert len(data["items"]) == 1
    assert data["items"][0]["changes"]["review_status"] == {
        "before": "pending", "after": "approved"
    }


# ---- 删除：成功、级联、失败 ----------------------------------------------

def test_delete_success_cascades_annotation_and_shares_correlation(client):
    op_id = create_operation(client).json()["id"]
    client.post(
        f"{API}/annotations",
        json={"operation_data_id": op_id, "is_success": True, "annotator": "a"},
        headers=EDITOR,
    )
    corr = f"corr-del-{uuid4()}"
    resp = client.delete(
        f"{API}/operations/{op_id}",
        headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
    )
    assert resp.status_code == 200, resp.text
    assert client.get(f"{API}/operations/{op_id}").status_code == 404

    # 作业已不存在，仍可通过全局事件流按作业与关联标识审计
    events = walk_all(
        client, f"{API}/change-events",
        correlation_id=corr,
    )
    actions = [e["action"] for e in events]
    assert actions == ["annotation.delete", "operation.delete"]
    assert all(e["success"] for e in events)
    assert all(e["correlation_id"] == corr for e in events)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)


def test_delete_missing_is_recorded_as_failed_attempt(client):
    missing_id = 9_999_999
    corr = f"corr-delmiss-{uuid4()}"
    resp = client.delete(
        f"{API}/operations/{missing_id}",
        headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "not_found"

    events = walk_all(
        client, f"{API}/change-events", correlation_id=corr
    )
    assert len(events) == 1
    event = events[0]
    assert event["success"] is False
    assert event["action"] == "operation.delete"
    assert event["reason_code"] == "not_found"
    assert event["changes"] is None


def test_delete_blocked_by_dataset_reference_is_denial(client):
    op_id = create_operation(client).json()["id"]
    ds = client.post(
        f"{API}/datasets",
        json={
            "name": "引用待删作业的数据集",
            "robot_model_id": 1,
            "scene_id": 1,
            "skill_id": 1,
            "owner_team": "data-team",
            "operation_data_ids": [op_id],
        },
    )
    assert ds.status_code == 200, ds.text
    dataset_id = ds.json()["id"]

    corr = f"corr-delfk-{uuid4()}"
    resp = client.delete(
        f"{API}/operations/{op_id}",
        headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "in_use_by_dataset"

    # 作业仍然存在，数据集条目引用关系保持完整
    assert client.get(f"{API}/operations/{op_id}").status_code == 200
    assert op_id in client.get(f"{API}/datasets/{dataset_id}/operation-ids").json()

    events = walk_all(
        client, f"{API}/change-events", correlation_id=corr
    )
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["reason_code"] == "in_use_by_dataset"
    # 失败请求不能留下伪装的成功删除事件
    assert not [e for e in events if e["success"]]


# ---- 翻页、时间区间、游标防串用 ------------------------------------------

def test_timeline_pagination_is_stable_and_cursor_bound_to_query(client):
    op_id = create_operation(client).json()["id"]
    for grade in "ABC":
        resp = client.put(
            f"{API}/operations/{op_id}",
            json={"data_grade": grade},
            headers=EDITOR,
        )
        assert resp.status_code == 200, resp.text

    all_events = walk_all(
        client, f"{API}/operations/{op_id}/timeline",
        page_size=2, action="operation.update",
    )
    assert [e["changes"]["data_grade"]["after"] for e in all_events] == ["A", "B", "C"]
    seq_pairs = [(e["happened_at"], e["seq"]) for e in all_events]
    assert seq_pairs == sorted(seq_pairs)

    # 取一页游标后改变过滤条件，游标必须被拒绝
    first = client.get(
        f"{API}/operations/{op_id}/timeline",
        params={"limit": 2, "action": "operation.update"},
    ).json()
    cursor = first["next_cursor"]
    mismatched = client.get(
        f"{API}/operations/{op_id}/timeline",
        params={"limit": 2, "action": "operation.create", "cursor": cursor},
    )
    assert mismatched.status_code == 400

    # 跨作业串用游标同样被拒绝
    global_page = client.get(
        f"{API}/change-events", params={"limit": 2, "cursor": cursor}
    )
    assert global_page.status_code == 400


def test_timeline_time_window_filter(client):
    op_id = create_operation(client).json()["id"]
    created_at = datetime.fromisoformat(
        timeline(client, op_id)["items"][0]["happened_at"]
    )

    def fmt(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    hit = client.get(
        f"{API}/operations/{op_id}/timeline",
        params={
            "start": fmt(created_at - timedelta(minutes=1)),
            "end": fmt(created_at + timedelta(minutes=1)),
        },
    )
    assert hit.status_code == 200
    assert any(e["action"] == "operation.create" for e in hit.json()["items"])

    empty = client.get(
        f"{API}/operations/{op_id}/timeline",
        params={
            "start": fmt(created_at + timedelta(days=1)),
            "end": fmt(created_at + timedelta(days=2)),
        },
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []


# ---- 敏感载荷可见性 ------------------------------------------------------

def test_admin_sees_raw_payload_but_editor_does_not(client):
    op_id = create_operation(
        client, headers=dict(EDITOR, **{"X-Correlation-ID": (c := f"corr-raw-{uuid4()}")})
    ).json()["id"]

    editor_view = timeline(client, op_id, headers=EDITOR)
    event = next(e for e in editor_view["items"] if e["correlation_id"] == c)
    assert "raw_after" not in event
    assert event["changes"]["motion_trajectory"]["after"]["redacted"] is True

    admin_view = timeline(client, op_id, headers=ADMIN)
    admin_event = next(e for e in admin_view["items"] if e["correlation_id"] == c)
    assert admin_event["raw_after"]["motion_trajectory"]["secret_plan"] == "抓取A点"
    assert admin_event["raw_after"]["perception_records"]["camera"] == "cam-7"

    # 管理员视图不改变脱敏 changes 的表现
    assert admin_event["changes"]["motion_trajectory"]["after"]["redacted"] is True


# ---- 一次请求多项变化共享关联标识（批量） --------------------------------

def test_batch_request_shares_correlation_across_success_and_denial(client):
    corr = f"corr-batch-{uuid4()}"
    resp = client.post(
        f"{API}/operations/batch",
        json=[
            operation_payload(robot_serial="BATCH-OK"),
            operation_payload(robot_model_id=999_999, robot_serial="BATCH-BAD"),
        ],
        headers=dict(EDITOR, **{"X-Correlation-ID": corr}),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success_count"] == 1 and body["failure_count"] == 1

    events = walk_all(
        client, f"{API}/change-events", correlation_id=corr
    )
    assert len(events) == 2
    assert [e["success"] for e in events] == [True, False]
    assert events[1]["reason_code"] == "invalid_reference"
    assert events[1]["changes"] is None
    # 成功事件能定位到新建的作业，拒绝事件没有伪造作业 ID
    assert events[0]["operation_data_id"] is not None
    assert events[1]["operation_data_id"] is None


# ---- 不可变性与重启还原 ---------------------------------------------------

def test_change_events_are_immutable_at_storage_level(db_path):
    conn = sqlite3.connect(db_path)
    try:
        seq = conn.execute("SELECT MAX(seq) FROM change_events").fetchone()[0]
        assert seq is not None
        with pytest.raises(sqlite3.Error):
            conn.execute(
                "UPDATE change_events SET operator_id = 'attacker' WHERE seq = ?",
                (seq,),
            )
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM change_events WHERE seq = ?", (seq,))
        conn.commit()
        unchanged = conn.execute(
            "SELECT operator_id FROM change_events WHERE seq = ?", (seq,)
        ).fetchone()
        assert unchanged is not None
        assert unchanged[0] != "attacker"
    finally:
        conn.close()


def test_order_survives_restart_and_seq_keeps_growing(db_path):
    # 模拟服务重启：用全新引擎重新打开同一个数据库文件
    restart_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    with Session(restart_engine) as session:
        rows = session.query(ChangeEvent.seq).order_by(
            ChangeEvent.happened_at.asc(), ChangeEvent.seq.asc()
        ).all()
        seqs = [r[0] for r in rows]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))
        max_seq = session.query(sa_func.max(ChangeEvent.seq)).scalar()

        # 重启后新追加的序号继续单调增长
        new_event = ChangeEvent(
            correlation_id="restart-check",
            operation_data_id=None,
            action="restart.probe",
            entity="system",
            happened_at=datetime.now(timezone.utc).replace(tzinfo=None),
            operator_role="admin",
            request_source="test",
            success=True,
        )
        session.add(new_event)
        session.commit()
        session.refresh(new_event)
        assert new_event.seq == max_seq + 1
    restart_engine.dispose()
