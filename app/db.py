"""数据库连接池与 schema 初始化。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from psycopg import connect
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://broadcast:broadcast@localhost:5432/broadcast",
)

_pool: ConnectionPool | None = None


def init_pool(retries: int = 30, delay: float = 1.0) -> None:
    """等待数据库就绪后创建连接池（容器编排下 DB 可能慢于应用启动）。"""
    global _pool
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            with connect(DATABASE_URL, connect_timeout=2) as probe:
                probe.execute("SELECT 1")
            break
        except Exception as exc:  # noqa: BLE001 - 启动期重试任意连接错误
            last_exc = exc
            time.sleep(delay)
    else:
        raise RuntimeError(f"无法连接数据库: {last_exc}")
    _pool = ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
    )


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("连接池尚未初始化")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_schema() -> None:
    """执行建表脚本（幂等，可重复启动）。"""
    sql = (Path(__file__).parent / "sql" / "schema.sql").read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        conn.execute(sql)
