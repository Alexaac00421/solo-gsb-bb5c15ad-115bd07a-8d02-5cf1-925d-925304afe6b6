-- 命令回执表：request_no 全局唯一，承载幂等语义。
-- 同一 request_no 重复提交时比对 payload，一致则返回首次 receipt，不一致报 409。
CREATE TABLE IF NOT EXISTS commands (
    request_no   TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    payload      JSONB NOT NULL,
    receipt      JSONB NOT NULL,
    accepted_at  TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- command_type 取值约束（独立命名约束，幂等迁移：老库的行内 CHECK 自动改名重建）。
-- amend：修订命令，对已生效告警整体替换播发版本（文案/级别/区域），不改生效终点。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'commands_command_type_check_v2'
    ) THEN
        ALTER TABLE commands DROP CONSTRAINT IF EXISTS commands_command_type_check;
        ALTER TABLE commands ADD CONSTRAINT commands_command_type_check_v2
            CHECK (command_type IN ('publish', 'revoke', 'amend'));
    END IF;
END $$;

-- 告警表：alert_no 主键即"不可复用"约束（含已撤销/已过期告警）。
-- 生效区间 [start_at, end_at)：start_at = accepted_at + delay_seconds，
-- end_at = start_at + duration_seconds。revoked_at 之后的时刻不再播发。
-- level/message 为当前（最新修订后）快照，历史版本见 alert_versions。
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
-- 每次修订在同一事务内整体替换为当版区域；历史区域见 alert_area_versions。
CREATE TABLE IF NOT EXISTS alert_areas (
    alert_no BIGINT NOT NULL REFERENCES alerts (alert_no) ON DELETE CASCADE,
    area     TEXT NOT NULL,
    PRIMARY KEY (alert_no, area)
);

-- 告警内容版本：version_no=0 为发布原版；每次受理修订追加一行。
-- 该版从 valid_from（=修订受理时刻，0 版为告警起点）起播发，
-- 直至下一版的 valid_from（末版到 end_at）；修订不改变 end_at 与撤销时刻。
CREATE TABLE IF NOT EXISTS alert_versions (
    alert_no   BIGINT NOT NULL REFERENCES alerts (alert_no) ON DELETE CASCADE,
    version_no INTEGER NOT NULL CHECK (version_no >= 0),
    level      INTEGER NOT NULL CHECK (level BETWEEN 1 AND 5),
    message    TEXT NOT NULL CHECK (length(btrim(message)) > 0),
    valid_from TIMESTAMPTZ NOT NULL,
    request_no TEXT NOT NULL REFERENCES commands (request_no),
    PRIMARY KEY (alert_no, version_no)
);

CREATE INDEX IF NOT EXISTS idx_alert_versions_from
    ON alert_versions (alert_no, valid_from DESC, version_no DESC);

-- 告警区域版本：与 alert_versions 一一对应，保存该版覆盖区域，支撑按时刻还原。
CREATE TABLE IF NOT EXISTS alert_area_versions (
    alert_no   BIGINT NOT NULL REFERENCES alerts (alert_no) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    area       TEXT NOT NULL,
    PRIMARY KEY (alert_no, version_no, area),
    FOREIGN KEY (alert_no, version_no)
        REFERENCES alert_versions (alert_no, version_no) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_alert_area_versions_area
    ON alert_area_versions (area);

CREATE INDEX IF NOT EXISTS idx_alert_areas_area ON alert_areas (area);
CREATE INDEX IF NOT EXISTS idx_alerts_window ON alerts (start_at, end_at);

-- 老库升级：为修订功能上线前已存在的告警补齐 0 版内容与区域（幂等）。
INSERT INTO alert_versions (alert_no, version_no, level, message, valid_from, request_no)
SELECT a.alert_no, 0, a.level, a.message, a.start_at, a.request_no
FROM alerts a
ON CONFLICT (alert_no, version_no) DO NOTHING;

INSERT INTO alert_area_versions (alert_no, version_no, area)
SELECT aa.alert_no, 0, aa.area
FROM alert_areas aa
ON CONFLICT (alert_no, version_no, area) DO NOTHING;
