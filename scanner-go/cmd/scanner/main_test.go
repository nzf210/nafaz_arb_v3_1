package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"nafaz_arb/internal/domain"
	"nafaz_arb/internal/sink"
)

func TestScannerEndToEndDryRunPipeline(t *testing.T) {
	nowMs := time.Now().UnixMilli()

	// Mock OKX (negative funding -> attractive BUY_LONG leg) & Bybit (high positive funding -> attractive SELL_SHORT leg)
	okxSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := map[string]any{
			"bids":               []map[string]any{{"price": 65000.0, "quantity": 5.0}},
			"asks":               []map[string]any{{"price": 65005.0, "quantity": 5.0}},
			"volume_24h_usdt":    25000000.0,
			"funding_rate":       -0.0005,
			"interval_hours":     8,
			"next_settlement_ms": nowMs + 3600000,
			"timestamp_ms":       nowMs,
			"asset_ratios":       map[string]float64{"USDT": 1.0, "BTC": 0.95, "ETH": 0.95, "SOL": 0.90},
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer okxSrv.Close()

	bybitSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := map[string]any{
			"bids":               []map[string]any{{"price": 65020.0, "quantity": 5.0}},
			"asks":               []map[string]any{{"price": 65025.0, "quantity": 5.0}},
			"volume_24h_usdt":    25000000.0,
			"funding_rate":       0.0012,
			"interval_hours":     4,
			"next_settlement_ms": nowMs + 3600000,
			"timestamp_ms":       nowMs,
			"asset_ratios":       map[string]float64{"USDT": 1.0, "BTC": 0.95, "ETH": 0.95, "SOL": 0.90},
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer bybitSrv.Close()

	scannerCfg, err := domain.LoadScannerConfig("../../../configs/scanner.base.yaml")
	if err != nil {
		t.Fatalf("LoadScannerConfig: %v", err)
	}
	exchangesCfg, err := domain.LoadExchangesConfig("../../../configs/exchanges.yaml")
	if err != nil {
		t.Fatalf("LoadExchangesConfig: %v", err)
	}

	// Point OKX & Bybit to mock servers; disable MEXC for focused 2-exchange test
	okxCfg := exchangesCfg.Exchanges["okx"]
	okxCfg.BaseURL = okxSrv.URL
	okxCfg.SymbolAllowlist = []string{"BTC"}
	exchangesCfg.Exchanges["okx"] = okxCfg

	bybitCfg := exchangesCfg.Exchanges["bybit"]
	bybitCfg.BaseURL = bybitSrv.URL
	bybitCfg.SymbolAllowlist = []string{"BTC"}
	exchangesCfg.Exchanges["bybit"] = bybitCfg

	mexcCfg := exchangesCfg.Exchanges["mexc"]
	mexcCfg.Enabled = false
	exchangesCfg.Exchanges["mexc"] = mexcCfg

	kv := sink.NewMemoryKVStore()
	runner, err := NewRunner(scannerCfg, exchangesCfg, BuildDefaultRegistry(), kv)
	if err != nil {
		t.Fatalf("NewRunner: %v", err)
	}

	ctx := context.Background()
	runner.PollOnce(ctx)
	published, err := runner.RunTick(ctx, nowMs)
	if err != nil {
		t.Fatalf("RunTick: %v", err)
	}
	if published == 0 {
		t.Fatalf("expected published signals > 0, got 0")
	}

	msgs := kv.PublishedMessages(domain.KeyCoreSignals)
	if len(msgs) != published {
		t.Fatalf("published messages count = %d, want %d", len(msgs), published)
	}

	var sig domain.SignalPayload
	if err := json.Unmarshal([]byte(msgs[0]), &sig); err != nil {
		t.Fatalf("unmarshal published signal: %v", err)
	}
	if err := sig.Validate(); err != nil {
		t.Fatalf("signal failed v3.1 contract validation: %v", err)
	}
}
