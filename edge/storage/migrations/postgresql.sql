-- TERRANET EDGE — esquema local (PostgreSQL, referência de produção).

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    category TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_identifier TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL DEFAULT '1.0',
    manufacturer TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    hardware_version TEXT NOT NULL DEFAULT '',
    firmware_version TEXT NOT NULL DEFAULT '',
    communication_profile JSONB NOT NULL DEFAULT '{}',
    capability_profile JSONB NOT NULL DEFAULT '[]',
    operational_status TEXT NOT NULL DEFAULT 'REGISTERED',
    configuration_version INTEGER NOT NULL DEFAULT 1,
    installed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}',
    last_seen_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_type, source_identifier)
);

CREATE TABLE IF NOT EXISTS device_lifecycle_log (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    event_type TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON telemetry_events (device_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_tenant_time ON telemetry_events (tenant_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS sync_outbox (
    event_id TEXT PRIMARY KEY REFERENCES telemetry_events(event_id),
    status TEXT NOT NULL DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    last_attempt_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    synchronized_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON sync_outbox (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS dead_letters (
    id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    payload TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS device_state (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    observed_at TIMESTAMPTZ NOT NULL,
    measurements JSONB NOT NULL,
    quality JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_rules (
    rule_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    definition JSONB NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    safety_critical BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_history (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    version INTEGER,
    definition JSONB,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_state (
    rule_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (rule_id, device_id)
);

CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at);
