package domain

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

const (
	SchemaVersionV31 = "3.1"

	KeyCoreSignals          = "core:signals"
	KeyCoreKillSwitch       = "core:kill_switch"
	KeyPrefixCoreDedup      = "core:dedup:"
	KeyPrefixCoreLock       = "core:lock:"
	KeyPrefixMetaExchange   = "meta:exchange:"
	KeyPrefixMetaCollateral = "meta:collateral:"
	KeyMetaRejectionStats   = "meta:rejection_stats"
)

type MarketType string

const (
	MarketTypeSpot       MarketType = "spot"
	MarketTypeLinearPerp MarketType = "linear_perp"
)

type CanonSymbol string

func NewCanonSymbol(base string, marketType MarketType) (CanonSymbol, error) {
	b := strings.ToUpper(strings.TrimSpace(base))
	if b == "" || strings.ContainsAny(b, "/: ") {
		return "", fmt.Errorf("invalid base asset: %q", base)
	}
	switch marketType {
	case MarketTypeSpot:
		return CanonSymbol(b + "/USDT"), nil
	case MarketTypeLinearPerp:
		return CanonSymbol(b + "/USDT:USDT"), nil
	default:
		return "", fmt.Errorf("unsupported market type: %q", marketType)
	}
}

func (s CanonSymbol) Validate() error {
	str := string(s)
	if strings.HasSuffix(str, "/USDT:USDT") {
		base := strings.TrimSuffix(str, "/USDT:USDT")
		if base != "" && !strings.ContainsAny(base, "/: ") {
			return nil
		}
		return fmt.Errorf("invalid linear_perp canonical symbol: %q", str)
	}
	if strings.HasSuffix(str, "/USDT") {
		base := strings.TrimSuffix(str, "/USDT")
		if base != "" && !strings.ContainsAny(base, "/: ") {
			return nil
		}
		return fmt.Errorf("invalid spot canonical symbol: %q", str)
	}
	return fmt.Errorf("canonical symbol must end with /USDT or /USDT:USDT: %q", str)
}

func (s CanonSymbol) MarketType() (MarketType, error) {
	if err := s.Validate(); err != nil {
		return "", err
	}
	if strings.HasSuffix(string(s), "/USDT:USDT") {
		return MarketTypeLinearPerp, nil
	}
	return MarketTypeSpot, nil
}

func (s CanonSymbol) BaseAsset() string {
	str := string(s)
	if idx := strings.IndexByte(str, '/'); idx > 0 {
		return str[:idx]
	}
	return ""
}

type RouteType string

const (
	RouteSpotPerpSameCEX  RouteType = "spot_perp_same_cex"
	RoutePerpPerpCrossCEX RouteType = "perp_perp_cross_cex"
	RouteSpotPerpCrossCEX RouteType = "spot_perp_cross_cex"
)

type LegAction string

const (
	ActionBuySpot   LegAction = "BUY_SPOT"
	ActionBuyLong   LegAction = "BUY_LONG"
	ActionSellShort LegAction = "SELL_SHORT"
)

type OrderBookLevel struct {
	Price    float64 `json:"price"`
	Quantity float64 `json:"quantity"`
}

type OrderBookSnapshot struct {
	Exchange      string           `json:"exchange"`
	CanonSymbol   CanonSymbol      `json:"canon_symbol"`
	RawSymbol     string           `json:"raw_symbol"`
	MarketType    MarketType       `json:"market_type"`
	Bids          []OrderBookLevel `json:"bids"`
	Asks          []OrderBookLevel `json:"asks"`
	Volume24hUSDT float64          `json:"volume_24h_usdt"`
	TimestampMs   int64            `json:"timestamp_ms"`
}

type FundingSnapshot struct {
	Exchange         string      `json:"exchange"`
	CanonSymbol      CanonSymbol `json:"canon_symbol"`
	RawSymbol        string      `json:"raw_symbol"`
	FundingRate      float64     `json:"funding_rate"`
	IntervalHours    int         `json:"interval_hours"`
	NextSettlementMs int64       `json:"next_settlement_ms"`
	TimestampMs      int64       `json:"timestamp_ms"`
}

