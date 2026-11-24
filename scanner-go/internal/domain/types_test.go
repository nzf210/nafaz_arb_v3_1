package domain

import (
	"encoding/json"
	"path/filepath"
	"testing"
)

func TestCanonSymbol(t *testing.T) {
	spot, err := NewCanonSymbol("btc", MarketTypeSpot)
	if err != nil || spot != "BTC/USDT" {
		t.Fatalf("expected BTC/USDT, got %q, err=%v", spot, err)
	}
	if spot.BaseAsset() != "BTC" {
		t.Fatalf("expected base BTC, got %q", spot.BaseAsset())
	}
	if mt, err := spot.MarketType(); err != nil || mt != MarketTypeSpot {
		t.Fatalf("expected spot market type, got %q, err=%v", mt, err)
	}

	perp, err := NewCanonSymbol("ETH", MarketTypeLinearPerp)
	if err != nil || perp != "ETH/USDT:USDT" {
		t.Fatalf("expected ETH/USDT:USDT, got %q, err=%v", perp, err)
	}
	if perp.BaseAsset() != "ETH" {
		t.Fatalf("expected base ETH, got %q", perp.BaseAsset())
	}
	if mt, err := perp.MarketType(); err != nil || mt != MarketTypeLinearPerp {
		t.Fatalf("expected linear_perp market type, got %q, err=%v", mt, err)
	}

	if _, err := NewCanonSymbol("", MarketTypeSpot); err == nil {
		t.Fatal("expected error for empty base asset")
	}
	if err := CanonSymbol("BTC/USD").Validate(); err == nil {
		t.Fatal("expected error for non-USDT symbol")
	}
}

func TestFundingSnapshotValidate(t *testing.T) {
	valid := &FundingSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT:USDT",
		RawSymbol:     "BTC-USDT-SWAP",
		FundingRate:   0.0001,
		IntervalHours: 8,
		TimestampMs:   1700000000000,
	}
	if err := valid.Validate(); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	invalid := &FundingSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT:USDT",
		IntervalHours: 0,
	}
	if err := invalid.Validate(); err == nil {
		t.Fatal("expected error when IntervalHours <= 0")
	}
}

func TestSignalPayloadJSONContract(t *testing.T) {
	sig := SignalPayload{
		SchemaVersion:      SchemaVersionV31,
		SignalID:           "sig_okx_bybit_btc_1700000000123",
		DedupKey:           "perp_perp:BTC:okx:bybit",
		ProfileID:          "funding_carry_core",
		RouteType:          RoutePerpPerpCrossCEX,
		BaseAsset:          "BTC",
		TargetNotionalUSDT: 1000.0,
		Legs: SignalLegs{
			LegA: SignalLeg{
				Exchange:      "okx",
				MarketType:    MarketTypeLinearPerp,
				RawSymbol:     "BTC-USDT-SWAP",
				CanonSymbol:   "BTC/USDT:USDT",
				Action:        ActionBuyLong,
				VWAPPrice:     65000.5,
				FundingRate:   -0.0001,
				IntervalHours: 8,
			},
			LegB: SignalLeg{
				Exchange:      "bybit",
				MarketType:    MarketTypeLinearPerp,
				RawSymbol:     "BTCUSDT",
				CanonSymbol:   "BTC/USDT:USDT",
				Action:        ActionSellShort,
				VWAPPrice:     65010.2,
				FundingRate:   0.0005,
				IntervalHours: 8,
			},
		},
		Metrics: SignalMetrics{
			GrossFunding24h: 0.0018,
			RoundTripCost:   0.0012,
			NetDailyYield:   0.001628,
			BreakevenDays:   0.67,
		},
		GeneratedAtMs: 1700000000123,
		ExpiresAtMs:   1700000002623,
	}

	if err := sig.Validate(); err != nil {
		t.Fatalf("valid signal failed validation: %v", err)
	}

	raw, err := json.Marshal(sig)
	if err != nil {
		t.Fatalf("marshal failed: %v", err)
	}

	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("unmarshal to map failed: %v", err)
	}
	if decoded["schema_version"] != "3.1" {
		t.Fatalf("expected schema_version 3.1, got %v", decoded["schema_version"])
	}

	var roundTrip SignalPayload
	if err := json.Unmarshal(raw, &roundTrip); err != nil {
		t.Fatalf("roundtrip unmarshal failed: %v", err)
	}
	if roundTrip.SignalID != sig.SignalID || roundTrip.Legs.LegA.IntervalHours != 8 || roundTrip.Metrics.NetDailyYield != 0.001628 {
		t.Fatalf("roundtrip mismatch: %+v", roundTrip)
	}
}

func TestLoadConfigs(t *testing.T) {
	scannerCfgPath := filepath.Join("..", "..", "..", "configs", "scanner.base.yaml")
	scCfg, err := LoadScannerConfig(scannerCfgPath)
	if err != nil {
		t.Fatalf("failed loading scanner.base.yaml: %v", err)
	}
	if len(scCfg.Profiles) != 2 {
		t.Fatalf("expected 2 profiles, got %d", len(scCfg.Profiles))
	}
	if scCfg.RoutePolicies.EnableSpotPerpCrossCEX {
		t.Fatal("expected enable_spot_perp_cross_cex to be false by default")
	}

	exCfgPath := filepath.Join("..", "..", "..", "configs", "exchanges.yaml")
	exCfg, err := LoadExchangesConfig(exCfgPath)
	if err != nil {
		t.Fatalf("failed loading exchanges.yaml: %v", err)
	}
	for _, name := range []string{"okx", "bybit", "mexc"} {
		if _, ok := exCfg.Exchanges[name]; !ok {
			t.Fatalf("expected exchange %q in exchanges.yaml", name)
		}
	}

	t.Setenv("REDIS_ADDR", "redis:6379")
	t.Setenv("DRY_RUN", "false")
	scEnvCfg, err := LoadScannerConfig(scannerCfgPath)
	if err != nil {
		t.Fatalf("failed loading scanner config with env override: %v", err)
	}
	if scEnvCfg.Engine.RedisAddr != "redis:6379" || scEnvCfg.Engine.DryRun != false {
		t.Fatalf("expected env override redis:6379 & dry_run=false, got %+v", scEnvCfg.Engine)
	}
}
