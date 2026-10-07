"""端到端 API 测试：需先启动服务（默认 http://127.0.0.1:8000，可用 TEST_BASE_URL 覆盖）。

运行：pytest tests/ -v
"""
from __future__ import annotations

import itertools
import json
import os
import random
import threading
import time
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


def amend_payload(pub_alert_no, **over):
    p = {
        "request_no": rid(),
        "alert_no": pub_alert_no,
        "areas": [area()],
        "level": 4,
        "message": "修订后文案：请沿北侧通道撤离",
    }
    p.update(over)
    return p


def do_amend(client, pub_alert_no, **over):
    return client.post("/api/commands/amend", json=amend_payload(pub_alert_no, **over))


def broadcast(client, a, at=None):
    params = {"area": a}
    if at is not None:
        params["at"] = at
    return client.get("/api/broadcast", params=params).json()["results"]


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


# ---------------------------------------------------------------- 修订受理与回执

def test_amend_accepted_receipt(client):
    pub = do_publish(client, duration_seconds=3600).json()
    new_areas = [area(), area()]
    r = do_amend(client, pub["alert_no"], areas=new_areas, level=2,
                 message="修订文案 v2")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["request_no"] is not None
    assert body["command_type"] == "amend"
    assert body["status"] == "accepted"
    assert body["alert_no"] == pub["alert_no"]
    assert body["level"] == 2
    assert body["message"] == "修订文案 v2"
    assert body["areas"] == new_areas
    # 受理时刻 = 新版生效时刻；终点沿用原发布终点，不延长不缩短
    assert body["amended_at"] == body["accepted_at"] == body["effective_from"]
    assert body["effective_to"] == pub["effective_to"]
    assert parse(body["accepted_at"]) > parse(pub["accepted_at"])


# ---------------------------------------------------------------- 修订后的播发与历史

def test_amend_replaces_broadcast_but_keeps_history(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=3, message="原版文案", duration_seconds=3600)).json()

    am = do_amend(client, pub["alert_no"], areas=[a], level=5,
                  message="修订版文案")
    assert am.status_code == 201

    # 修订受理时刻之后（含受理时刻边界）→ 播新版
    now_res = broadcast(client, a)
    assert len(now_res) == 1
    assert now_res[0]["message"] == "修订版文案"
    assert now_res[0]["level"] == 5
    at_boundary = broadcast(client, a, at=am.json()["amended_at"])
    assert at_boundary[0]["message"] == "修订版文案"

    # 修订前的历史时刻 → 仍返回旧版本文案/级别，生效区间不变
    hist = broadcast(client, a, at=pub["effective_from"])
    assert len(hist) == 1
    assert hist[0]["message"] == "原版文案"
    assert hist[0]["level"] == 3
    assert hist[0]["effective_from"] == pub["effective_from"]
    assert hist[0]["effective_to"] == pub["effective_to"]

    # 原终点之后仍为空——修订没有延长播发
    assert broadcast(client, a, at=pub["effective_to"]) == []


def test_amend_replaces_areas_versioned(client):
    a1, a2, a3 = area(), area(), area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a1, a2], duration_seconds=3600)).json()

    am = do_amend(client, pub["alert_no"], areas=[a2, a3])
    assert am.status_code == 201

    # 修订后：旧区域 a1 不再播发，a2 延续，新区域 a3 开始播发
    assert broadcast(client, a1) == []
    assert broadcast(client, a2)[0]["message"] == "修订后文案：请沿北侧通道撤离"
    assert broadcast(client, a3)[0]["alert_no"] == pub["alert_no"]

    # 历史时刻：a1 仍可查到，a3 当时尚不在覆盖范围内
    hist_at = pub["effective_from"]
    assert broadcast(client, a1, at=hist_at)[0]["alert_no"] == pub["alert_no"]
    assert broadcast(client, a3, at=hist_at) == []
    # a2 上的历史版本仍是原内容
    assert broadcast(client, a2, at=hist_at)[0]["message"] == "测试告警：请立即疏散"


def test_amend_changes_priority_from_amend_time(client):
    a = area()
    # 高优先级先发布（很快到期），中级优先级后发布、长期有效
    high = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=5, message="高级原版", duration_seconds=3600)).json()
    low = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=2, message="中级告警", duration_seconds=3600)).json()
    assert broadcast(client, a)[0]["alert_no"] == high["alert_no"]

    # 高级别告警被修订降为最低级 → 从受理时刻起让位于原中级告警
    am = do_amend(client, high["alert_no"], areas=[a], level=1, message="高级被降级")
    assert am.status_code == 201
    now_res = broadcast(client, a)
    assert now_res[0]["alert_no"] == low["alert_no"]
    assert now_res[0]["message"] == "中级告警"

    # 修订前的历史时刻裁决仍以旧级别 5 取胜
    hist = broadcast(client, a, at=high["effective_from"])
    assert hist[0]["alert_no"] == high["alert_no"]
    assert hist[0]["message"] == "高级原版"
    assert hist[0]["level"] == 5


