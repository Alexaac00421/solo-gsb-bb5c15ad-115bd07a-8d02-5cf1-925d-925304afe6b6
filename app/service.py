"""播发裁决核心逻辑：命令受理（幂等）、修订、撤销、按时刻裁决查询。

事务约定：
- 每条命令在单个数据库事务内完成「回执登记 + 业务写入」，任一步失败整体回滚，
  不留下部分状态；失败的命令不占用 request_no。
- 幂等通过 commands.request_no 主键 + INSERT ... ON CONFLICT DO NOTHING 实现：
  并发下同请求号只有一个事务真正写入，其余阻塞至其提交后读取首次回执。
- 修订以 alert_versions/alert_area_versions 追加版本、整体替换当前快照实现：
  受理时刻起播新版，历史时刻经版本表还原；修订不改变 end_at 与已撤销时刻。
- 修订/撤销先以 SELECT ... FOR UPDATE 锁定 alerts 行再写入，同告警命令按
  成功提交顺序串行生效；锁等待后的受理时刻以数据库 clock_timestamp() 为准。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from psycopg.errors import DeadlockDetected, UniqueViolation
from psycopg.types.json import Jsonb

from .db import get_pool
from .schemas import AmendCommand, PublishCommand, RevokeCommand


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

        # 0 版内容/区域：历史按时刻裁决的基准（与上方写入同一事务）
        conn.execute(
            """
            INSERT INTO alert_versions
                (alert_no, version_no, level, message, valid_from, request_no)
            VALUES (%s, 0, %s, %s, %s, %s)
            """,
            (cmd.alert_no, cmd.level, cmd.message, start_at, cmd.request_no),
        )
        conn.execute(
            """
            INSERT INTO alert_area_versions (alert_no, version_no, area)
            SELECT %s, 0, t.area FROM unnest(%s::text[]) AS t(area)
            """,
            (cmd.alert_no, cmd.areas),
        )

    return receipt, 201


def _lock_alert(conn, alert_no: int):
    """锁定告警行（无此告警返回 None）。修订/撤销借此按提交顺序串行生效。"""
    return conn.execute(
        "SELECT start_at, end_at, revoked_at FROM alerts WHERE alert_no = %s FOR UPDATE",
        (alert_no,),
    ).fetchone()


def _db_now(conn) -> datetime:
    """取数据库时钟时刻（clock_timestamp 反映锁等待后的真实时刻，而非事务起点）。"""
    return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def handle_amend(cmd: AmendCommand) -> tuple[dict[str, Any], int]:
    """受理修订命令：从受理时刻起整体替换播发版本，不改生效终点与修订前历史。

    仅“已生效、未到期、未撤销”的告警可修订；不存在报 404，状态不符报 409。
    资源获取顺序与撤销一致（先登记命令、再锁告警行），同告警的修订/撤销按
    成功提交顺序串行生效，且不会形成锁等待环。任一校验失败都在事务内回滚，
    不占请求号、不留半态。
    """
    accepted_at = datetime.now(timezone.utc)

    payload = {"type": "amend", **cmd.model_dump(mode="json")}
    receipt: dict[str, Any] = {
        "request_no": cmd.request_no,
        "command_type": "amend",
        "status": "accepted",
        "alert_no": cmd.alert_no,
        "level": cmd.level,
        "message": cmd.message,
        "areas": cmd.areas,
        "accepted_at": _iso(accepted_at),
        "amended_at": _iso(accepted_at),
        "effective_from": _iso(accepted_at),
        "effective_to": None,  # 取锁后以告警原终点回填
    }

    try:
        with get_pool().connection() as conn:  # 正常退出自动提交，异常自动回滚
            # 先登记命令（与撤销同序：同号异载荷 409 优先于状态/不存在判定；
            # 同号重放直接返回首次回执，不参与后续状态判定与锁竞争）
            first, replay_receipt = _idempotent_insert(
                conn, cmd.request_no, "amend", payload, receipt, accepted_at
            )
            if not first:
                return replay_receipt, 200

            # 再锁告警行：与同告警撤销竞争时在这里排队
            row = _lock_alert(conn, cmd.alert_no)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"告警号 {cmd.alert_no} 不存在，无法修订",
                )
            # 受理时刻以取锁后的数据库时钟为准：后提交者必然更晚，
            # 不会被锁等待前记下的时间戳排到竞争命令之前
            accepted_at = _db_now(conn)

            if row["revoked_at"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"告警号 {cmd.alert_no} 已撤销，无法修订",
                )
            if accepted_at < row["start_at"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"告警号 {cmd.alert_no} 尚未生效，无法修订",
                )
            if accepted_at >= row["end_at"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"告警号 {cmd.alert_no} 已到期，无法修订",
                )

            receipt.update(
                accepted_at=_iso(accepted_at),
                amended_at=_iso(accepted_at),
                effective_from=_iso(accepted_at),
                effective_to=_iso(row["end_at"]),
            )

            version_no = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no FROM alert_versions WHERE alert_no = %s",
                (cmd.alert_no,),
            ).fetchone()["next_no"]

            # 追加版本（历史行不变，裁决按时刻选取对应版本）
            conn.execute(
                """
                INSERT INTO alert_versions
                    (alert_no, version_no, level, message, valid_from, request_no)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (cmd.alert_no, version_no, cmd.level, cmd.message, accepted_at, cmd.request_no),
            )
            conn.execute(
                """
                INSERT INTO alert_area_versions (alert_no, version_no, area)
                SELECT %s, %s, t.area FROM unnest(%s::text[]) AS t(area)
                """,
                (cmd.alert_no, version_no, cmd.areas),
            )

            # 当前快照整体替换（与版本追加、回执落库同一事务，同成同败）
            conn.execute(
                "UPDATE alerts SET level = %s, message = %s WHERE alert_no = %s",
                (cmd.level, cmd.message, cmd.alert_no),
            )
            conn.execute("DELETE FROM alert_areas WHERE alert_no = %s", (cmd.alert_no,))
            conn.execute(
                "INSERT INTO alert_areas (alert_no, area) SELECT %s, t.area FROM unnest(%s::text[]) AS t(area)",
                (cmd.alert_no, cmd.areas),
            )
            conn.execute(
                "UPDATE commands SET receipt = %s, accepted_at = %s WHERE request_no = %s",
                (Jsonb(receipt), accepted_at, cmd.request_no),
            )
    except DeadlockDetected as exc:
        # 防御性兜底：正常加锁顺序下不应出现等待环
        raise HTTPException(
            status_code=409,
            detail=f"请求号 {cmd.request_no!r} 正与其他命令并发冲突，请重试",
        ) from exc

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

    try:
        with get_pool().connection() as conn:
            # 先登记命令（保持既有优先级：同号异载荷 409 优先于告警不存在 404）
            first, replay_receipt = _idempotent_insert(
                conn, cmd.request_no, "revoke", payload, receipt, accepted_at
            )
            if not first:
                return replay_receipt, 200

            # 再锁告警行：与同告警修订竞争时在这里排队，取锁后以数据库时钟为准，
            # 后提交者必然更晚，不会被锁等待前记下的时间戳“追溯终止”修订窗口。
            row = _lock_alert(conn, cmd.alert_no)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"告警号 {cmd.alert_no} 不存在，无法撤销",
                )
            locked_at = _db_now(conn)

            # 无既有撤销（含与修订竞争后由本方首次终止）→ 取锁后时刻，
            # 保证“修订先提交”时终止时刻不会早于修订受理时刻；
            # 已被更早撤销 → 保留更早终止时刻，避免“复活”已终止时段。
            updated = conn.execute(
                """
                UPDATE alerts
                SET revoked_at = CASE
                        WHEN revoked_at IS NULL THEN %s
                        ELSE LEAST(revoked_at, %s)
                    END
                WHERE alert_no = %s
                RETURNING revoked_at
                """,
                (locked_at, locked_at, cmd.alert_no),
            ).fetchone()
            effective_revoked_at = updated["revoked_at"]

            # 实际终止时刻以取锁后的数据库时钟为准；回执与命令记录如实反映
            if effective_revoked_at != accepted_at:
                receipt["accepted_at"] = _iso(effective_revoked_at)
                receipt["revoked_at"] = _iso(effective_revoked_at)
                conn.execute(
                    "UPDATE commands SET receipt = %s, accepted_at = %s WHERE request_no = %s",
                    (Jsonb(receipt), effective_revoked_at, cmd.request_no),
                )
    except DeadlockDetected as exc:
        # 仅当同请求号被异类型命令并发持锁时可能形成等待环；语义等同请求号冲突
        raise HTTPException(
            status_code=409,
            detail=f"请求号 {cmd.request_no!r} 正与其他命令并发冲突，请重试",
        ) from exc

    return receipt, 201


