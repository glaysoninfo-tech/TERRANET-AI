-- TERRANET CORE — esquema SQLite (testes e desenvolvimento local).

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    name TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_ids TEXT NOT NULL DEFAULT '[]',
    token_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    config_version INTEGER NOT NULL DEFAULT 1,
    applied_config_version INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at TEXT,
    last_metrics TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL
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
    communication_profile TEXT NOT NULL DEFAULT '{}',
    capability_profile TEXT NOT NULL DEFAULT '[]',
    operational_status TEXT NOT NULL DEFAULT 'REGISTERED',
    configuration_version INTEGER NOT NULL DEFAULT 1,
    installed_at TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    last_seen_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (source_type, source_identifier)
);

CREATE TABLE IF NOT EXISTS device_lifecycle_log (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_id TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    event_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    ingested_at TEXT NOT NULL,
    edge_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_core_telemetry_device_time
    ON telemetry_events (device_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_core_telemetry_tenant_time
    ON telemetry_events (tenant_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS device_state (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    tenant_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    measurements TEXT NOT NULL,
    quality TEXT NOT NULL,
    location TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_positions (
    event_id TEXT PRIMARY KEY REFERENCES telemetry_events(event_id),
    tenant_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    observed_at TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt_m REAL,
    speed_kmh REAL,
    heading_deg REAL
);
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
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_edge_state ON commands (edge_id, state);
CREATE INDEX IF NOT EXISTS idx_commands_tenant ON commands (tenant_id, issued_at DESC);

CREATE TABLE IF NOT EXISTS automation_rules (
    rule_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    site_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_reports (
    id TEXT PRIMARY KEY,
    edge_id TEXT NOT NULL REFERENCES edges(edge_id),
    intended_version INTEGER NOT NULL,
    applied_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    reported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    tenant_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_core_audit_tenant_at ON audit_log (tenant_id, at DESC);

-- Evidências rastreáveis de execução de testes (AUD-001 §7, impede falsa aprovação GO/NO-GO)
CREATE TABLE IF NOT EXISTS test_executions (
    execution_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    test_code TEXT NOT NULL,
    category TEXT NOT NULL,
    git_commit TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT '',
    equipment TEXT NOT NULL DEFAULT '',
    firmware_version TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    expected_result TEXT NOT NULL,
    observed_result TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}',
    approved_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_test_exec_tenant_code
    ON test_executions (tenant_id, test_code, finished_at DESC);
