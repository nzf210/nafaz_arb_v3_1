package mexc

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"nafaz_arb/internal/domain"
)

func TestSymbolMapping(t *testing.T) {
	a, err := NewAdapter(domain.ExchangeConfig{BaseURL: "http://localhost", RateLimitRPS: 10})
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		raw   string
		mt    domain.MarketType
		canon domain.CanonSymbol
	}{
		{"BTCUSDT", domain.MarketTypeSpot, "BTC/USDT"},
		{"ETHUSDT", domain.MarketTypeSpot, "ETH/USDT"},
		{"SOLUSDT", domain.MarketTypeSpot, "SOL/USDT"},
		{"BTC_USDT", domain.MarketTypeLinearPerp, "BTC/USDT:USDT"},
		{"ETH_USDT", domain.MarketTypeLinearPerp, "ETH/USDT:USDT"},
		{"SOL_USDT", domain.MarketTypeLinearPerp, "SOL/USDT:USDT"},
	}

	for _, tc := range cases {
		gotCanon, err := a.NormalizeSymbol(tc.raw, tc.mt)
		if err != nil || gotCanon != tc.canon {
			t.Fatalf("NormalizeSymbol(%q, %q) = %q, %v; want %q", tc.raw, tc.mt, gotCanon, err, tc.canon)
		}
		gotRaw := a.ToRawSymbol(tc.canon)
		if gotRaw != tc.raw {
			t.Fatalf("ToRawSymbol(%q) = %q; want %q", tc.canon, gotRaw, tc.raw)
		}
	}

	// Spot and Perp formats must not be interchangeable on MEXC
	if _, err := a.NormalizeSymbol("BTC_USDT", domain.MarketTypeSpot); err == nil {
		t.Fatal("expected MEXC contract symbol BTC_USDT to be rejected for spot")
	}
	if _, err := a.NormalizeSymbol("BTCUSDT", domain.MarketTypeLinearPerp); err == nil {
		t.Fatal("expected MEXC spot symbol BTCUSDT to be rejected for linear_perp")
	}
}

func TestFetchMarketsMEXC(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v3/exchangeInfo":
			_, _ = w.Write([]byte(`{"symbols":[
				{"symbol":"BTCUSDT","status":"1","baseAsset":"BTC","quoteAsset":"USDT","isSpotTradingAllowed":true},
				{"symbol":"DISABLEDUSDT","status":"1","baseAsset":"DISABLED","quoteAsset":"USDT","isSpotTradingAllowed":false}
			]}`))
		case "/api/v1/contract/detail":
			_, _ = w.Write([]byte(`{"success":true,"data":[
				{"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":0.0001,"state":0},
				{"symbol":"ABC_USDT","baseCoin":"ABC","quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"state":0},
				{"symbol":"OFFLINE_USDT","baseCoin":"OFFLINE","quoteCoin":"USDT","settleCoin":"USDT","contractSize":1,"state":1}
			]}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	a, err := NewAdapter(domain.ExchangeConfig{BaseURL: srv.URL, RateLimitRPS: 50})
	if err != nil {
		t.Fatal(err)
	}
	markets, err := a.FetchMarkets(context.Background())
	if err != nil {
		t.Fatalf("FetchMarkets failed: %v", err)
	}
	if len(markets) != 3 {
		t.Fatalf("expected 3 active MEXC markets (1 spot + 2 linear perp), got %d: %+v", len(markets), markets)
	}
}

func TestFetchEndpointsAnd429Retry(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		switch r.URL.Path {
		case "/api/v1/contract/depth/BTC_USDT":
			_, _ = w.Write([]byte(`{"success":true,"data":{"asks":[[65100.0,35000]],"bids":[[65090.0,40000]],"timestamp":1700000000000,"volume24h":12000000}}`))
		case "/api/v1/contract/detail":
			_, _ = w.Write([]byte(`{"success":true,"data":{"symbol":"BTC_USDT","contractSize":0.0001}}`))
		case "/api/v1/contract/funding_rate/BTC_USDT":
			_, _ = w.Write([]byte(`{"success":true,"data":{"symbol":"BTC_USDT","fundingRate":0.0005,"nextSettleTime":1700028800000,"collectCycle":8,"timestamp":1700000000000}}`))
		case "/api/v1/contract/funding_rate/BAD_USDT":
			_, _ = w.Write([]byte(`{"success":true,"data":{"symbol":"BAD_USDT","fundingRate":0.0005,"nextSettleTime":1700028800000,"collectCycle":0,"timestamp":1700000000000}}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	a, err := NewAdapter(domain.ExchangeConfig{
		BaseURL:      srv.URL,
		RateLimitRPS: 100,
		MaxRetries:   2,
	})
	if err != nil {
		t.Fatal(err)
	}

	ob, err := a.FetchOrderBook(context.Background(), "BTC/USDT:USDT")
	if err != nil {
		t.Fatalf("FetchOrderBook err: %v", err)
	}
	if len(ob.Bids) != 1 || ob.Bids[0].Price != 65090.0 || ob.Bids[0].Quantity != 4.0 {
		t.Fatalf("unexpected orderbook: %+v", ob)
	}

	fr, err := a.FetchFundingRate(context.Background(), "BTC/USDT:USDT")
	if err != nil {
		t.Fatalf("FetchFundingRate err: %v", err)
	}
	if fr.IntervalHours != 8 || fr.FundingRate != 0.0005 {
		t.Fatalf("unexpected funding snapshot: %+v", fr)
	}

	if _, err := a.FetchFundingRate(context.Background(), "BAD/USDT:USDT"); err == nil {
		t.Fatalf("expected error on zero collectCycle")
	}
}