def test_multiple_sequential_amends_chain_versions(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=3, message="v0", duration_seconds=3600)).json()
    am1 = do_amend(client, pub["alert_no"], areas=[a], level=3, message="v1").json()
    am2 = do_amend(client, pub["alert_no"], areas=[a], level=3, message="v2").json()
    assert parse(am2["accepted_at"]) >= parse(am1["accepted_at"])

    assert broadcast(client, a)[0]["message"] == "v2"
    assert broadcast(client, a, at=am1["amended_at"])[0]["message"] in ("v1", "v2")
    # v1 生效窗口内的任意时刻（取两版受理时刻之间）仍是 v1
    middle = iso(parse(am1["accepted_at"]) + (parse(am2["accepted_at"]) - parse(am1["accepted_at"])) / 2)
    assert broadcast(client, a, at=middle)[0]["message"] == "v1"
    assert broadcast(client, a, at=pub["effective_from"])[0]["message"] == "v0"
    # 两次修订终点均为原终点
    assert am1["effective_to"] == am2["effective_to"] == pub["effective_to"]


# ---------------------------------------------------------------- 修订资格校验

def test_amend_unknown_alert_404(client):
    r = do_amend(client, next_alert_no())
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_amend_not_yet_effective_409(client):
    pub = client.post("/api/commands/publish", json=publish_payload(
        delay_seconds=3600, duration_seconds=60)).json()
    r = do_amend(client, pub["alert_no"])
    assert r.status_code == 409
    assert "尚未生效" in r.json()["detail"]


def test_amend_expired_409(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=1)).json()
    time.sleep(1.2)
    r = do_amend(client, pub["alert_no"], areas=[a])
    assert r.status_code == 409
    assert "到期" in r.json()["detail"]
    # 到期后修订未留下任何版本/区域痕迹
    assert broadcast(client, a) == []


def test_amend_revoked_409(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=3600)).json()
    assert client.post("/api/commands/revoke",
                       json={"request_no": rid(), "alert_no": pub["alert_no"]}).status_code == 201
    r = do_amend(client, pub["alert_no"], areas=[a])
    assert r.status_code == 409
    assert "撤销" in r.json()["detail"]
    assert broadcast(client, a) == []


# ---------------------------------------------------------------- 修订与撤销的顺序

def test_revoke_after_amend_keeps_amend_history(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=3, message="原版", duration_seconds=3600)).json()
    am = do_amend(client, pub["alert_no"], areas=[a], level=4, message="修订版").json()
    rv = client.post("/api/commands/revoke",
                     json={"request_no": rid(), "alert_no": pub["alert_no"]})
    assert rv.status_code == 201

    # 撤销终止后续播发
    assert broadcast(client, a) == []
    # 修订期间（修订受理后、撤销前）保留修订版历史
    during = broadcast(client, a, at=am["amended_at"])
    assert len(during) == 1
    assert during[0]["message"] == "修订版"
    assert during[0]["level"] == 4
    # 修订前历史仍是原版
    assert broadcast(client, a, at=pub["effective_from"])[0]["message"] == "原版"


def test_amend_after_revoke_fails_even_at_history_time(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=3600)).json()
    client.post("/api/commands/revoke",
                json={"request_no": rid(), "alert_no": pub["alert_no"]})
    # 撤销先成功 → 即便查询时刻落在历史窗口，修订也必须失败
    r = do_amend(client, pub["alert_no"], areas=[a])
    assert r.status_code == 409


def test_concurrent_amend_and_revoke_single_winner(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=3600)).json()
    amend_body = amend_payload(pub["alert_no"], areas=[a])
    revoke_body = {"request_no": rid(), "alert_no": pub["alert_no"]}

    barrier = threading.Barrier(2)

    def send_amend():
        barrier.wait()
        return client.post("/api/commands/amend", json=amend_body)

    def send_revoke():
        barrier.wait()
        return client.post("/api/commands/revoke", json=revoke_body)

    with ThreadPoolExecutor(max_workers=2) as ex:
        r_amend, r_revoke = list(ex.map(lambda f: f(), [send_amend, send_revoke]))

    assert r_revoke.status_code == 201
    assert r_amend.status_code in (201, 409)
    if r_amend.status_code == 201:
        # 修订先提交：修订版播发过，随后被撤销
        assert broadcast(client, a) == []
        assert broadcast(client, a, at=r_amend.json()["amended_at"])[0]["message"] \
            == amend_body["message"]
    else:
        # 撤销先提交：无修订版本残留，历史仍为原版
        assert "撤销" in r_amend.json()["detail"]
        assert broadcast(client, a, at=pub["effective_from"])[0]["message"] \
            == "测试告警：请立即疏散"


