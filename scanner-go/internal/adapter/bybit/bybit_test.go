package bybit

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

	bases := []string{"BTC", "ETH", "SOL"}
	for _, b := range bases {
		raw := b + "USDT"
		spotCanon := domain.CanonSymbol(b + "/USDT")
		perpCanon := domain.CanonSymbol(b + "/USDT:USDT")

		gotSpot, err := a.NormalizeSymbol(raw, domain.MarketTypeSpot)
		if err != nil || gotSpot != spotCanon {
			t.Fatalf("spot normalize(%q) = %q, %v", raw, gotSpot, err)
		}
		if a.ToRawSymbol(spotCanon) != raw {
			t.Fatalf("spot ToRawSymbol(%q) = %q", spotCanon, a.ToRawSymbol(spotCanon))
		}

		gotPerp, err := a.NormalizeSymbol(raw, domain.MarketTypeLinearPerp)
		if err != nil || gotPerp != perpCanon {
			t.Fatalf("perp normalize(%q) = %q, %v", raw, gotPerp, err)
		}
		if a.ToRawSymbol(perpCanon) != raw {
			t.Fatalf("perp ToRawSymbol(%q) = %q", perpCanon, a.ToRawSymbol(perpCanon))
		}
	}

	if _, err := a.NormalizeSymbol("BTC-26DEC25", domain.MarketTypeLinearPerp); err == nil {
		t.Fatal("expected dated futures symbol with hyphen to be rejected by Bybit NormalizeSymbol")
	}
}

func TestFetchMarketsBybit(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Query().Get("category") {
		case "spot":
			_, _ = w.Write([]byte(`{"retCode":0,"result":{"list":[
				{"symbol":"BTCUSDT","baseCoin":"BTC","quoteCoin":"USDT","status":"Trading"},
				{"symbol":"HALTUSDT","baseCoin":"HALT","quoteCoin":"USDT","status":"Closed"}
			]}}`))
		case "linear":
			_, _ = w.Write([]byte(`{"retCode":0,"result":{"list":[
				{"symbol":"BTCUSDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractType":"LinearPerpetual","status":"Trading"},
				{"symbol":"XYZUSDT","baseCoin":"XYZ","quoteCoin":"USDT","settleCoin":"USDT","contractType":"LinearPerpetual","status":"Trading"},
				{"symbol":"BTC-26DEC25","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT","contractType":"LinearFutures","status":"Trading"}
			]}}`))
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
		t.Fatalf("expected 3 active Bybit markets (1 spot + 2 linear perp), got %d: %+v", len(markets), markets)
	}
}

func mockBybitHandler(hits *atomic.Int32) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		switch r.URL.Path {
		case "/v5/market/orderbook":
			_, _ = w.Write([]byte(`{"retCode":0,"result":{"s":"BTCUSDT","b":[["64900","1.2"]],"a":[["64910","0.8"]],"ts":1700000000000,"turnover24h":"25000000"}}`))
		case "/v5/market/tickers":
			if r.URL.Query().Get("symbol") == "BADUSDT" {
				_, _ = w.Write([]byte(`{"retCode":0,"time":1700000000000,"result":{"list":[{"symbol":"BADUSDT","fundingRate":"0.0001","nextFundingTime":"1700028800000","fundingIntervalHour":"0"}]}}`))
				return
			}
			_, _ = w.Write([]byte(`{"retCode":0,"time":1700000000000,"result":{"list":[{"symbol":"BTCUSDT","fundingRate":"0.0002","nextFundingTime":"1700014400000","fundingIntervalHour":"4"}]}}`))
		case "/v5/account/collateral-info":
			_, _ = w.Write([]byte(`{"retCode":0,"result":{"list":[{"currency":"BTC","collateralRatio":"0.95"}]}}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}
}

func TestFetchEndpointsAnd429Retry(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(mockBybitHandler(&hits))
	defer srv.Close()

	a, err := NewAdapter(domain.ExchangeConfig{
		BaseURL:      srv.URL,
		RateLimitRPS: 100,
		MaxRetries:   2,
	})
	if err != nil {
		t.Fatal(err)
	}

	ob, err := a.FetchOrderBook(context.Background(), "BTC/USDT")
	if err != nil {
		t.Fatalf("FetchOrderBook err: %v", err)
	}
	if len(ob.Bids) != 1 || ob.Bids[0].Price != 64900 {
		t.Fatalf("unexpected orderbook: %+v", ob)
	}

	fr, err := a.FetchFundingRate(context.Background(), "BTC/USDT:USDT")
	if err != nil {
		t.Fatalf("FetchFundingRate err: %v", err)
	}
	if fr.IntervalHours != 4 || fr.FundingRate != 0.0002 {
		t.Fatalf("unexpected funding snapshot: %+v", fr)
	}

	if _, err := a.FetchFundingRate(context.Background(), "BAD/USDT:USDT"); err == nil {
		t.Fatalf("expected error on zero interval_hours")
	}

	col, err := a.FetchCollateralRatio(context.Background())
	if err != nil {
		t.Fatalf("FetchCollateralRatio err: %v", err)
	}
	if col.AssetRatios["BTC"] != 0.95 {
		t.Fatalf("unexpected collateral: %+v", col)
	}
}
