-- TERRANET CORE — esquema PostgreSQL + PostGIS (ADR-007, ADR-008, PIT-001 §11).

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    name TEXT NOT NULL,
    location JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_ids JSONB NOT NULL DEFAULT '[]',
    token_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    config_version INTEGER NOT NULL DEFAULT 1,
    applied_config_version INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at TIMESTAMPTZ,
    last_metrics JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
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
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_id TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    event_type TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL,
    edge_id TEXT,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_core_telemetry_device_time
    ON telemetry_events (device_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_core_telemetry_tenant_time
    ON telemetry_events (tenant_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS device_state (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    tenant_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    measurements JSONB NOT NULL,
    quality JSONB NOT NULL,
    location JSONB,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_positions (
    event_id TEXT PRIMARY KEY REFERENCES telemetry_events(event_id),
    tenant_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    observed_at TIMESTAMPTZ NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    alt_m DOUBLE PRECISION,
    speed_kmh DOUBLE PRECISION,
    heading_deg DOUBLE PRECISION,
    geom GEOGRAPHY(Point, 4326) GENERATED ALWAYS AS (
        ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography
    ) STORED
);
CREATE INDEX IF NOT EXISTS idx_positions_geom ON telemetry_positions USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_positions_device_time
    ON telemetry_positions (device_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_positions_tenant_time
    ON telemetry_positions (tenant_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_id TEXT NOT NULL,
    edge_id TEXT NOT NULL REFERENCES edges(edge_id),
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    operation TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_edge_state ON commands (edge_id, state);
CREATE INDEX IF NOT EXISTS idx_commands_tenant ON commands (tenant_id, issued_at DESC);

CREATE TABLE IF NOT EXISTS automation_rules (
    rule_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition JSONB NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS config_reports (
    id TEXT PRIMARY KEY,
    edge_id TEXT NOT NULL REFERENCES edges(edge_id),
    intended_version INTEGER NOT NULL,
    applied_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    reported_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL,
    tenant_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_core_audit_tenant_at ON audit_log (tenant_id, at DESC);

CREATE TABLE IF NOT EXISTS test_executions (
    execution_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    test_code TEXT NOT NULL,
    category TEXT NOT NULL,
    git_commit TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '',
    equipment TEXT NOT NULL DEFAULT '',
    firmware_version TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    expected_result TEXT NOT NULL,
    observed_result TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}',
    approved_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_test_exec_tenant_code
    ON test_executions (tenant_id, test_code, finished_at DESC);
