"""播发裁决核心逻辑：命令受理（幂等）、撤销、修订、按时刻裁决查询。

事务约定：
- 每条命令在单个数据库事务内完成「回执登记 + 业务写入」，任一步失败整体回滚，
  不留下部分状态；失败的命令不占用 request_no。
- 幂等通过 commands.request_no 主键 + INSERT ... ON CONFLICT DO NOTHING 实现：
  并发下同请求号只有一个事务真正写入，其余阻塞至其提交后读取首次回执。

修订语义：
- 修订从受理时刻起整体替换播发版本（文案/级别/区域），不改变原生效终点 end_at，
  也不改变修订前历史；查询某时刻时按该时刻的最新版本裁决。
- 仅「已生效（start_at <= now）、未到期（now < end_at）、未撤销」的告警可修订，
  其余（不存在/未生效/已到期/已撤销）拒绝（404/409）。
- 同告警的修订与撤销通过 SELECT ... FOR UPDATE 串行化，按成功提交顺序生效：
  撤销先提交则后续修订失败；修订先提交后撤销，修订期间的历史仍可查询。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .db import get_pool
from .schemas import PublishCommand, ReviseCommand, RevokeCommand


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _idempotent_insert(
    conn,
    request_no: str,
    command_type: str,
    payload: dict[str, Any],
    receipt: dict[str, Any],
    accepted_at: datetime,
) -> tuple[bool, dict[str, Any] | None]:
    """尝试登记命令。返回 (是否首次登记, 重放时的首次回执)。

    - 首次登记：返回 (True, None)，调用方继续业务写入。
    - 载荷一致的重复提交：返回 (False, 首次回执)。
    - 载荷不一致：抛 409。
    """
    inserted = conn.execute(
        """
        INSERT INTO commands (request_no, command_type, payload, receipt, accepted_at)
        VALUES (%(request_no)s, %(command_type)s, %(payload)s, %(receipt)s, %(accepted_at)s)
        ON CONFLICT (request_no) DO NOTHING
        RETURNING request_no
        """,
        {
            "request_no": request_no,
            "command_type": command_type,
            "payload": Jsonb(payload),
            "receipt": Jsonb(receipt),
            "accepted_at": accepted_at,
        },
    ).fetchone()
    if inserted is not None:
        return True, None

    existing = conn.execute(
        "SELECT payload, receipt FROM commands WHERE request_no = %s",
        (request_no,),
    ).fetchone()
    if existing["payload"] == payload:
        return False, existing["receipt"]
    raise HTTPException(
        status_code=409,
        detail=f"请求号 {request_no!r} 已被不同载荷的命令使用，拒绝重复利用",
    )


def handle_publish(cmd: PublishCommand) -> tuple[dict[str, Any], int]:
    """受理发布命令，返回 (回执, HTTP 状态码)。重放返回首次回执与 200。"""
    accepted_at = datetime.now(timezone.utc)
    start_at = accepted_at + timedelta(seconds=cmd.delay_seconds)
    end_at = start_at + timedelta(seconds=cmd.duration_seconds)

    payload = {"type": "publish", **cmd.model_dump(mode="json")}
    receipt: dict[str, Any] = {
        "request_no": cmd.request_no,
        "command_type": "publish",
        "status": "accepted",
        "alert_no": cmd.alert_no,
        "level": cmd.level,
        "message": cmd.message,
        "areas": cmd.areas,
        "delay_seconds": cmd.delay_seconds,
        "duration_seconds": cmd.duration_seconds,
        "accepted_at": _iso(accepted_at),
        "effective_from": _iso(start_at),
        "effective_to": _iso(end_at),
    }

    with get_pool().connection() as conn:  # 正常退出自动提交，异常自动回滚
        first, replay_receipt = _idempotent_insert(
            conn, cmd.request_no, "publish", payload, receipt, accepted_at
        )
        if not first:
            return replay_receipt, 200

        try:
            conn.execute(
                """
                INSERT INTO alerts (alert_no, level, message, delay_seconds, duration_seconds,
                                    accepted_at, start_at, end_at, request_no)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cmd.alert_no,
                    cmd.level,
                    cmd.message,
                    cmd.delay_seconds,
                    cmd.duration_seconds,
                    accepted_at,
                    start_at,
                    end_at,
                    cmd.request_no,
                ),
            )
        except UniqueViolation as exc:
            raise HTTPException(
                status_code=409,
                detail=f"告警号 {cmd.alert_no} 已被使用，告警号不可复用",
            ) from exc

        # 区域与告警同事务写入，同成同败
        conn.execute(
            "INSERT INTO alert_areas (alert_no, area) SELECT %s, t.area FROM unnest(%s::text[]) AS t(area)",
            (cmd.alert_no, cmd.areas),
        )

    return receipt, 201


