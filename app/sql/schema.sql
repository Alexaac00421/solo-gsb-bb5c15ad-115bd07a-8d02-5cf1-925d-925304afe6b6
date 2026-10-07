-- 命令回执表：request_no 全局唯一，承载幂等语义。
-- 同一 request_no 重复提交时比对 payload，一致则返回首次 receipt，不一致报 409。
CREATE TABLE IF NOT EXISTS commands (
    request_no   TEXT PRIMARY KEY,
    command_type TEXT NOT NULL CHECK (command_type IN ('publish', 'revoke')),
    payload      JSONB NOT NULL,
    receipt      JSONB NOT NULL,
    accepted_at  TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 告警表：alert_no 主键即"不可复用"约束（含已撤销/已过期告警）。
-- 生效区间 [start_at, end_at)：start_at = accepted_at + delay_seconds，
-- end_at = start_at + duration_seconds。revoked_at 之后的时刻不再播发。
CREATE TABLE IF NOT EXISTS alerts (
    alert_no         BIGINT PRIMARY KEY CHECK (alert_no > 0),
    level            INTEGER NOT NULL CHECK (level BETWEEN 1 AND 5),
    message          TEXT NOT NULL CHECK (length(btrim(message)) > 0),
    delay_seconds    INTEGER NOT NULL CHECK (delay_seconds >= 0),
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
    accepted_at      TIMESTAMPTZ NOT NULL,
    start_at         TIMESTAMPTZ NOT NULL,
    end_at           TIMESTAMPTZ NOT NULL,
    revoked_at       TIMESTAMPTZ,
    request_no       TEXT NOT NULL REFERENCES commands (request_no)
);

-- 告警覆盖区域：与告警同事务写入，同成同败。
CREATE TABLE IF NOT EXISTS alert_areas (
    alert_no BIGINT NOT NULL REFERENCES alerts (alert_no) ON DELETE CASCADE,
    area     TEXT NOT NULL,
    PRIMARY KEY (alert_no, area)
);

CREATE INDEX IF NOT EXISTS idx_alert_areas_area ON alert_areas (area);
CREATE INDEX IF NOT EXISTS idx_alerts_window ON alerts (start_at, end_at);