func (f *FundingSnapshot) Validate() error {
	if f == nil {
		return errors.New("funding snapshot is nil")
	}
	if f.IntervalHours <= 0 {
		return fmt.Errorf("invalid interval_hours %d: must be > 0", f.IntervalHours)
	}
	return f.CanonSymbol.Validate()
}

type CollateralSnapshot struct {
	Exchange    string             `json:"exchange"`
	AssetRatios map[string]float64 `json:"asset_ratios"`
	TimestampMs int64              `json:"timestamp_ms"`
}

type SignalLeg struct {
	Exchange      string      `json:"exchange"`
	MarketType    MarketType  `json:"market_type"`
	RawSymbol     string      `json:"raw_symbol"`
	CanonSymbol   CanonSymbol `json:"canon_symbol"`
	Action        LegAction   `json:"action"`
	VWAPPrice     float64     `json:"vwap_price"`
	FundingRate   float64     `json:"funding_rate"`
	IntervalHours int         `json:"interval_hours"`
}

type SignalLegs struct {
	LegA SignalLeg `json:"leg_a"`
	LegB SignalLeg `json:"leg_b"`
}

type SignalMetrics struct {
	GrossFunding24h float64 `json:"gross_funding_24h"`
	RoundTripCost   float64 `json:"round_trip_cost"`
	NetDailyYield   float64 `json:"net_daily_yield"`
	BreakevenDays   float64 `json:"breakeven_days"`
}

type SignalPayload struct {
	SchemaVersion      string        `json:"schema_version"`
	SignalID           string        `json:"signal_id"`
	DedupKey           string        `json:"dedup_key"`
	ProfileID          string        `json:"profile_id"`
	RouteType          RouteType     `json:"route_type"`
	BaseAsset          string        `json:"base_asset"`
	TargetNotionalUSDT float64       `json:"target_notional_usdt"`
	Legs               SignalLegs    `json:"legs"`
	Metrics            SignalMetrics `json:"metrics"`
	GeneratedAtMs      int64         `json:"generated_at_ms"`
	ExpiresAtMs        int64         `json:"expires_at_ms"`
}

func (p *SignalPayload) Validate() error {
	if p == nil {
		return errors.New("signal payload is nil")
	}
	if p.SchemaVersion != SchemaVersionV31 {
		return fmt.Errorf("unsupported schema_version: %q", p.SchemaVersion)
	}
	if p.SignalID == "" || p.DedupKey == "" || p.ProfileID == "" || p.BaseAsset == "" {
		return errors.New("missing required signal identifiers")
	}
	if p.TargetNotionalUSDT <= 0 {
		return errors.New("target_notional_usdt must be > 0")
	}
	if p.ExpiresAtMs <= p.GeneratedAtMs {
		return errors.New("expires_at_ms must be > generated_at_ms")
	}
	return nil
}

type EngineConfig struct {
	TickIntervalMs   int     `yaml:"tick_interval_ms"`
	SignalValidityMs int64   `yaml:"signal_validity_ms"`
	DedupCooldownSec int     `yaml:"dedup_cooldown_sec"`
	MinYieldDelta    float64 `yaml:"min_yield_delta"`
	DryRun           bool    `yaml:"dry_run"`
	RedisAddr        string  `yaml:"redis_addr"`
}

type FreshnessConfig struct {
	MaxBookAgeMs        int64 `yaml:"max_book_age_ms"`
	MaxFundingAgeSec    int64 `yaml:"max_funding_age_sec"`
	MaxCollateralAgeSec int64 `yaml:"max_collateral_age_sec"`
}

type RoutePolicyConfig struct {
	EnableSpotPerpSameCEX  bool    `yaml:"enable_spot_perp_same_cex"`
	EnablePerpPerpCrossCEX bool    `yaml:"enable_perp_perp_cross_cex"`
	EnableSpotPerpCrossCEX bool    `yaml:"enable_spot_perp_cross_cex"`
	MinCollateralRatio     float64 `yaml:"min_collateral_ratio"`
}

