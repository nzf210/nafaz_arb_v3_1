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
	// Notice: SymbolAllowlist is intentionally empty to verify automatic market discovery!
	okxCfg := exchangesCfg.Exchanges["okx"]
	okxCfg.BaseURL = okxSrv.URL
	okxCfg.SymbolAllowlist = nil
	exchangesCfg.Exchanges["okx"] = okxCfg

	bybitCfg := exchangesCfg.Exchanges["bybit"]
	bybitCfg.BaseURL = bybitSrv.URL
	bybitCfg.SymbolAllowlist = nil
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
		t.Fatalf("expected published signals > 0 from automatic discovery, got 0")
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

func newPhaseOKXServer(nowMs int64, phase *int) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v5/public/instruments":
			writeOKXInstrumentsFixture(w, r.URL.Query().Get("instType"), *phase)
		case "/api/v5/market/books":
			_, _ = w.Write([]byte(`{"code":"0","data":[{"asks":[["100.0","50"]],"bids":[["99.9","50"]],"ts":"` + jsonNumber(nowMs) + `","volCcy24h":"25000000"}]}`))
		case "/api/v5/public/funding-rate":
			_, _ = w.Write([]byte(`{"code":"0","data":[{"fundingRate":"-0.0005","fundingTime":"` + jsonNumber(nowMs) + `","nextFundingTime":"` + jsonNumber(nowMs+14400000) + `","intervalHours":4,"ts":"` + jsonNumber(nowMs) + `"}]}`))
		case "/api/v5/public/discount-rate-interest-free-quota":
			_, _ = w.Write([]byte(`{"code":"0","data":[{"ccy":"USDT","discountRate":"1.0"}]}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func writeOKXInstrumentsFixture(w http.ResponseWriter, instType string, phase int) {
	if instType == "SPOT" {
		_, _ = w.Write([]byte(`{"code":"0","data":[]}`))
		return
	}
	if phase == 1 {
		_, _ = w.Write([]byte(`{"code":"0","data":[
			{"instId":"BTC-USDT-SWAP","ctType":"linear","settleCcy":"USDT","ctVal":"1","state":"live"},
			{"instId":"ABC-USDT-SWAP","ctType":"linear","settleCcy":"USDT","ctVal":"1","state":"live"},
			{"instId":"ONLY1-USDT-SWAP","ctType":"linear","settleCcy":"USDT","ctVal":"1","state":"live"},
			{"instId":"BLOCKED-USDT-SWAP","ctType":"linear","settleCcy":"USDT","ctVal":"1","state":"live"}
		]}`))
		return
	}
	_, _ = w.Write([]byte(`{"code":"0","data":[
		{"instId":"BTC-USDT-SWAP","ctType":"linear","settleCcy":"USDT","ctVal":"1","state":"live"},
		{"instId":"NEWCOIN-USDT-SWAP","ctType":"linear","settleCcy":"USDT","ctVal":"1","state":"live"}
	]}`))
}

func newPhaseMEXCServer(nowMs int64) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v3/exchangeInfo":
			_, _ = w.Write([]byte(`{"symbols":[]}`))
		case "/api/v1/contract/detail":
			_, _ = w.Write([]byte(`{"success":true,"data":[
				{"symbol":"BTC_USDT","quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"state":0},
				{"symbol":"ABC_USDT","quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"state":0},
				{"symbol":"NEWCOIN_USDT","quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"state":0},
				{"symbol":"BLOCKED_USDT","quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"state":0}
			]}`))
		case "/api/v1/contract/funding_rate/BTC_USDT", "/api/v1/contract/funding_rate/ABC_USDT", "/api/v1/contract/funding_rate/NEWCOIN_USDT":
			_, _ = w.Write([]byte(`{"success":true,"data":{"symbol":"BTC_USDT","fundingRate":0.0012,"nextSettleTime":` + jsonNumber(nowMs+28800000) + `,"collectCycle":8,"timestamp":` + jsonNumber(nowMs) + `}}`))
		default:
			_, _ = w.Write([]byte(`{"success":true,"data":{"asks":[[100.1,50]],"bids":[[100.0,50]],"timestamp":` + jsonNumber(nowMs) + `,"volume24h":25000000}}`))
		}
	}))
}

func TestDynamicDiscoveryMatchingDelistingAndFaultIsolation(t *testing.T) {
	nowMs := time.Now().UnixMilli()
	phase := 1

	okxSrv := newPhaseOKXServer(nowMs, &phase)
	defer okxSrv.Close()

	mexcSrv := newPhaseMEXCServer(nowMs)
	defer mexcSrv.Close()

	// Bybit simulates an API outage (HTTP 500) to verify fault isolation
	bybitSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer bybitSrv.Close()

	scannerCfg, err := domain.LoadScannerConfig("../../../configs/scanner.base.yaml")
	if err != nil {
		t.Fatal(err)
	}
	exchangesCfg, err := domain.LoadExchangesConfig("../../../configs/exchanges.yaml")
	if err != nil {
		t.Fatal(err)
	}

	for name, url := range map[string]string{"okx": okxSrv.URL, "bybit": bybitSrv.URL, "mexc": mexcSrv.URL} {
		c := exchangesCfg.Exchanges[name]
		c.BaseURL = url
		c.MaxRetries = 0
		c.MarketDiscovery.Blocklist = []string{"BLOCKED"}
		exchangesCfg.Exchanges[name] = c
	}

	kv := sink.NewMemoryKVStore()
	runner, err := NewRunner(scannerCfg, exchangesCfg, BuildDefaultRegistry(), kv)
	if err != nil {
		t.Fatal(err)
	}

	// Duplicate task acquisition check
	if !runner.tryAcquireTask("okx:BTC/USDT:USDT") {
		t.Fatal("expected first task acquire to succeed")
	}
	if runner.tryAcquireTask("okx:BTC/USDT:USDT") {
		t.Fatal("expected duplicate concurrent task acquire to be rejected")
	}
	runner.releaseTask("okx:BTC/USDT:USDT")

	ctx := context.Background()
	runner.PollOnce(ctx)
	match1 := runner.MatchMarkets()
	if len(match1.PerpPerpRoutes["ABC"]) != 2 || len(match1.PerpPerpRoutes["BTC"]) != 2 {
		t.Fatalf("phase 1 expected ABC and BTC matched between OKX and MEXC, got %+v", match1.PerpPerpRoutes)
	}
	if _, hasBlocked := match1.PerpPerpRoutes["BLOCKED"]; hasBlocked {
		t.Fatal("blocklisted BLOCKED symbol must not be matched")
	}
	if _, hasOnly1 := match1.PerpPerpRoutes["ONLY1"]; hasOnly1 {
		t.Fatal("single-exchange ONLY1 symbol must not be matched")
	}

	// Phase 2: ABC delisted from OKX, NEWCOIN listed on OKX (matches MEXC)
	phase = 2
	runner.DiscoverMarkets(ctx, true)
	runner.PollOnce(ctx)
	match2 := runner.MatchMarkets()
	if _, hasABC := match2.PerpPerpRoutes["ABC"]; hasABC {
		t.Fatal("phase 2 expected delisted ABC to be evicted from matched routes")
	}
	if _, ok := runner.Store.GetOrderBook("okx", "ABC/USDT:USDT"); ok {
		t.Fatal("phase 2 expected delisted ABC orderbook to be evicted from StateStore")
	}
	if len(match2.PerpPerpRoutes["NEWCOIN"]) != 2 {
		t.Fatalf("phase 2 expected newly discovered NEWCOIN to be matched in 2 directions, got %+v", match2.PerpPerpRoutes["NEWCOIN"])
	}
}

func jsonNumber(v int64) string {
	b, _ := json.Marshal(v)
	return string(b)
}
