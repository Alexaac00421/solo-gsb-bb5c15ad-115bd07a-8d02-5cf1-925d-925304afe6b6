"""播发裁决核心逻辑：命令受理（幂等）、撤销、按时刻裁决查询。

事务约定：
- 每条命令在单个数据库事务内完成「回执登记 + 业务写入」，任一步失败整体回滚，
  不留下部分状态；失败的命令不占用 request_no。
- 幂等通过 commands.request_no 主键 + INSERT ... ON CONFLICT DO NOTHING 实现：
  并发下同请求号只有一个事务真正写入，其余阻塞至其提交后读取首次回执。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .db import get_pool
from .schemas import PublishCommand, RevokeCommand


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

        # 重复撤销保留最早的终止时刻，避免“复活”已终止时段
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


def query_broadcast(at: datetime | None, area: str | None) -> dict[str, Any]:
    """裁决查询：指定时刻各区域应播发的告警。

    同一区域多条同时有效时，按「级别高 → 受理时间早 → 告警号小」取一条；
    高级别告警到期/被撤销后，仍有效的低级别告警自然恢复（纯时间窗口判定）。
    """
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    sql = """
        SELECT DISTINCT ON (aa.area)
               aa.area, a.alert_no, a.level, a.message,
               a.accepted_at, a.start_at, a.end_at
        FROM alert_areas aa
        JOIN alerts a ON a.alert_no = aa.alert_no
        WHERE a.start_at <= %(moment)s
          AND %(moment)s < a.end_at
          AND (a.revoked_at IS NULL OR %(moment)s < a.revoked_at)
    """
    params: dict[str, Any] = {"moment": moment}
    if area is not None:
        sql += " AND aa.area = %(area)s"
        params["area"] = area
    sql += " ORDER BY aa.area, a.level DESC, a.accepted_at ASC, a.alert_no ASC"

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
