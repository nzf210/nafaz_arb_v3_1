package engine

import (
	"errors"
	"math"
	"testing"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/domain"
)

func almostEqual(a, b, tol float64) bool {
	return math.Abs(a-b) <= tol
}

func TestComputeVWAP_SlippageAndInsufficientDepth(t *testing.T) {
	book := &domain.OrderBookSnapshot{
		Exchange:    "okx",
		CanonSymbol: "BTC/USDT:USDT",
		RawSymbol:   "BTC-USDT-SWAP",
		MarketType:  domain.MarketTypeLinearPerp,
		Bids: []domain.OrderBookLevel{
			{Price: 100.0, Quantity: 5.0}, // 500 USDT
			{Price: 99.0, Quantity: 10.0}, // 990 USDT
		},
		Asks: []domain.OrderBookLevel{
			{Price: 101.0, Quantity: 5.0}, // 505 USDT
			{Price: 103.0, Quantity: 10.0}, // 1030 USDT
		},
		Volume24hUSDT: 5_000_000,
		TimestampMs:   1000,
	}

	// Buy 1000 USDT -> takes 505 USDT @ 101.0 (qty 5.0) + 495 USDT @ 103.0 (qty 495/103 = 4.80582524)
	resBuy, err := ComputeVWAP(book, domain.ActionBuyLong, 1000.0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	expectedMid := (100.0 + 101.0) / 2.0 // 100.5
	if !almostEqual(resBuy.MidPrice, expectedMid, 1e-6) {
		t.Fatalf("expected midPrice %.4f, got %.4f", expectedMid, resBuy.MidPrice)
	}
	if resBuy.VWAPPrice <= 101.0 {
		t.Fatalf("expected VWAP > top-of-book 101.0, got %.6f", resBuy.VWAPPrice)
	}
	expectedQty := 5.0 + (495.0 / 103.0)
	expectedVWAP := 1000.0 / expectedQty
	if !almostEqual(resBuy.VWAPPrice, expectedVWAP, 1e-6) {
		t.Fatalf("expected VWAP %.6f, got %.6f", expectedVWAP, resBuy.VWAPPrice)
	}
	expectedSlip := math.Abs(expectedVWAP-expectedMid) / expectedMid
	if !almostEqual(resBuy.Slippage, expectedSlip, 1e-6) {
		t.Fatalf("expected slippage %.6f, got %.6f", expectedSlip, resBuy.Slippage)
	}

	// Sell 1000 USDT -> takes 500 USDT @ 100.0 + 500 USDT @ 99.0
	resSell, err := ComputeVWAP(book, domain.ActionSellShort, 1000.0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if resSell.VWAPPrice >= 100.0 {
		t.Fatalf("expected sell VWAP < top-of-book 100.0, got %.6f", resSell.VWAPPrice)
	}

	// Insufficient depth: asks total = 505 + 1030 = 1535 USDT, request 2000 USDT
	_, err = ComputeVWAP(book, domain.ActionBuyLong, 2000.0)
	if !errors.Is(err, ErrInsufficientDepth) {
		t.Fatalf("expected ErrInsufficientDepth, got %v", err)
	}
}

func TestNormalizeGrossFunding24h_IntervalMismatch(t *testing.T) {
	// Leg A (Long) = -0.0002 per 4h -> -(-0.0002)*(24/4) = +0.0012
	// Leg B (Short) = +0.0001 per 1h -> +(0.0001)*(24/1) = +0.0024
	// Total = 0.0036
	gross := NormalizeGrossFunding24h(-0.0002, 4, false, 0.0001, 1)
	if !almostEqual(gross, 0.0036, 1e-9) {
		t.Fatalf("expected gross 0.0036, got %.8f", gross)
	}

	// Spot-Perp: Leg A is spot (even if longRate passed non-zero, ignored), Leg B is 0.0005 per 8h -> 0.0015
	grossSpot := NormalizeGrossFunding24h(0.0099, 8, true, 0.0005, 8)
	if !almostEqual(grossSpot, 0.0015, 1e-9) {
		t.Fatalf("expected spot gross 0.0015, got %.8f", grossSpot)
	}
}

func TestMatrixEvaluator_StalenessAndCollateralGates(t *testing.T) {
	nowMs := int64(1_700_000_000_000)
	cfg := &domain.ScannerConfig{
		Engine: domain.EngineConfig{
			TickIntervalMs:   1000,
			SignalValidityMs: 2500,
			DedupCooldownSec: 300,
			MinYieldDelta:    0.0005,
		},
		Freshness: domain.FreshnessConfig{
			MaxBookAgeMs:        2500,
			MaxFundingAgeSec:    120,
			MaxCollateralAgeSec: 7200,
		},
		RoutePolicies: domain.RoutePolicyConfig{
			EnableSpotPerpSameCEX:  true,
			EnablePerpPerpCrossCEX: true,
			EnableSpotPerpCrossCEX: false,
			MinCollateralRatio:     0.85,
		},
		Profiles: []domain.ProfileConfig{
			{
				ID:                    "funding_carry_core",
				TargetNotionalUSDT:    1000,
				TargetHoldDays:        7,
				MaxBreakevenDays:      3,
				MinNetDailyYield:      0.0005,
				MinVolume24hUSDT:      1_000_000,
				MaxInitialBasisSpread: 0.02,
			},
		},
	}

	store := NewStateStore()
	store.UpsertExchange(ExchangeMeta{
		Name:    "okx",
		Healthy: true,
		Capabilities: adapter.Capabilities{
			HasSpot:           true,
			HasPerp:           true,
			HasCollateralInfo: true,
			UnifiedMargin:     true,
		},
		SpotTakerFee: 0.0008,
		PerpTakerFee: 0.0005,
	})

	// 1. Stale book (older than 2500ms)
	store.UpdateOrderBook(&domain.OrderBookSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT",
		RawSymbol:     "BTC-USDT",
		MarketType:    domain.MarketTypeSpot,
		Bids:          []domain.OrderBookLevel{{Price: 50000, Quantity: 1}},
		Asks:          []domain.OrderBookLevel{{Price: 50001, Quantity: 1}},
		Volume24hUSDT: 10_000_000,
		TimestampMs:   nowMs - 3000, // STALE
	})
	store.UpdateOrderBook(&domain.OrderBookSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT:USDT",
		RawSymbol:     "BTC-USDT-SWAP",
		MarketType:    domain.MarketTypeLinearPerp,
		Bids:          []domain.OrderBookLevel{{Price: 50005, Quantity: 1}},
		Asks:          []domain.OrderBookLevel{{Price: 50006, Quantity: 1}},
		Volume24hUSDT: 10_000_000,
		TimestampMs:   nowMs - 500,
	})
	store.UpdateFunding(&domain.FundingSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT:USDT",
		RawSymbol:     "BTC-USDT-SWAP",
		FundingRate:   0.001,
		IntervalHours: 8,
		TimestampMs:   nowMs - 10_000,
	})
	store.UpdateCollateral(&domain.CollateralSnapshot{
		Exchange:    "okx",
		AssetRatios: map[string]float64{"BTC": 0.95},
		TimestampMs: nowMs - 60_000,
	})

	ev := NewMatrixEvaluator(cfg, store)
	sigs := ev.EvaluateTick(nowMs)
	if len(sigs) != 0 {
		t.Fatalf("expected 0 signals on stale book, got %d", len(sigs))
	}
	if ev.RejectionStats()["stale_book"] != 1 {
		t.Fatalf("expected stale_book rejection=1, got %v", ev.RejectionStats())
	}

	// Fix book age, make funding stale (> 120s)
	store.UpdateOrderBook(&domain.OrderBookSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT",
		RawSymbol:     "BTC-USDT",
		MarketType:    domain.MarketTypeSpot,
		Bids:          []domain.OrderBookLevel{{Price: 50000, Quantity: 1}},
		Asks:          []domain.OrderBookLevel{{Price: 50001, Quantity: 1}},
		Volume24hUSDT: 10_000_000,
		TimestampMs:   nowMs - 500,
	})
	store.UpdateFunding(&domain.FundingSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT:USDT",
		RawSymbol:     "BTC-USDT-SWAP",
		FundingRate:   0.001,
		IntervalHours: 8,
		TimestampMs:   nowMs - 130_000, // STALE (>120s)
	})
	sigs = ev.EvaluateTick(nowMs)
	if len(sigs) != 0 || ev.RejectionStats()["stale_funding"] != 1 {
		t.Fatalf("expected stale_funding rejection=1, got %v", ev.RejectionStats())
	}

	// Fix funding age, make collateral stale (> 7200s)
	store.UpdateFunding(&domain.FundingSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "BTC/USDT:USDT",
		RawSymbol:     "BTC-USDT-SWAP",
		FundingRate:   0.001,
		IntervalHours: 8,
		TimestampMs:   nowMs - 10_000,
	})
	store.UpdateCollateral(&domain.CollateralSnapshot{
		Exchange:    "okx",
		AssetRatios: map[string]float64{"BTC": 0.95},
		TimestampMs: nowMs - 7300_000, // STALE
	})
	sigs = ev.EvaluateTick(nowMs)
	if len(sigs) != 0 || ev.RejectionStats()["stale_collateral"] != 1 {
		t.Fatalf("expected stale_collateral rejection=1, got %v", ev.RejectionStats())
	}

	// Fix collateral age, make collateral ratio low (< 0.85)
	store.UpdateCollateral(&domain.CollateralSnapshot{
		Exchange:    "okx",
		AssetRatios: map[string]float64{"BTC": 0.50},
		TimestampMs: nowMs - 60_000,
	})
	sigs = ev.EvaluateTick(nowMs)
	if len(sigs) != 0 || ev.RejectionStats()["collateral_ratio_low"] != 1 {
		t.Fatalf("expected collateral_ratio_low rejection=1, got %v", ev.RejectionStats())
	}

	// Fix collateral ratio -> should emit spot_perp_same_cex signal
	store.UpdateCollateral(&domain.CollateralSnapshot{
		Exchange:    "okx",
		AssetRatios: map[string]float64{"BTC": 0.95},
		TimestampMs: nowMs - 60_000,
	})
	sigs = ev.EvaluateTick(nowMs)
	if len(sigs) != 1 {
		t.Fatalf("expected 1 valid spot_perp_same_cex signal, got %d", len(sigs))
	}
	if sigs[0].RouteType != domain.RouteSpotPerpSameCEX {
		t.Fatalf("expected route spot_perp_same_cex, got %s", sigs[0].RouteType)
	}
	if sigs[0].DedupKey != "spot_perp_same_cex:BTC:okx:okx" {
		t.Fatalf("unexpected dedup key: %s", sigs[0].DedupKey)
	}
}

