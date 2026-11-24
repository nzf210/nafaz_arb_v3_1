package adapter_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/adapter/bybit"
	"nafaz_arb/internal/adapter/mexc"
	"nafaz_arb/internal/adapter/okx"
	"nafaz_arb/internal/domain"
)

func TestRegistryAndAdapterContracts(t *testing.T) {
	reg := adapter.NewRegistry()
	reg.Register("okx", okx.NewAdapter)
	reg.Register("bybit", bybit.NewAdapter)
	reg.Register("mexc", mexc.NewAdapter)

	wantNames := []string{"bybit", "mexc", "okx"}
	if !reflect.DeepEqual(reg.Names(), wantNames) {
		t.Fatalf("Names() = %v, want %v", reg.Names(), wantNames)
	}

	if _, err := reg.Create("unknown", domain.ExchangeConfig{}); err == nil {
		t.Fatalf("expected error for unregistered exchange")
	}

	for _, name := range wantNames {
		var attempts atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if attempts.Add(1) == 1 {
				w.WriteHeader(http.StatusTooManyRequests)
				return
			}
			// Return canonical fallback JSON understood by all adapters
			_, _ = w.Write([]byte(`{
				"bids": [{"price": 60000, "quantity": 1.5}],
				"asks": [{"price": 60010, "quantity": 1.5}],
				"volume_24h_usdt": 10000000,
				"funding_rate": 0.0004,
				"interval_hours": 8,
				"next_settlement_ms": 1700028800000,
				"timestamp_ms": 1700000000000,
				"asset_ratios": {"USDT": 1.0, "BTC": 0.95}
			}`))
		}))
		defer srv.Close()

		ad, err := reg.Create(name, domain.ExchangeConfig{
			Enabled:       true,
			BaseURL:       srv.URL,
			RateLimitRPS:  50,
			MaxRetries:    2,
			UnifiedMargin: true,
		})
		if err != nil {
			t.Fatalf("Create(%q) failed: %v", name, err)
		}
		if ad.Name() != name {
			t.Fatalf("Name() = %q, want %q", ad.Name(), name)
		}

		// Two-way symbol roundtrip for BTC, ETH, SOL across Spot & Linear Perp
		for _, base := range []string{"BTC", "ETH", "SOL"} {
			for _, mt := range []domain.MarketType{domain.MarketTypeSpot, domain.MarketTypeLinearPerp} {
				canon, err := domain.NewCanonSymbol(base, mt)
				if err != nil {
					t.Fatal(err)
				}
				raw := ad.ToRawSymbol(canon)
				if raw == "" {
					t.Fatalf("%s: empty raw symbol for %s", name, canon)
				}
				roundtrip, err := ad.NormalizeSymbol(raw, mt)
				if err != nil {
					t.Fatalf("%s: NormalizeSymbol(%q, %q) failed: %v", name, raw, mt, err)
				}
				if roundtrip != canon {
					t.Fatalf("%s: roundtrip %q -> %q != %q", name, raw, roundtrip, canon)
				}
			}
		}

		ob, err := ad.FetchOrderBook(context.Background(), "BTC/USDT:USDT")
		if err != nil {
			t.Fatalf("%s FetchOrderBook failed: %v", name, err)
		}
		if ob.Exchange != name || len(ob.Bids) == 0 {
			t.Fatalf("%s invalid orderbook: %+v", name, ob)
		}

		fr, err := ad.FetchFundingRate(context.Background(), "BTC/USDT:USDT")
		if err != nil {
			t.Fatalf("%s FetchFundingRate failed: %v", name, err)
		}
		if fr.IntervalHours <= 0 {
			t.Fatalf("%s expected IntervalHours > 0, got %d", name, fr.IntervalHours)
		}

		col, err := ad.FetchCollateralRatio(context.Background())
		if err != nil {
			t.Fatalf("%s FetchCollateralRatio failed: %v", name, err)
		}
		if col.Exchange != name {
			t.Fatalf("%s unexpected collateral exchange: %+v", name, col)
		}
	}
}
