-- nafaz_arb v3.1 PostgreSQL persistent schema

CREATE TABLE IF NOT EXISTS runtime_settings (
    profile_id TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    canary_max_notional_usdt DOUBLE PRECISION,
    max_notional_usdt DOUBLE PRECISION,
    target_hold_days DOUBLE PRECISION,
    basis_target_spread DOUBLE PRECISION,
    basis_adverse_stop_spread DOUBLE PRECISION,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS target_hold_days DOUBLE PRECISION;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS basis_target_spread DOUBLE PRECISION;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS basis_adverse_stop_spread DOUBLE PRECISION;

UPDATE runtime_settings
SET target_hold_days = CASE profile_id
    WHEN 'funding_carry_core' THEN 7.0
    WHEN 'basis_convergence_fast' THEN 1.0
    ELSE 3.0
END
WHERE target_hold_days IS NULL;

UPDATE runtime_settings
SET basis_target_spread = 0.001,
    basis_adverse_stop_spread = 0.005
WHERE profile_id = 'basis_convergence_fast'
  AND (basis_target_spread IS NULL OR basis_adverse_stop_spread IS NULL);

INSERT INTO runtime_settings (
    profile_id, enabled, dry_run, canary_max_notional_usdt, max_notional_usdt,
    target_hold_days, basis_target_spread, basis_adverse_stop_spread
)
VALUES
    ('funding_carry_core', TRUE, TRUE, 20.0, 1000.0, 7.0, NULL, NULL),
    ('basis_convergence_fast', TRUE, TRUE, 20.0, 1000.0, 1.0, 0.001, 0.005)
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    opened_at_ms BIGINT NOT NULL DEFAULT ((EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT),
    realized_pnl_usdt DOUBLE PRECISION,
    close_reason TEXT,
    closed_at TIMESTAMPTZ
);

ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS opened_at_ms BIGINT;
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS realized_pnl_usdt DOUBLE PRECISION;
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS close_reason TEXT;
ALTER TABLE active_positions ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
UPDATE active_positions
SET opened_at_ms = (EXTRACT(EPOCH FROM updated_at) * 1000)::BIGINT
WHERE opened_at_ms IS NULL;
ALTER TABLE active_positions ALTER COLUMN opened_at_ms SET DEFAULT ((EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT);
ALTER TABLE active_positions ALTER COLUMN opened_at_ms SET NOT NULL;

CREATE TABLE IF NOT EXISTS risk_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS closed_trades (
    id BIGSERIAL PRIMARY KEY,
    base_asset TEXT NOT NULL,
    profile_id TEXT,
    qty DOUBLE PRECISION NOT NULL DEFAULT 0,
    leg_a_exchange TEXT,
    leg_b_exchange TEXT,
    entry_price_a DOUBLE PRECISION,
    entry_price_b DOUBLE PRECISION,
    exit_price_a DOUBLE PRECISION,
    exit_price_b DOUBLE PRECISION,
    closed_qty_a DOUBLE PRECISION NOT NULL DEFAULT 0,
    closed_qty_b DOUBLE PRECISION NOT NULL DEFAULT 0,
    realized_pnl_usdt DOUBLE PRECISION,
    fees_usdt DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_pnl_usdt DOUBLE PRECISION,
    pnl_status TEXT NOT NULL DEFAULT 'unavailable',
    close_reason TEXT,
    opened_at_ms BIGINT,
    closed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_closed_trades_closed_at ON closed_trades (closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_closed_trades_asset ON closed_trades (base_asset);
ALTER TABLE closed_trades ALTER COLUMN fees_usdt DROP NOT NULL;
ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS funding_usdt DOUBLE PRECISION;
ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS entry_fees_usdt DOUBLE PRECISION;
ALTER TABLE closed_trades ADD COLUMN IF NOT EXISTS exit_fees_usdt DOUBLE PRECISION;

ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS min_profit_usdt DOUBLE PRECISION;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS max_loss_usdt DOUBLE PRECISION;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS allow_loss_on_hold BOOLEAN;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS funding_flip_confirm_samples INTEGER;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS funding_flip_confirm_window_ms BIGINT;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS emergency_max_hold_days DOUBLE PRECISION;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS max_leg_drawdown_pct DOUBLE PRECISION;
ALTER TABLE runtime_settings ADD COLUMN IF NOT EXISTS max_open_positions INTEGER;

UPDATE runtime_settings
SET min_profit_usdt = COALESCE(min_profit_usdt, 0.0),
    max_loss_usdt = COALESCE(max_loss_usdt, 0.05),
    allow_loss_on_hold = COALESCE(allow_loss_on_hold, FALSE),
    emergency_max_hold_days = COALESCE(emergency_max_hold_days, 14.0),
    max_leg_drawdown_pct = COALESCE(max_leg_drawdown_pct, 0.60),
    max_open_positions = COALESCE(max_open_positions, 3);
UPDATE runtime_settings
SET funding_flip_confirm_samples = COALESCE(funding_flip_confirm_samples, 3),
    funding_flip_confirm_window_ms = COALESCE(funding_flip_confirm_window_ms, 300000);


