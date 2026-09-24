"""变更时间线接口测试。

覆盖：同秒并发更新的确定顺序、无变化请求、角色权限差异、
删除失败（权限/引用）场景、稳定翻页、重启后顺序还原、
失败请求不产生伪成功事件、时间线只追加不可篡改。
"""

import json
import os
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor

_DB_DIR = tempfile.mkdtemp(prefix="robot_timeline_test_")
DB_PATH = os.path.join(_DB_DIR, "timeline_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy.exc import IntegrityError

from main import app
from app.database import Base, SessionLocal, engine
from app.models import OperationChangeEvent


@sa_event.listens_for(engine, "connect")
def _set_busy_timeout(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


client = TestClient(app)
API = "/api/v1"
AUDITOR = {"X-Role": "auditor"}
ADMIN = {"X-Role": "admin", "X-Operator": "root"}


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def make_base_resources():
    model = client.post(f"{API}/robot-models", json={
        "name": "RM-Test", "manufacturer": "RealMotion", "description": "测试机型",
    }).json()
    scene = client.post(f"{API}/scenes", json={
        "name": "测试场景", "category": "生产制造",
    }).json()
    skill = client.post(f"{API}/skills", json={
        "name": "抓取", "category": "操作",
    }).json()
    return model["id"], scene["id"], skill["id"]


def operation_payload(ids, **overrides):
    payload = {
        "robot_model_id": ids[0],
        "scene_id": ids[1],
        "skill_id": ids[2],
        "robot_serial": "SN-ALPHA-0001",
        "motion_trajectory": {"points": [[0.1, 0.2, 0.3]], "frames": 120},
        "perception_records": {"camera": "cam-01", "objects": ["cube"]},
        "timestamp_start": "2026-09-24T08:00:00+00:00",
        "timestamp_end": "2026-09-24T08:00:05+00:00",
    }
    payload.update(overrides)
    return payload


def make_operation(ids, headers=None, **overrides):
    resp = client.post(f"{API}/operations", json=operation_payload(ids, **overrides), headers=headers or {})
    assert resp.status_code == 200
    return resp.json()


def get_timeline(op_id, headers=None, **params):
    resp = client.get(f"{API}/operations/{op_id}/timeline", params=params, headers=headers or {})
    assert resp.status_code == 200
    return resp.json()


def test_create_event_masked_and_role_visibility():
    ids = make_base_resources()
    op = make_operation(ids, headers={
        "X-Operator": "alice",
        "X-Source-System": "robot-gateway",
        "X-Occurred-At": "2026-09-24T09:00:00+00:00",
    })
    op_id = op["id"]

    plain = get_timeline(op_id)
    assert [i["event_type"] for i in plain["items"]] == ["create"]
    item = plain["items"][0]
    assert item["outcome"] == "applied"
    assert item["operator"] == "alice"
    assert item["request_source"] == "robot-gateway"
    assert item["occurred_at"].startswith("2026-09-24T09:00:00")
    assert item["correlation_id"]
    assert item["changes"] is None  # 普通调用方看不到敏感载荷

    as_operator = get_timeline(op_id, headers={"X-Role": "operator"})
    assert as_operator["items"][0]["changes"] is None

    audit = get_timeline(op_id, headers=AUDITOR)
    changes = {c["field"]: c for c in audit["items"][0]["changes"]}
    assert changes["robot_serial"]["before"] is None
    assert changes["robot_serial"]["after"] == "SN***01"
    trajectory = changes["motion_trajectory"]["after"]
    assert trajectory["__masked__"] is True
    assert trajectory["sha256"]
    raw = json.dumps(audit, ensure_ascii=False)
    assert "SN-ALPHA-0001" not in raw  # 原文不落地、不泄漏
    assert "0.1" not in raw


def test_partial_update_and_no_change_request():
    ids = make_base_resources()
    op = make_operation(ids)
    op_id = op["id"]

    resp = client.put(f"{API}/operations/{op_id}", json={"robot_serial": "SN-BETA-0002"},
                      headers={"X-Operator": "bob"})
    assert resp.status_code == 200

    audit = get_timeline(op_id, headers=AUDITOR)
    updates = [i for i in audit["items"] if i["event_type"] == "update"]
    assert len(updates) == 1
    changes = updates[0]["changes"]
    assert [c["field"] for c in changes] == ["robot_serial"]
    assert changes[0]["before"] == "SN***01"
    assert changes[0]["after"] == "SN***02"

    # 无变化请求：相同值、相同时间、空 body 都不产生新事件
    before_count = len(audit["items"])
    assert client.put(f"{API}/operations/{op_id}", json={"robot_serial": "SN-BETA-0002"}).status_code == 200
    assert client.put(f"{API}/operations/{op_id}",
                      json={"timestamp_start": "2026-09-24T08:00:00+00:00"}).status_code == 200
    assert client.put(f"{API}/operations/{op_id}", json={}).status_code == 200
    after = get_timeline(op_id)
    assert len(after["items"]) == before_count


def test_annotation_link_events():
    ids = make_base_resources()
    op = make_operation(ids)
    op_id = op["id"]

    resp = client.post(f"{API}/annotations", json={
        "operation_data_id": op_id,
        "is_success": False,
        "failure_category": "感知异常",
        "failure_description": "视觉识别失败，目标丢失",
        "annotator": "carol",
    }, headers={"X-Operator": "carol"})
    assert resp.status_code == 200
    annotation_id = resp.json()["id"]

    assert client.put(f"{API}/annotations/{annotation_id}",
                      json={"review_status": "approved", "reviewer": "dave"}).status_code == 200

    audit = get_timeline(op_id, headers=AUDITOR)
    links = [i for i in audit["items"] if i["event_type"] == "annotation_link"]
    assert len(links) == 2

    first = {c["field"]: c for c in links[0]["changes"]}
    assert first["is_success"]["after"] is False
    assert first["failure_category"]["after"] == "感知异常"
    assert first["failure_description"]["after"] == "视觉***丢失"
    assert "视觉识别失败" not in json.dumps(audit, ensure_ascii=False)

    second = {c["field"]: c for c in links[1]["changes"]}
    assert second["review_status"]["after"] == "approved"
    assert second["reviewer"]["after"] == "dave"

    assert client.delete(f"{API}/annotations/{annotation_id}").status_code == 200
    audit = get_timeline(op_id, headers=AUDITOR)
    links = [i for i in audit["items"] if i["event_type"] == "annotation_link"]
    assert len(links) == 3
    third = {c["field"]: c for c in links[2]["changes"]}
    assert third["review_status"]["before"] == "approved"
    assert third["review_status"]["after"] is None


def test_batch_create_shares_correlation_id():
    ids = make_base_resources()
    items = [operation_payload(ids, robot_serial=f"SN-BATCH-{i:04d}") for i in range(3)]
    resp = client.post(f"{API}/operations/batch", json=items,
                       headers={"X-Correlation-Id": "batch-req-001", "X-Operator": "ingestor"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success_count"] == 3

    correlation_ids = set()
    for result in body["results"]:
        timeline = get_timeline(result["data"]["id"])
        creates = [i for i in timeline["items"] if i["event_type"] == "create"]
        assert len(creates) == 1
        assert creates[0]["operator"] == "ingestor"
        correlation_ids.add(creates[0]["correlation_id"])
    assert correlation_ids == {"batch-req-001"}


def test_same_second_concurrent_updates_have_deterministic_order():
    ids = make_base_resources()
    op = make_operation(ids)
    op_id = op["id"]
    same_instant = "2026-09-24T10:00:00+00:00"

    def worker(i):
        thread_client = TestClient(app)
        return thread_client.put(
            f"{API}/operations/{op_id}",
            json={"robot_serial": f"SN-CONCUR-{i:04d}"},
            headers={"X-Occurred-At": same_instant, "X-Operator": f"worker-{i}"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(worker, range(4)))
    assert all(r.status_code == 200 for r in responses)

    audit = get_timeline(op_id, headers=AUDITOR)
    updates = [i for i in audit["items"] if i["event_type"] == "update"]
    assert len(updates) == 4
    assert len({u["occurred_at"] for u in updates}) == 1  # 同一业务时刻
    seqs = [u["seq"] for u in updates]
    assert seqs == sorted(seqs)  # 顺序由单调序号决定

    again = get_timeline(op_id, headers=AUDITOR)
    assert [i["seq"] for i in again["items"]] == [i["seq"] for i in audit["items"]]


def test_delete_attempts_rejected_then_applied():
    ids = make_base_resources()
    op = make_operation(ids)
    op_id = op["id"]

    # 权限不足：拒绝并留痕，作业仍在
    resp = client.delete(f"{API}/operations/{op_id}", headers={"X-Operator": "eve"})
    assert resp.status_code == 403
    assert client.get(f"{API}/operations/{op_id}").status_code == 200

    # 管理员但被数据集引用：拒绝并留痕
    dataset = client.post(f"{API}/datasets", json={
        "name": "审核数据集", "robot_model_id": ids[0], "scene_id": ids[1],
        "owner_team": "qa-team", "operation_data_ids": [op_id],
    }, headers=ADMIN)
    assert dataset.status_code == 200
    resp = client.delete(f"{API}/operations/{op_id}", headers=ADMIN)
    assert resp.status_code == 409

    audit = get_timeline(op_id, headers=AUDITOR)
    attempts = [i for i in audit["items"] if i["event_type"] == "delete_attempt"]
    assert [a["outcome"] for a in attempts] == ["rejected", "rejected"]
    assert all(a["changes"] is None for a in attempts)  # 拒绝只记录事实，无伪差异
    assert attempts[0]["operator"] == "eve"
    assert "无权" in attempts[0]["reason"]
    assert "引用" in attempts[1]["reason"]

    # 解除引用后删除成功
    assert client.delete(f"{API}/datasets/{dataset.json()['id']}").status_code == 200
    resp = client.delete(f"{API}/operations/{op_id}", headers=ADMIN)
    assert resp.status_code == 200
    assert client.get(f"{API}/operations/{op_id}").status_code == 404

    audit = get_timeline(op_id, headers=AUDITOR)
    assert [i["event_type"] for i in audit["items"]] == [
        "create", "delete_attempt", "delete_attempt", "delete_attempt",
    ]
    attempts = audit["items"][1:]
    assert [a["outcome"] for a in attempts] == ["rejected", "rejected", "applied"]
    snapshot = {c["field"]: c for c in attempts[-1]["changes"]}
    assert snapshot["robot_serial"]["before"] == "SN***01"
    assert snapshot["robot_serial"]["after"] is None


def test_timeline_pagination_time_range_and_cursor_guard():
    ids = make_base_resources()
    op = make_operation(ids, headers={"X-Occurred-At": "2026-09-24T09:00:00+00:00"})
    op_id = op["id"]
    for i in range(1, 6):
        resp = client.put(f"{API}/operations/{op_id}",
                          json={"robot_serial": f"SN-PAGE-{i:04d}"},
                          headers={"X-Occurred-At": f"2026-09-24T09:0{i}:00+00:00"})
        assert resp.status_code == 200

    seen, cursor, pages = [], None, 0
    while True:
        params = {"page_size": 2}
        if cursor:
            params["cursor"] = cursor
        page = get_timeline(op_id, **params)
        seen.extend(i["seq"] for i in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert pages < 10
    assert pages == 3
    assert len(seen) == 6  # 1 创建 + 5 更新
    assert seen == sorted(seen)

    ranged = get_timeline(op_id, since="2026-09-24T09:02:00+00:00", until="2026-09-24T09:04:00+00:00")
    assert [i["event_type"] for i in ranged["items"]] == ["update"] * 3

    only_updates = get_timeline(op_id, event_type="update")
    assert len(only_updates["items"]) == 5
    only_rejected = get_timeline(op_id, outcome="rejected")
    assert only_rejected["items"] == []

    resp = client.get(f"{API}/operations/{op_id}/timeline", params={"cursor": "tampered-cursor"})
    assert resp.status_code == 400

    first_page = get_timeline(op_id, page_size=2)
    other = make_operation(ids)
    resp = client.get(f"{API}/operations/{other['id']}/timeline",
                      params={"cursor": first_page["next_cursor"]})
    assert resp.status_code == 400  # 游标绑定查询条件，跨作业复用被拒绝


def test_order_survives_restart():
    ids = make_base_resources()
    op = make_operation(ids, headers={"X-Occurred-At": "2026-09-24T11:00:00+00:00"})
    op_id = op["id"]
    for i in range(3):
        assert client.put(f"{API}/operations/{op_id}",
                          json={"robot_serial": f"SN-RS-{i:04d}"},
                          headers={"X-Occurred-At": "2026-09-24T11:00:00+00:00"}).status_code == 200

    before = [i["seq"] for i in get_timeline(op_id)["items"]]
    assert len(before) == 4

    # 模拟服务重启：断开全部连接，从磁盘文件重新读取
    engine.dispose()
    with sqlite3.connect(DB_PATH) as raw:
        rows = raw.execute(
            "SELECT id FROM operation_change_events WHERE operation_id = ? ORDER BY occurred_at, id",
            (op_id,),
        ).fetchall()
    assert [row[0] for row in rows] == before

    after = [i["seq"] for i in get_timeline(op_id)["items"]]
    assert after == before


def test_failed_requests_leave_no_success_events():
    ids = make_base_resources()

    bad = operation_payload(ids, robot_model_id=9999)
    assert client.post(f"{API}/operations", json=bad).status_code == 400

    assert client.put(f"{API}/operations/9999", json={"robot_serial": "SN-X-0001"}).status_code == 404
    assert client.delete(f"{API}/operations/9999", headers=ADMIN).status_code == 404
    assert get_timeline(9999)["items"] == []

    op = make_operation(ids)
    op_id = op["id"]
    first = client.post(f"{API}/annotations", json={"operation_data_id": op_id, "is_success": True})
    assert first.status_code == 200
    duplicate = client.post(f"{API}/annotations", json={"operation_data_id": op_id, "is_success": True})
    assert duplicate.status_code == 400

    timeline = get_timeline(op_id)
    assert [i["event_type"] for i in timeline["items"]] == ["create", "annotation_link"]
    assert all(i["outcome"] == "applied" for i in timeline["items"])


def test_timeline_table_is_append_only():
    ids = make_base_resources()
    op = make_operation(ids)

    db = SessionLocal()
    try:
        event = db.query(OperationChangeEvent).first()
        assert event is not None
        event.operator = "tampered"
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        with pytest.raises(IntegrityError):
            db.delete(event)
            db.commit()
        db.rollback()
    finally:
        db.close()

    timeline = get_timeline(op["id"])
    assert timeline["items"][0]["operator"] == "anonymous"
