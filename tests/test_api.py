"""端到端 API 测试：需先启动服务（默认 http://127.0.0.1:8000，可用 TEST_BASE_URL 覆盖）。

运行：pytest tests/ -v
"""
from __future__ import annotations

import itertools
import json
import os
import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000")

_alert_counter = itertools.count(random.randint(100_000, 900_000) * 1000)


def next_alert_no() -> int:
    return next(_alert_counter)


def rid() -> str:
    return f"req-{uuid.uuid4().hex[:16]}"


def area() -> str:
    return f"区域-{uuid.uuid4().hex[:8]}"


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as c:
        yield c


def publish_payload(**over):
    p = {
        "request_no": rid(),
        "alert_no": next_alert_no(),
        "areas": [area()],
        "level": 3,
        "message": "测试告警：请立即疏散",
        "delay_seconds": 0,
        "duration_seconds": 600,
    }
    p.update(over)
    return p


def do_publish(client, **over):
    return client.post("/api/commands/publish", json=publish_payload(**over))


# ---------------------------------------------------------------- 基础受理

def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_publish_accepted_receipt(client):
    p = publish_payload(areas=[area(), area()], level=4, delay_seconds=10, duration_seconds=120)
    r = client.post("/api/commands/publish", json=p)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["request_no"] == p["request_no"]
    assert body["command_type"] == "publish"
    assert body["status"] == "accepted"
    assert body["alert_no"] == p["alert_no"]
    assert body["areas"] == p["areas"]
    # 生效区间 = 受理时间 + 延迟 ~ +持续
    assert parse(body["effective_from"]) == parse(body["accepted_at"]) + timedelta(seconds=10)
    assert parse(body["effective_to"]) == parse(body["effective_from"]) + timedelta(seconds=120)


# ---------------------------------------------------------------- 幂等与冲突

def test_idempotent_replay_returns_first_receipt(client):
    p = publish_payload()
    r1 = client.post("/api/commands/publish", json=p)
    assert r1.status_code == 201
    r2 = client.post("/api/commands/publish", json=p)
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_same_request_no_different_payload_conflicts(client):
    p = publish_payload()
    assert client.post("/api/commands/publish", json=p).status_code == 201
    p2 = dict(p, message="另一份文案")
    r = client.post("/api/commands/publish", json=p2)
    assert r.status_code == 409


def test_request_no_conflict_across_command_types(client):
    p = publish_payload()
    assert client.post("/api/commands/publish", json=p).status_code == 201
    r = client.post("/api/commands/revoke",
                    json={"request_no": p["request_no"], "alert_no": p["alert_no"]})
    assert r.status_code == 409


def test_concurrent_identical_publish_single_accept(client):
    p = publish_payload()

    def send():
        return client.post("/api/commands/publish", json=p)

    with ThreadPoolExecutor(max_workers=8) as ex:
        rs = list(ex.map(lambda _: send(), range(8)))
    codes = sorted(r.status_code for r in rs)
    assert codes.count(201) == 1, codes
    assert codes.count(200) == 7, codes
    bodies = {json.dumps(r.json(), sort_keys=True) for r in rs if r.status_code in (200, 201)}
    assert len(bodies) == 1  # 所有响应都是同一份首次回执（语义一致）


def test_concurrent_conflicting_payloads_single_winner(client):
    req = rid()
    a1, a2 = next_alert_no(), next_alert_no()

    def send(i):
        return client.post("/api/commands/publish",
                           json=publish_payload(request_no=req, alert_no=a1 if i % 2 == 0 else a2))

    with ThreadPoolExecutor(max_workers=6) as ex:
        rs = list(ex.map(send, range(6)))
    codes = [r.status_code for r in rs]
    assert codes.count(201) == 1, codes
    assert codes.count(409) == 5, codes


# ---------------------------------------------------------------- 入参校验

@pytest.mark.parametrize("override", [
    {"areas": []},                          # 空区域列表
    {"areas": ["A", "A"]},                  # 区域重复
    {"areas": ["  "]},                      # 空白区域名
    {"message": ""},                        # 空文案
    {"message": "   "},                     # 空白文案
    {"duration_seconds": 0},                # 非正持续
    {"duration_seconds": -5},
    {"delay_seconds": -1},                  # 负延迟
    {"level": 0},
    {"level": 6},
    {"alert_no": 0},
    {"alert_no": -7},
    {"request_no": ""},
    {"unknown_field": 1},                   # 未知字段
])
def test_invalid_publish_rejected(client, override):
    r = do_publish(client, **override)
    assert r.status_code == 422, (override, r.text)


def test_failed_publish_leaves_no_state(client):
    a = area()
    # 触发 422（空文案）
    r = do_publish(client, areas=[a], message="")
    assert r.status_code == 422
    # 触发 409（告警号复用）
    p1 = publish_payload()
    assert client.post("/api/commands/publish", json=p1).status_code == 201
    p2 = publish_payload(alert_no=p1["alert_no"], areas=[a])
    assert client.post("/api/commands/publish", json=p2).status_code == 409
    # 区域 a 上没有任何残留告警
    q = client.get("/api/broadcast", params={"area": a})
    assert q.status_code == 200
    assert q.json()["results"] == []
    # 失败命令不占用请求号：同一请求号换新载荷可正常受理
    p3 = publish_payload(request_no=p2["request_no"])
    assert client.post("/api/commands/publish", json=p3).status_code == 201


