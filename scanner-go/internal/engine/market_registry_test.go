package engine

import (
	"reflect"
	"testing"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/domain"
)

func makePerpDesc(exchange, raw, base string) domain.MarketDescriptor {
	return domain.MarketDescriptor{
		Exchange:       exchange,
		RawSymbol:      raw,
		CanonSymbol:    domain.CanonSymbol(base + "/USDT:USDT"),
		BaseAsset:      base,
		QuoteAsset:     domain.QuoteAssetUSDT,
		MarketType:     domain.MarketTypeLinearPerp,
		Active:         true,
		DiscoveredAtMs: 1700000000000,
	}
}

func makeSpotDesc(exchange, raw, base string) domain.MarketDescriptor {
	return domain.MarketDescriptor{
		Exchange:       exchange,
		RawSymbol:      raw,
		CanonSymbol:    domain.CanonSymbol(base + "/USDT"),
		BaseAsset:      base,
		QuoteAsset:     domain.QuoteAssetUSDT,
		MarketType:     domain.MarketTypeSpot,
		Active:         true,
		DiscoveredAtMs: 1700000000000,
	}
}

func TestMarketRegistry_CrossCEXMatchingMatrixAndDynamicLifecycle(t *testing.T) {
	reg := NewMarketRegistry()
	defaultCfg := domain.ExchangeConfig{Enabled: true}
	nowMs := int64(1700000000000)

	// Matrix from PRD Phase 2.C:
	// BTC, ETH, SOL: OKX + Bybit + MEXC
	// ABC: OKX + MEXC (Unavailable on Bybit)
	// XYZ: Bybit + MEXC (Unavailable on OKX)
	// ONLY1: OKX only (Single-CEX -> must NOT match cross-CEX)
	reg.UpdateExchangeMarkets("okx", defaultCfg, []domain.MarketDescriptor{
		makePerpDesc("okx", "BTC-USDT-SWAP", "BTC"),
		makePerpDesc("okx", "ETH-USDT-SWAP", "ETH"),
		makePerpDesc("okx", "SOL-USDT-SWAP", "SOL"),
		makePerpDesc("okx", "ABC-USDT-SWAP", "ABC"),
		makePerpDesc("okx", "ONLY1-USDT-SWAP", "ONLY1"),
	}, nowMs)

	reg.UpdateExchangeMarkets("bybit", defaultCfg, []domain.MarketDescriptor{
		makePerpDesc("bybit", "BTCUSDT", "BTC"),
		makePerpDesc("bybit", "ETHUSDT", "ETH"),
		makePerpDesc("bybit", "SOLUSDT", "SOL"),
		makePerpDesc("bybit", "XYZUSDT", "XYZ"),
	}, nowMs)

	reg.UpdateExchangeMarkets("mexc", defaultCfg, []domain.MarketDescriptor{
		makePerpDesc("mexc", "BTC_USDT", "BTC"),
		makePerpDesc("mexc", "ETH_USDT", "ETH"),
		makePerpDesc("mexc", "SOL_USDT", "SOL"),
		makePerpDesc("mexc", "ABC_USDT", "ABC"),
		makePerpDesc("mexc", "XYZ_USDT", "XYZ"),
	}, nowMs)

	exchanges := []ExchangeMeta{
		{Name: "okx", Healthy: true, Capabilities: adapter.Capabilities{HasSpot: true, HasPerp: true, UnifiedMargin: true}},
		{Name: "bybit", Healthy: true, Capabilities: adapter.Capabilities{HasSpot: true, HasPerp: true, UnifiedMargin: true}},
		{Name: "mexc", Healthy: true, Capabilities: adapter.Capabilities{HasSpot: true, HasPerp: true, UnifiedMargin: false}},
	}
	policies := domain.RoutePolicyConfig{
		EnableSpotPerpSameCEX:  true,
		EnablePerpPerpCrossCEX: true,
		EnableSpotPerpCrossCEX: false,
	}

	match := reg.MatchUniverse(exchanges, policies)

	wantMatched := []string{"ABC", "BTC", "ETH", "SOL", "XYZ"}
	if !reflect.DeepEqual(match.MatchedBaseAssets, wantMatched) {
		t.Fatalf("MatchedBaseAssets = %v, want %v", match.MatchedBaseAssets, wantMatched)
	}
	if _, hasOnly1 := match.PerpPerpRoutes["ONLY1"]; hasOnly1 {
		t.Fatal("single-CEX pair ONLY1 must not produce cross-CEX routes")
	}

	// Verify ABC only has OKX->MEXC and MEXC->OKX (never Bybit)
	abcRoutes := match.PerpPerpRoutes["ABC"]
	wantABC := []ExchangePair{
		{LongExchange: "mexc", ShortExchange: "okx"},
		{LongExchange: "okx", ShortExchange: "mexc"},
	}
	if !reflect.DeepEqual(abcRoutes, wantABC) {
		t.Fatalf("ABC routes = %+v, want %+v", abcRoutes, wantABC)
	}

	// Verify XYZ only has Bybit->MEXC and MEXC->Bybit (never OKX)
	xyzRoutes := match.PerpPerpRoutes["XYZ"]
	wantXYZ := []ExchangePair{
		{LongExchange: "bybit", ShortExchange: "mexc"},
		{LongExchange: "mexc", ShortExchange: "bybit"},
	}
	if !reflect.DeepEqual(xyzRoutes, wantXYZ) {
		t.Fatalf("XYZ routes = %+v, want %+v", xyzRoutes, wantXYZ)
	}

	// Verify OKX polling targets do NOT include ONLY1 or XYZ
	for _, d := range match.PollingTargets["okx"] {
		if d.BaseAsset == "ONLY1" || d.BaseAsset == "XYZ" {
			t.Fatalf("unexpected polling target on okx: %+v", d)
		}
	}

	// Dynamic continuous discovery:
	// 1. Add NEWCOIN on OKX and Bybit
	// 2. Delist ABC from OKX
	added, removed := reg.UpdateExchangeMarkets("okx", defaultCfg, []domain.MarketDescriptor{
		makePerpDesc("okx", "BTC-USDT-SWAP", "BTC"),
		makePerpDesc("okx", "ETH-USDT-SWAP", "ETH"),
		makePerpDesc("okx", "SOL-USDT-SWAP", "SOL"),
		makePerpDesc("okx", "NEWCOIN-USDT-SWAP", "NEWCOIN"),
	}, nowMs+300_000)
	if len(added) != 1 || added[0].BaseAsset != "NEWCOIN" {
		t.Fatalf("expected NEWCOIN added on okx, got %+v", added)
	}
	if len(removed) != 2 { // ABC and ONLY1 removed
		t.Fatalf("expected 2 removed markets (ABC, ONLY1) on okx, got %+v", removed)
	}

	reg.UpdateExchangeMarkets("bybit", defaultCfg, []domain.MarketDescriptor{
		makePerpDesc("bybit", "BTCUSDT", "BTC"),
		makePerpDesc("bybit", "ETHUSDT", "ETH"),
		makePerpDesc("bybit", "SOLUSDT", "SOL"),
		makePerpDesc("bybit", "XYZUSDT", "XYZ"),
		makePerpDesc("bybit", "NEWCOINUSDT", "NEWCOIN"),
	}, nowMs+300_000)

	match2 := reg.MatchUniverse(exchanges, policies)
	if _, hasABC := match2.PerpPerpRoutes["ABC"]; hasABC {
		t.Fatal("delisted ABC on OKX leaves only MEXC (1 CEX) and must be evicted from cross-CEX routes")
	}
	if len(match2.PerpPerpRoutes["NEWCOIN"]) != 2 {
		t.Fatalf("expected NEWCOIN matched in both directions between OKX and Bybit, got %+v", match2.PerpPerpRoutes["NEWCOIN"])
	}
}