type ProfileConfig struct {
	ID                    string  `yaml:"id"`
	TargetNotionalUSDT    float64 `yaml:"target_notional_usdt"`
	TargetHoldDays        float64 `yaml:"target_hold_days"`
	MaxBreakevenDays      float64 `yaml:"max_breakeven_days"`
	MinNetDailyYield      float64 `yaml:"min_net_daily_yield"`
	MinVolume24hUSDT      float64 `yaml:"min_volume_24h_usdt"`
	MaxInitialBasisSpread float64 `yaml:"max_initial_basis_spread"`
}

type ScannerConfig struct {
	Engine        EngineConfig      `yaml:"engine"`
	Freshness     FreshnessConfig   `yaml:"freshness"`
	RoutePolicies RoutePolicyConfig `yaml:"route_policies"`
	Profiles      []ProfileConfig   `yaml:"profiles"`
}

func (c *ScannerConfig) Validate() error {
	if c.Engine.TickIntervalMs <= 0 || c.Engine.SignalValidityMs <= 0 {
		return errors.New("engine tick_interval_ms and signal_validity_ms must be > 0")
	}
	if c.Freshness.MaxBookAgeMs <= 0 || c.Freshness.MaxFundingAgeSec <= 0 || c.Freshness.MaxCollateralAgeSec <= 0 {
		return errors.New("freshness thresholds must be > 0")
	}
	if len(c.Profiles) == 0 {
		return errors.New("at least one profile must be configured")
	}
	for _, p := range c.Profiles {
		if p.ID == "" || p.TargetNotionalUSDT <= 0 || p.TargetHoldDays <= 0 || p.MaxBreakevenDays <= 0 {
			return fmt.Errorf("invalid profile config for id %q", p.ID)
		}
	}
	return nil
}

type PollingConfig struct {
	BookIntervalMs         int `yaml:"book_interval_ms"`
	FundingIntervalSec     int `yaml:"funding_interval_sec"`
	FundingFastIntervalSec int `yaml:"funding_fast_interval_sec"`
	CollateralIntervalSec  int `yaml:"collateral_interval_sec"`
}

type ExchangeConfig struct {
	Enabled                 bool          `yaml:"enabled"`
	BaseURL                 string        `yaml:"base_url"`
	WSURL                   string        `yaml:"ws_url"`
	SpotTakerFee            float64       `yaml:"spot_taker_fee"`
	PerpTakerFee            float64       `yaml:"perp_taker_fee"`
	RateLimitRPS            float64       `yaml:"rate_limit_rps"`
	MaxRetries              int           `yaml:"max_retries"`
	CircuitBreakerThreshold int           `yaml:"circuit_breaker_threshold"`
	SymbolAllowlist         []string      `yaml:"symbol_allowlist"`
	SymbolBlocklist         []string      `yaml:"symbol_blocklist"`
	UnifiedMargin           bool          `yaml:"unified_margin"`
	Polling                 PollingConfig `yaml:"polling"`
}

type ExchangesConfig struct {
	Exchanges map[string]ExchangeConfig `yaml:"exchanges"`
}

func (c *ExchangesConfig) Validate() error {
	if len(c.Exchanges) == 0 {
		return errors.New("exchanges config cannot be empty")
	}
	for name, ex := range c.Exchanges {
		if ex.BaseURL == "" {
			return fmt.Errorf("exchange %q missing base_url", name)
		}
		if ex.RateLimitRPS <= 0 {
			return fmt.Errorf("exchange %q rate_limit_rps must be > 0", name)
		}
		if ex.Polling.BookIntervalMs <= 0 || ex.Polling.FundingIntervalSec <= 0 {
			return fmt.Errorf("exchange %q polling intervals must be > 0", name)
		}
	}
	return nil
}

func LoadScannerConfig(path string) (*ScannerConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg ScannerConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	cfg.ApplyEnvOverrides()
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &cfg, nil
}

func (c *ScannerConfig) ApplyEnvOverrides() {
	if v := strings.TrimSpace(os.Getenv("REDIS_ADDR")); v != "" {
		c.Engine.RedisAddr = v
	}
	if v := strings.ToLower(strings.TrimSpace(os.Getenv("DRY_RUN"))); v != "" {
		c.Engine.DryRun = (v == "true" || v == "1" || v == "yes" || v == "on")
	}
}

func LoadExchangesConfig(path string) (*ExchangesConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg ExchangesConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &cfg, nil
}