def test_alert_no_not_reusable(client):
    p = publish_payload()
    assert client.post("/api/commands/publish", json=p).status_code == 201
    r = do_publish(client, alert_no=p["alert_no"])
    assert r.status_code == 409


# ---------------------------------------------------------------- 裁决查询

def test_query_active_and_expired(client):
    a = area()
    p = publish_payload(areas=[a], duration_seconds=3600)
    r = client.post("/api/commands/publish", json=p)
    assert r.status_code == 201
    body = r.json()

    q = client.get("/api/broadcast", params={"area": a})
    res = q.json()["results"]
    assert len(res) == 1
    assert res[0]["alert_no"] == p["alert_no"]
    assert res[0]["area"] == a

    # 生效窗口结束之后 → 空结果
    after = parse(body["effective_to"]) + timedelta(seconds=1)
    q2 = client.get("/api/broadcast", params={"area": a, "at": iso(after)})
    assert q2.json()["results"] == []

    # 生效窗口起点边界（含起点）
    q3 = client.get("/api/broadcast", params={"area": a, "at": body["effective_from"]})
    assert q3.json()["results"][0]["alert_no"] == p["alert_no"]


def test_query_delay_not_yet_active(client):
    a = area()
    p = publish_payload(areas=[a], delay_seconds=3600, duration_seconds=60)
    body = client.post("/api/commands/publish", json=p).json()
    assert client.get("/api/broadcast", params={"area": a}).json()["results"] == []
    during = parse(body["effective_from"]) + timedelta(seconds=1)
    res = client.get("/api/broadcast", params={"area": a, "at": iso(during)}).json()["results"]
    assert res[0]["alert_no"] == p["alert_no"]


def test_priority_and_lower_level_resume(client):
    a = area()
    high = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=5, duration_seconds=2)).json()
    low = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=1, duration_seconds=3600)).json()

    # 高级别生效期间 → 播高级别
    res = client.get("/api/broadcast", params={"area": a}).json()["results"]
    assert res[0]["alert_no"] == high["alert_no"]

    # 高级别到期后 → 低级别自动恢复
    after_high = parse(high["effective_to"]) + timedelta(seconds=1)
    res2 = client.get("/api/broadcast", params={"area": a, "at": iso(after_high)}).json()["results"]
    assert res2[0]["alert_no"] == low["alert_no"]


def test_same_level_earlier_accepted_wins(client):
    a = area()
    first = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=3, duration_seconds=3600)).json()
    second = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=3, duration_seconds=3600)).json()
    res = client.get("/api/broadcast", params={"area": a}).json()["results"]
    # 受理时间早者优先（若受理时间恰好相同，则告警号小者优先——先提交的必然小）
    assert res[0]["alert_no"] == first["alert_no"]
    assert first["alert_no"] < second["alert_no"]


def test_query_all_areas_and_empty(client):
    a1, a2 = area(), area()
    client.post("/api/commands/publish", json=publish_payload(areas=[a1]))
    client.post("/api/commands/publish", json=publish_payload(areas=[a2]))
    res = client.get("/api/broadcast").json()["results"]
    got = {r["area"] for r in res}
    assert {a1, a2} <= got
    # 从未发布过的区域 → 空结果
    assert client.get("/api/broadcast",
                      params={"area": area()}).json()["results"] == []


# ---------------------------------------------------------------- 撤销

def test_revoke_stops_future_but_keeps_history(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=3600)).json()

    rv = client.post("/api/commands/revoke",
                     json={"request_no": rid(), "alert_no": pub["alert_no"]})
    assert rv.status_code == 201, rv.text
    rbody = rv.json()
    assert rbody["command_type"] == "revoke"
    assert rbody["alert_no"] == pub["alert_no"]

    # 撤销之后 → 不再播发
    assert client.get("/api/broadcast", params={"area": a}).json()["results"] == []
    # 撤销时刻边界（不含）→ 不播发
    assert client.get("/api/broadcast", params={
        "area": a, "at": rbody["revoked_at"]}).json()["results"] == []
    # 历史时刻（发布生效起点）→ 仍可查到
    hist = client.get("/api/broadcast", params={
        "area": a, "at": pub["effective_from"]}).json()["results"]
    assert hist[0]["alert_no"] == pub["alert_no"]


def test_revoke_idempotent_replay(client):
    pub = do_publish(client).json()
    payload = {"request_no": rid(), "alert_no": pub["alert_no"]}
    r1 = client.post("/api/commands/revoke", json=payload)
    assert r1.status_code == 201
    r2 = client.post("/api/commands/revoke", json=payload)
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_revoke_unknown_alert(client):
    r = client.post("/api/commands/revoke",
                    json={"request_no": rid(), "alert_no": next_alert_no()})
    assert r.status_code == 404


def test_revoke_releases_area_to_lower_priority(client):
    a = area()
    high = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=5, duration_seconds=3600)).json()
    low = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=1, duration_seconds=3600)).json()
    assert client.get("/api/broadcast", params={"area": a}).json()["results"][0]["alert_no"] \
        == high["alert_no"]
    client.post("/api/commands/revoke", json={"request_no": rid(), "alert_no": high["alert_no"]})
    # 高级别被撤销后，低级别立即恢复
    assert client.get("/api/broadcast", params={"area": a}).json()["results"][0]["alert_no"] \
        == low["alert_no"]