def handle_revoke(cmd: RevokeCommand) -> tuple[dict[str, Any], int]:
    """受理撤销命令：按受理时间截断告警的后续播发，历史时刻不受影响。"""
    accepted_at = datetime.now(timezone.utc)

    payload = {"type": "revoke", **cmd.model_dump(mode="json")}
    receipt: dict[str, Any] = {
        "request_no": cmd.request_no,
        "command_type": "revoke",
        "status": "accepted",
        "alert_no": cmd.alert_no,
        "accepted_at": _iso(accepted_at),
        "revoked_at": _iso(accepted_at),
    }

    with get_pool().connection() as conn:
        first, replay_receipt = _idempotent_insert(
            conn, cmd.request_no, "revoke", payload, receipt, accepted_at
        )
        if not first:
            return replay_receipt, 200

        # 重复撤销保留最早的终止时刻，避免“复活”已终止时段。
        # UPDATE 自身持有行锁：与修订事务在同一告警行上串行化，按提交顺序生效。
        row = conn.execute(
            """
            UPDATE alerts
            SET revoked_at = LEAST(COALESCE(revoked_at, %s), %s)
            WHERE alert_no = %s
            RETURNING revoked_at
            """,
            (accepted_at, accepted_at, cmd.alert_no),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"告警号 {cmd.alert_no} 不存在，无法撤销",
            )

        effective_revoked_at = row["revoked_at"]
        if effective_revoked_at != accepted_at:
            # 该告警此前已被更早撤销，回执如实反映实际生效的终止时刻
            receipt["revoked_at"] = _iso(effective_revoked_at)
            conn.execute(
                "UPDATE commands SET receipt = %s WHERE request_no = %s",
                (Jsonb(receipt), cmd.request_no),
            )

    return receipt, 201


