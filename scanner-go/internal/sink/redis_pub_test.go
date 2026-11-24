package sink

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"nafaz_arb/internal/domain"
)

func makeTestSignal(dedupKey string, netYield float64, nowMs int64) domain.SignalPayload {
	return domain.SignalPayload{
		SchemaVersion:      domain.SchemaVersionV31,
		SignalID:           "sig_test_1",
		DedupKey:           dedupKey,
		ProfileID:          "funding_carry_core",
		RouteType:          domain.RoutePerpPerpCrossCEX,
		BaseAsset:          "BTC",
		TargetNotionalUSDT: 1000,
		Legs: domain.SignalLegs{
			LegA: domain.SignalLeg{
				Exchange:      "okx",
				MarketType:    domain.MarketTypeLinearPerp,
				RawSymbol:     "BTC-USDT-SWAP",
				CanonSymbol:   "BTC/USDT:USDT",
				Action:        domain.ActionBuyLong,
				VWAPPrice:     65000,
				FundingRate:   -0.0001,
				IntervalHours: 8,
			},
			LegB: domain.SignalLeg{
				Exchange:      "bybit",
				MarketType:    domain.MarketTypeLinearPerp,
				RawSymbol:     "BTCUSDT",
				CanonSymbol:   "BTC/USDT:USDT",
				Action:        domain.ActionSellShort,
				VWAPPrice:     65010,
				FundingRate:   0.0005,
				IntervalHours: 8,
			},
		},
		Metrics: domain.SignalMetrics{
			GrossFunding24h: 0.0018,
			RoundTripCost:   0.0012,
			NetDailyYield:   netYield,
			BreakevenDays:   0.67,
		},
		GeneratedAtMs: nowMs,
		ExpiresAtMs:   nowMs + 2500,
	}
}

func TestSignalPublisher_DeduplicationAndCooldown(t *testing.T) {
	ctx := context.Background()
	mem := NewMemoryKVStore()
	fakeNow := time.Unix(1_700_000_000, 0)
	mem.SetClock(func() time.Time { return fakeNow })

	pub := NewSignalPublisher(mem, domain.EngineConfig{
		DedupCooldownSec: 300,
		MinYieldDelta:    0.0005, // 0.05% daily yield jump threshold
	})

	dedupKey := "perp_perp_cross_cex:BTC:okx:bybit"

	// 1. First signal publishes
	sig1 := makeTestSignal(dedupKey, 0.0020, fakeNow.UnixMilli())
	ok, err := pub.PublishSignal(ctx, sig1)
	if err != nil || !ok {
		t.Fatalf("expected first signal to publish, ok=%v err=%v", ok, err)
	}

	// 2. Identical signal during cooldown is suppressed
	fakeNow = fakeNow.Add(10 * time.Second)
	sig2 := makeTestSignal(dedupKey, 0.0020, fakeNow.UnixMilli())
	ok, err = pub.PublishSignal(ctx, sig2)
	if err != nil || ok {
		t.Fatalf("expected duplicate signal to be suppressed during cooldown, ok=%v err=%v", ok, err)
	}

	// 3. Small yield increase (< MinYieldDelta 0.0005) is still suppressed
	fakeNow = fakeNow.Add(10 * time.Second)
	sig3 := makeTestSignal(dedupKey, 0.0023, fakeNow.UnixMilli())
	ok, err = pub.PublishSignal(ctx, sig3)
	if err != nil || ok {
		t.Fatalf("expected small yield delta signal to be suppressed, ok=%v err=%v", ok, err)
	}

	// 4. Yield spike >= MinYieldDelta (0.0020 -> 0.0026, delta = 0.0006 >= 0.0005) bypasses cooldown
	fakeNow = fakeNow.Add(10 * time.Second)
	sig4 := makeTestSignal(dedupKey, 0.0026, fakeNow.UnixMilli())
	ok, err = pub.PublishSignal(ctx, sig4)
	if err != nil || !ok {
		t.Fatalf("expected yield spike signal to publish immediately, ok=%v err=%v", ok, err)
	}

	// 5. After TTL cooldown expires (> 300s), lower yield signal can publish again
	fakeNow = fakeNow.Add(301 * time.Second)
	sig5 := makeTestSignal(dedupKey, 0.0018, fakeNow.UnixMilli())
	ok, err = pub.PublishSignal(ctx, sig5)
	if err != nil || !ok {
		t.Fatalf("expected signal after cooldown expiration to publish, ok=%v err=%v", ok, err)
	}

	msgs := mem.PublishedMessages(domain.KeyCoreSignals)
	if len(msgs) != 3 {
		t.Fatalf("expected 3 published JSON messages in %s, got %d", domain.KeyCoreSignals, len(msgs))
	}

	var decoded domain.SignalPayload
	if err := json.Unmarshal([]byte(msgs[0]), &decoded); err != nil {
		t.Fatalf("failed to unmarshal published signal JSON: %v", err)
	}
	if decoded.SchemaVersion != domain.SchemaVersionV31 || decoded.DedupKey != dedupKey {
		t.Fatalf("unexpected decoded payload: %+v", decoded)
	}
}

func TestSignalPublisher_MetaObservabilityHelpers(t *testing.T) {
	ctx := context.Background()
	mem := NewMemoryKVStore()
	pub := NewSignalPublisher(mem, domain.EngineConfig{
		DedupCooldownSec: 300,
		MinYieldDelta:    0.0005,
	})

	if err := pub.UpdateExchangeHealth(ctx, "okx", "healthy", 1700000000123, 42, 0); err != nil {
		t.Fatalf("UpdateExchangeHealth failed: %v", err)
	}
	exHash := mem.GetHash(domain.KeyPrefixMetaExchange + "okx")
	if exHash["status"] != "healthy" || exHash["active_symbols_count"] != "42" {
		t.Fatalf("unexpected exchange health hash: %v", exHash)
	}

	if err := pub.UpdateCollateralSnapshot(ctx, &domain.CollateralSnapshot{
		Exchange:    "okx",
		AssetRatios: map[string]float64{"BTC": 0.95, "ETH": 0.90},
		TimestampMs: 1700000000500,
	}); err != nil {
		t.Fatalf("UpdateCollateralSnapshot failed: %v", err)
	}
	colHash := mem.GetHash(domain.KeyPrefixMetaCollateral + "okx")
	if colHash["BTC"] != "0.9500" || colHash["ETH"] != "0.9000" {
		t.Fatalf("unexpected collateral hash: %v", colHash)
	}

	if err := pub.RecordRejectionStats(ctx, map[string]int64{
		"stale_book":    3,
		"low_net_yield": 5,
	}); err != nil {
		t.Fatalf("RecordRejectionStats failed: %v", err)
	}
	if err := pub.RecordRejectionStats(ctx, map[string]int64{
		"stale_book": 2,
	}); err != nil {
		t.Fatalf("RecordRejectionStats second call failed: %v", err)
	}
	rejHash := mem.GetHash(domain.KeyMetaRejectionStats)
	if rejHash["stale_book"] != "5" || rejHash["low_net_yield"] != "5" {
		t.Fatalf("unexpected rejection stats hash: %v", rejHash)
	}
}