func TestMarketRegistry_SpotVsPerpNotConfused(t *testing.T) {
	reg := NewMarketRegistry()
	cfg := domain.ExchangeConfig{Enabled: true}
	nowMs := int64(1700000000000)

	// OKX has TOKEN on Spot only; Bybit has TOKEN on Linear Perp only.
	// With EnableSpotPerpCrossCEX = false, TOKEN must NOT match Perp-Perp or Same-CEX Spot-Perp!
	reg.UpdateExchangeMarkets("okx", cfg, []domain.MarketDescriptor{
		makeSpotDesc("okx", "TOKEN-USDT", "TOKEN"),
	}, nowMs)
	reg.UpdateExchangeMarkets("bybit", cfg, []domain.MarketDescriptor{
		makePerpDesc("bybit", "TOKENUSDT", "TOKEN"),
	}, nowMs)

	exchanges := []ExchangeMeta{
		{Name: "okx", Healthy: true, Capabilities: adapter.Capabilities{HasSpot: true, HasPerp: true, UnifiedMargin: true}},
		{Name: "bybit", Healthy: true, Capabilities: adapter.Capabilities{HasSpot: true, HasPerp: true, UnifiedMargin: true}},
	}
	match := reg.MatchUniverse(exchanges, domain.RoutePolicyConfig{
		EnableSpotPerpSameCEX:  true,
		EnablePerpPerpCrossCEX: true,
		EnableSpotPerpCrossCEX: false,
	})
	if len(match.MatchedBaseAssets) != 0 {
		t.Fatalf("Spot on OKX and Perp on Bybit must not match when SpotPerpCrossCEX is disabled, got %v", match.MatchedBaseAssets)
	}
}