def query_broadcast(at: datetime | None, area: str | None) -> dict[str, Any]:
    """裁决查询：指定时刻各区域应播发的告警。

    同一区域多条同时有效时，按「级别高 → 受理时间早 → 告警号小」取一条；
    高级别告警到期/被撤销后，仍有效的低级别告警自然恢复（纯时间窗口判定）。
    被修订的告警按查询时刻取当时版本：区域与级别均来自该版本
    （新版自修订受理时刻起播发，旧版时刻仍返回旧文案/旧级别/旧区域）。
    """
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    # 先在 LATERAL 子查询中为每条有效告警选出查询时刻的版本，
    # 再按区域 DISTINCT ON 裁决，避免旧版级别/区域干扰现行裁决。
    sql = """
        SELECT DISTINCT ON (aav.area)
               aav.area, a.alert_no, av.level, av.message,
               a.accepted_at, a.start_at, a.end_at
        FROM alerts a
        JOIN LATERAL (
            SELECT v.alert_no, v.version_no, v.level, v.message
            FROM alert_versions v
            WHERE v.alert_no = a.alert_no AND v.valid_from <= %(moment)s
            ORDER BY v.valid_from DESC, v.version_no DESC
            LIMIT 1
        ) av ON true
        JOIN alert_area_versions aav
          ON aav.alert_no = av.alert_no AND aav.version_no = av.version_no
        WHERE a.start_at <= %(moment)s
          AND %(moment)s < a.end_at
          AND (a.revoked_at IS NULL OR %(moment)s < a.revoked_at)
    """
    params: dict[str, Any] = {"moment": moment}
    if area is not None:
        sql += " AND aav.area = %(area)s"
        params["area"] = area
    sql += " ORDER BY aav.area, av.level DESC, a.accepted_at ASC, a.alert_no ASC"

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