def handle_revise(cmd: ReviseCommand) -> tuple[dict[str, Any], int]:
    """受理修订命令：从受理时刻起整体替换播发版本。

    前置条件（不满足则拒绝，且不占请求号、不留部分状态）：
    - 告警不存在 → 404；
    - 已撤销（revoked_at <= 受理时刻）→ 409；
    - 尚未生效（受理时刻 < start_at）或已到期（end_at <= 受理时刻）→ 409。
    """
    accepted_at = datetime.now(timezone.utc)

    payload = {"type": "revise", **cmd.model_dump(mode="json")}
    receipt: dict[str, Any] = {
        "request_no": cmd.request_no,
        "command_type": "revise",
        "status": "accepted",
        "alert_no": cmd.alert_no,
        "level": cmd.level,
        "message": cmd.message,
        "areas": cmd.areas,
        "accepted_at": _iso(accepted_at),
        "revised_at": _iso(accepted_at),
        "effective_from": None,  # 以下从事务内读取的告警原生效区间补齐
        "effective_to": None,
    }

    with get_pool().connection() as conn:
        first, replay_receipt = _idempotent_insert(
            conn, cmd.request_no, "revise", payload, receipt, accepted_at
        )
        if not first:
            return replay_receipt, 200

        # 锁定告警行：与撤销（以及其他修订）串行化，按成功提交顺序决定结果。
        alert = conn.execute(
            "SELECT start_at, end_at, revoked_at FROM alerts WHERE alert_no = %s FOR UPDATE",
            (cmd.alert_no,),
        ).fetchone()
        if alert is None:
            raise HTTPException(
                status_code=404,
                detail=f"告警号 {cmd.alert_no} 不存在，无法修订",
            )
        if alert["revoked_at"] is not None and alert["revoked_at"] <= accepted_at:
            raise HTTPException(
                status_code=409,
                detail=f"告警号 {cmd.alert_no} 已撤销，无法修订",
            )
        if accepted_at < alert["start_at"]:
            raise HTTPException(
                status_code=409,
                detail=f"告警号 {cmd.alert_no} 尚未生效，无法修订",
            )
        if alert["end_at"] <= accepted_at:
            raise HTTPException(
                status_code=409,
                detail=f"告警号 {cmd.alert_no} 已到期，无法修订",
            )

        # 回执保留原生效区间：修订不改变生效终点，亦不改起点。
        receipt["effective_from"] = _iso(alert["start_at"])
        receipt["effective_to"] = _iso(alert["end_at"])

        revision_row = conn.execute(
            """
            INSERT INTO alert_revisions (alert_no, level, message, accepted_at, request_no)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (cmd.alert_no, cmd.level, cmd.message, accepted_at, cmd.request_no),
        ).fetchone()

        # 修订区域与修订记录同事务写入，同成同败
        conn.execute(
            """
            INSERT INTO alert_revision_areas (revision_id, area)
            SELECT %s, t.area FROM unnest(%s::text[]) AS t(area)
            """,
            (revision_row["id"], cmd.areas),
        )

        # 回执落库为补齐生效区间后的最终内容，重放时原样返回
        conn.execute(
            "UPDATE commands SET receipt = %s WHERE request_no = %s",
            (Jsonb(receipt), cmd.request_no),
        )

    return receipt, 201


# 版本集合：v0 为发布版本（起播 start_at），其后为每次修订（起播修订受理时刻）。
# alert_levels 先按告警取「查询时刻适用版本」，再展开区域，供外层按区域裁决。
_BROADCAST_SQL = """
WITH alert_versions AS (
    SELECT a.alert_no, a.level, a.message, a.accepted_at AS publish_accepted_at,
           a.start_at, a.end_at, a.revoked_at,
           a.start_at AS content_from, 0 AS ver, NULL::bigint AS revision_id
    FROM alerts a
    UNION ALL
    SELECT a.alert_no, r.level, r.message, a.accepted_at AS publish_accepted_at,
           a.start_at, a.end_at, a.revoked_at,
           r.accepted_at AS content_from, 1 AS ver, r.id AS revision_id
    FROM alert_revisions r
    JOIN alerts a ON a.alert_no = r.alert_no
),
version_spans AS (
    SELECT alert_no, level, message, publish_accepted_at, start_at, end_at, revoked_at,
           content_from, ver, revision_id,
           LEAD(content_from) OVER (
               PARTITION BY alert_no ORDER BY content_from, ver
           ) AS content_to
    FROM alert_versions
),
alert_levels AS (
    SELECT DISTINCT ON (vs.alert_no)
           vs.alert_no, vs.level, vs.message, vs.publish_accepted_at,
           vs.start_at, vs.end_at, vs.ver, vs.revision_id
    FROM version_spans vs
    WHERE vs.start_at <= %(moment)s
      AND %(moment)s < vs.end_at
      AND (vs.revoked_at IS NULL OR %(moment)s < vs.revoked_at)
      AND vs.content_from <= %(moment)s
      AND (vs.content_to IS NULL OR %(moment)s < vs.content_to)
    ORDER BY vs.alert_no, vs.content_from DESC, vs.ver DESC
),
alert_area_rows AS (
    SELECT al.alert_no, al.level, al.message, al.publish_accepted_at,
           al.start_at, al.end_at, al.ver, aa.area
    FROM alert_levels al
    JOIN alert_areas aa ON aa.alert_no = al.alert_no
    WHERE al.ver = 0
    UNION ALL
    -- 必须挂接“该时刻被选中的那一版”修订（revision_id），不能取最新版，
    -- 否则查询两次修订之间的历史时刻会错误使用未来版本的区域。
    SELECT al.alert_no, al.level, al.message, al.publish_accepted_at,
           al.start_at, al.end_at, al.ver, ara.area
    FROM alert_levels al
    JOIN alert_revision_areas ara ON ara.revision_id = al.revision_id
    WHERE al.ver > 0
)
SELECT DISTINCT ON (aar.area)
       aar.area, aar.alert_no, aar.level, aar.message,
       aar.publish_accepted_at AS accepted_at, aar.start_at, aar.end_at
FROM alert_area_rows aar
WHERE TRUE {area_filter}
ORDER BY aar.area, aar.level DESC, aar.publish_accepted_at ASC, aar.alert_no ASC
"""


def query_broadcast(at: datetime | None, area: str | None) -> dict[str, Any]:
    """裁决查询：指定时刻各区域应播发的告警。

    同一区域多条同时有效时，按「级别高 → 发布受理时间早 → 告警号小」取一条；
    高级别告警到期/被撤销后，仍有效的低级别告警自然恢复（纯时间窗口判定）。
    文案/级别/区域均取查询时刻的适用版本（修订整体替换，历史版本仍可按
    历史时刻查到）。
    """
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    sql = _BROADCAST_SQL.format(
        area_filter=" AND aar.area = %(area)s" if area is not None else ""
    )
    params: dict[str, Any] = {"moment": moment}
    if area is not None:
        params["area"] = area

    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    return {
        "at": _iso(moment),
        "count": len(rows),
        "results": [
            {
                "area": r["area"],
                "alert_no": r["alert_no"],
                "level": r["level"],
                "message": r["message"],
                "accepted_at": _iso(r["accepted_at"]),
                "effective_from": _iso(r["start_at"]),
                "effective_to": _iso(r["end_at"]),
            }
            for r in rows
        ],
    }
