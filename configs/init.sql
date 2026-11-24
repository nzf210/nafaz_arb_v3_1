-- nafaz_arb v3.1 PostgreSQL persistent schema

CREATE TABLE IF NOT EXISTS runtime_settings (
    profile_id TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    canary_max_notional_usdt DOUBLE PRECISION,
    max_notional_usdt DOUBLE PRECISION,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO runtime_settings (profile_id, enabled, dry_run, canary_max_notional_usdt, max_notional_usdt)
VALUES
    ('funding_carry_core', TRUE, TRUE, 20.0, 1000.0),
    ('basis_convergence_fast', TRUE, TRUE, 20.0, 1000.0)
ON CONFLICT (profile_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS signal_audit (
    signal_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    route_type TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    target_notional_usdt DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    payload JSONB NOT NULL,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trade_executions (
    id BIGSERIAL PRIMARY KEY,
    signal_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    base_asset TEXT NOT NULL,
    status TEXT NOT NULL,
    dry_run BOOLEAN NOT NULL,
    qty DOUBLE PRECISION NOT NULL DEFAULT 0,
    effective_notional_usdt DOUBLE PRECISION NOT NULL DEFAULT 0,
    leg_a JSONB NOT NULL,
    leg_b JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS active_positions (
    base_asset TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    qty DOUBLE PRECISION NOT NULL,
    leg_a JSONB NOT NULL,
    leg_b JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS risk_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
