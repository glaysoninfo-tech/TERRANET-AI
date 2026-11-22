-- Políticas opcionais de Row-Level Security (RLS) para isolamento multicliente no PostgreSQL (PIT-001 §11.2).
-- Em produção, a API executa `SET LOCAL app.tenant_id = '<tenant_id>'` por transação com role não-superuser.

ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE
    tbl TEXT;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'devices', 'telemetry_events', 'device_state',
        'telemetry_positions', 'commands', 'automation_rules', 'audit_log'
    ] LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true))',
            tbl
        );
    END LOOP;
END $$;