func TestMatrixEvaluator_MultiProfileAndVWAPCacheSharing(t *testing.T) {
	nowMs := int64(1_700_000_000_000)
	cfg := &domain.ScannerConfig{
		Engine: domain.EngineConfig{
			TickIntervalMs:   1000,
			SignalValidityMs: 2500,
			DedupCooldownSec: 300,
			MinYieldDelta:    0.0005,
		},
		Freshness: domain.FreshnessConfig{
			MaxBookAgeMs:        2500,
			MaxFundingAgeSec:    120,
			MaxCollateralAgeSec: 7200,
		},
		RoutePolicies: domain.RoutePolicyConfig{
			EnableSpotPerpSameCEX:  false,
			EnablePerpPerpCrossCEX: true,
			EnableSpotPerpCrossCEX: false,
			MinCollateralRatio:     0.85,
		},
		Profiles: []domain.ProfileConfig{
			{
				ID:                    "funding_carry_core",
				TargetNotionalUSDT:    1000,
				TargetHoldDays:        7,
				MaxBreakevenDays:      3.0,
				MinNetDailyYield:      0.001,
				MinVolume24hUSDT:      1_000_000,
				MaxInitialBasisSpread: 0.02,
			},
			{
				ID:                    "basis_convergence_fast",
				TargetNotionalUSDT:    1000, // Same notional -> must share VWAP cache
				TargetHoldDays:        1,
				MaxBreakevenDays:      1.5,
				MinNetDailyYield:      0.001,
				MinVolume24hUSDT:      1_000_000,
				MaxInitialBasisSpread: 0.02,
			},
		},
	}

	store := NewStateStore()
	store.UpsertExchange(ExchangeMeta{
		Name:         "okx",
		Healthy:      true,
		Capabilities: adapter.Capabilities{HasPerp: true},
		PerpTakerFee: 0.0005,
	})
	store.UpsertExchange(ExchangeMeta{
		Name:         "bybit",
		Healthy:      true,
		Capabilities: adapter.Capabilities{HasPerp: true},
		PerpTakerFee: 0.0005,
	})

	store.UpdateOrderBook(&domain.OrderBookSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "ETH/USDT:USDT",
		RawSymbol:     "ETH-USDT-SWAP",
		MarketType:    domain.MarketTypeLinearPerp,
		Bids:          []domain.OrderBookLevel{{Price: 2000.0, Quantity: 10}},
		Asks:          []domain.OrderBookLevel{{Price: 2000.2, Quantity: 10}},
		Volume24hUSDT: 25_000_000,
		TimestampMs:   nowMs - 200,
	})
	store.UpdateOrderBook(&domain.OrderBookSnapshot{
		Exchange:      "bybit",
		CanonSymbol:   "ETH/USDT:USDT",
		RawSymbol:     "ETHUSDT",
		MarketType:    domain.MarketTypeLinearPerp,
		Bids:          []domain.OrderBookLevel{{Price: 2000.4, Quantity: 10}},
		Asks:          []domain.OrderBookLevel{{Price: 2000.6, Quantity: 10}},
		Volume24hUSDT: 25_000_000,
		TimestampMs:   nowMs - 200,
	})

	// OKX interval 4h (-0.0002 -> +0.0012/day for long), Bybit interval 8h (+0.0010 -> +0.0030/day for short)
	// Gross 24h for Long OKX / Short Bybit = 0.0042 (0.42%/day)
	store.UpdateFunding(&domain.FundingSnapshot{
		Exchange:      "okx",
		CanonSymbol:   "ETH/USDT:USDT",
		RawSymbol:     "ETH-USDT-SWAP",
		FundingRate:   -0.0002,
		IntervalHours: 4,
		TimestampMs:   nowMs - 5000,
	})
	store.UpdateFunding(&domain.FundingSnapshot{
		Exchange:      "bybit",
		CanonSymbol:   "ETH/USDT:USDT",
		RawSymbol:     "ETHUSDT",
		FundingRate:   0.0010,
		IntervalHours: 8,
		TimestampMs:   nowMs - 5000,
	})

	ev := NewMatrixEvaluator(cfg, store)
	out := ev.evaluateTickInternal(nowMs)

	// 2 profiles pass for (Long OKX, Short Bybit); reverse direction (Long Bybit, Short OKX) has gross = -0.0042 (rejected as negative_gross)
	if len(out.signals) != 2 {
		t.Fatalf("expected 2 signals (one per profile), got %d", len(out.signals))
	}
	// Only 2 VWAP computations should happen (okx BUY_LONG 1000 & bybit SELL_SHORT 1000), shared across both profiles!
	if out.vwapCalls != 2 {
		t.Fatalf("expected VWAP cache to limit calls to 2, got %d", out.vwapCalls)
	}

	for _, sig := range out.signals {
		if err := sig.Validate(); err != nil {
			t.Fatalf("invalid signal emitted: %v", err)
		}
		if sig.DedupKey != "perp_perp_cross_cex:ETH:okx:bybit" {
			t.Fatalf("unexpected dedup_key: %s", sig.DedupKey)
		}
		if sig.ExpiresAtMs != nowMs+2500 {
			t.Fatalf("expected expires_at_ms %d, got %d", nowMs+2500, sig.ExpiresAtMs)
		}
	}
}
