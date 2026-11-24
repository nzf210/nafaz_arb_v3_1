package okx

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
		{"BTC-USDT", domain.MarketTypeSpot, "BTC/USDT"},
		{"ETH-USDT", domain.MarketTypeSpot, "ETH/USDT"},
		{"SOL-USDT", domain.MarketTypeSpot, "SOL/USDT"},
		{"BTC-USDT-SWAP", domain.MarketTypeLinearPerp, "BTC/USDT:USDT"},
		{"ETH-USDT-SWAP", domain.MarketTypeLinearPerp, "ETH/USDT:USDT"},
		{"SOL-USDT-SWAP", domain.MarketTypeLinearPerp, "SOL/USDT:USDT"},
	}

	for _, tc := range cases {
		gotCanon, err := a.NormalizeSymbol(tc.raw, tc.mt)
		if err != nil {
			t.Fatalf("NormalizeSymbol(%q, %q) failed: %v", tc.raw, tc.mt, err)
		}
		if gotCanon != tc.canon {
			t.Fatalf("NormalizeSymbol(%q, %q) = %q, want %q", tc.raw, tc.mt, gotCanon, tc.canon)
		}
		gotRaw := a.ToRawSymbol(tc.canon)
		if gotRaw != tc.raw {
			t.Fatalf("ToRawSymbol(%q) = %q, want %q", tc.canon, gotRaw, tc.raw)
		}
	}
}

func mockOKXHandler(hits *atomic.Int32) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"code":"50011","msg":"Rate limit"}`))
			return
		}
		switch r.URL.Path {
		case "/api/v5/market/books":
			_, _ = w.Write([]byte(`{"code":"0","data":[{"asks":[["65010.5","150"]],"bids":[["65000.0","200"]],"ts":"1700000000000","volCcy24h":"15000000"}]}`))
		case "/api/v5/public/instruments":
			_, _ = w.Write([]byte(`{"code":"0","data":[{"instId":"BTC-USDT-SWAP","ctVal":"0.01"}]}`))
		case "/api/v5/public/funding-rate":
			if r.URL.Query().Get("instId") == "BAD-USDT-SWAP" {
				_, _ = w.Write([]byte(`{"code":"0","data":[{"instId":"BAD-USDT-SWAP","fundingRate":"0.0001","fundingTime":"1700000000000","nextFundingTime":"1700000000000"}]}`))
				return
			}
			_, _ = w.Write([]byte(`{"code":"0","data":[{"instId":"BTC-USDT-SWAP","fundingRate":"0.00035","fundingTime":"1700000000000","nextFundingTime":"1700028800000"}]}`))
		case "/api/v5/public/discount-rate-interest-free-quota":
			_, _ = w.Write([]byte(`{"code":"0","data":[{"ccy":"BTC","discountRate":"0.98"},{"ccy":"USDT","discountRate":"1.0"}]}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}
}

func TestFetchEndpointsAnd429Retry(t *testing.T) {
	var hits atomic.Int32
	srv := httptest.NewServer(mockOKXHandler(&hits))
	defer srv.Close()

	a, err := NewAdapter(domain.ExchangeConfig{
		BaseURL:      srv.URL,
		RateLimitRPS: 100,
		MaxRetries:   3,
		UnifiedMargin: true,
	})
	if err != nil {
		t.Fatal(err)
	}

	ob, err := a.FetchOrderBook(context.Background(), "BTC/USDT:USDT")
	if err != nil {
		t.Fatalf("FetchOrderBook err: %v", err)
	}
	if len(ob.Bids) != 1 || ob.Bids[0].Price != 65000.0 || ob.Bids[0].Quantity != 2.0 || ob.RawSymbol != "BTC-USDT-SWAP" {
		t.Fatalf("unexpected orderbook: %+v", ob)
	}

	fr, err := a.FetchFundingRate(context.Background(), "BTC/USDT:USDT")
	if err != nil {
		t.Fatalf("FetchFundingRate err: %v", err)
	}
	if fr.IntervalHours != 8 || fr.FundingRate != 0.00035 {
		t.Fatalf("unexpected funding snapshot: %+v", fr)
	}

	// Zero interval must be rejected
	if _, err := a.FetchFundingRate(context.Background(), "BAD/USDT:USDT"); err == nil {
		t.Fatalf("expected error on zero interval_hours")
	}

	col, err := a.FetchCollateralRatio(context.Background())
	if err != nil {
		t.Fatalf("FetchCollateralRatio err: %v", err)
	}
	if col.AssetRatios["BTC"] != 0.98 {
		t.Fatalf("unexpected collateral ratio: %+v", col)
	}
}