# ---------------------------------------------------------------- 修订的幂等与冲突

def test_amend_idempotent_replay(client):
    pub = do_publish(client).json()
    body = amend_payload(pub["alert_no"])
    r1 = client.post("/api/commands/amend", json=body)
    assert r1.status_code == 201
    r2 = client.post("/api/commands/amend", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_amend_replay_after_revoke_still_returns_first_receipt(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=3600)).json()
    body = amend_payload(pub["alert_no"], areas=[a])
    r1 = client.post("/api/commands/amend", json=body)
    assert r1.status_code == 201
    client.post("/api/commands/revoke",
                json={"request_no": rid(), "alert_no": pub["alert_no"]})
    # 告警此后被撤销，同请求号重放仍返回首次修订回执
    r2 = client.post("/api/commands/amend", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_amend_replay_after_expiry_still_returns_first_receipt(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=1)).json()
    body = amend_payload(pub["alert_no"], areas=[a])
    r1 = client.post("/api/commands/amend", json=body)
    assert r1.status_code == 201
    time.sleep(1.2)
    r2 = client.post("/api/commands/amend", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_revoke_replay_after_expiry_still_returns_first_receipt(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=1)).json()
    body = {"request_no": rid(), "alert_no": pub["alert_no"]}
    r1 = client.post("/api/commands/revoke", json=body)
    assert r1.status_code == 201
    time.sleep(1.2)
    r2 = client.post("/api/commands/revoke", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_amend_same_request_no_different_payload_conflicts(client):
    pub = do_publish(client).json()
    body = amend_payload(pub["alert_no"], message="文案 A")
    assert client.post("/api/commands/amend", json=body).status_code == 201
    body2 = dict(body, message="文案 B")
    r = client.post("/api/commands/amend", json=body2)
    assert r.status_code == 409


def test_amend_request_no_conflict_across_command_types(client):
    pub = do_publish(client).json()
    body = amend_payload(pub["alert_no"])
    assert client.post("/api/commands/amend", json=body).status_code == 201
    r = client.post("/api/commands/revoke",
                    json={"request_no": body["request_no"], "alert_no": pub["alert_no"]})
    assert r.status_code == 409


def test_concurrent_identical_amend_single_accept(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], duration_seconds=3600)).json()
    body = amend_payload(pub["alert_no"], areas=[a])

    def send():
        return client.post("/api/commands/amend", json=body)

    with ThreadPoolExecutor(max_workers=8) as ex:
        rs = list(ex.map(lambda _: send(), range(8)))
    codes = sorted(r.status_code for r in rs)
    assert codes.count(201) == 1, codes
    assert codes.count(200) == 7, codes
    bodies = {json.dumps(r.json(), sort_keys=True) for r in rs}
    assert len(bodies) == 1


# ---------------------------------------------------------------- 修订入参与失败原子性

@pytest.mark.parametrize("override", [
    {"areas": []},
    {"areas": ["A", "A"]},
    {"areas": ["  "]},
    {"message": ""},
    {"message": "   "},
    {"level": 0},
    {"level": 6},
    {"alert_no": 0},
    {"request_no": ""},
    {"duration_seconds": 60},                 # 修订不接受延迟/持续字段
    {"delay_seconds": 0},
    {"unknown_field": 1},
])
def test_invalid_amend_rejected(client, override):
    pub = do_publish(client).json()
    r = client.post("/api/commands/amend", json=amend_payload(pub["alert_no"], **override))
    assert r.status_code == 422, (override, r.text)


def test_failed_amend_leaves_no_state_and_frees_request_no(client):
    a = area()
    pub = client.post("/api/commands/publish", json=publish_payload(
        areas=[a], level=3, message="原版", duration_seconds=3600)).json()

    # 422：空白文案
    bad422 = amend_payload(pub["alert_no"], areas=[a], message="   ")
    assert client.post("/api/commands/amend", json=bad422).status_code == 422

    # 404：不存在的告警
    bad404 = amend_payload(next_alert_no(), areas=[a])
    assert client.post("/api/commands/amend", json=bad404).status_code == 404

    # 失败命令不占请求号：同号换载荷可受理（422 的号）
    ok = amend_payload(pub["alert_no"], areas=[a], request_no=bad422["request_no"],
                       message="真正修订")
    assert client.post("/api/commands/amend", json=ok).status_code == 201

    # 当前播发只体现成功的那一次修订；原版历史不变
    assert broadcast(client, a)[0]["message"] == "真正修订"
    assert broadcast(client, a, at=pub["effective_from"])[0]["message"] == "原版"
