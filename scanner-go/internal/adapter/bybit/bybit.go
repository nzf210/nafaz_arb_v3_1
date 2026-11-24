package bybit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/adapter/resilience"
	"nafaz_arb/internal/domain"
)

type Adapter struct {
	cfg      domain.ExchangeConfig
	client   *resilience.Client
	volMu    sync.RWMutex
	volCache map[string]cachedVol
}

type cachedVol struct {
	vol   float64
	expMs int64
}

func NewAdapter(cfg domain.ExchangeConfig) (adapter.ExchangeAdapter, error) {
	if strings.TrimSpace(cfg.BaseURL) == "" {
		return nil, errors.New("bybit base_url is required")
	}
	return &Adapter{
		cfg:      cfg,
		client:   resilience.NewClientFromExchangeConfig(cfg),
		volCache: make(map[string]cachedVol),
	}, nil
}

func (a *Adapter) Name() string {
	return "bybit"
}

func (a *Adapter) Capabilities() adapter.Capabilities {
	return adapter.Capabilities{
		HasSpot:           true,
		HasPerp:           true,
		HasCollateralInfo: true,
		UnifiedMargin:     a.cfg.UnifiedMargin,
	}
}

func (a *Adapter) Client() *resilience.Client {
	return a.client
}

func (a *Adapter) NormalizeSymbol(raw string, marketType domain.MarketType) (domain.CanonSymbol, error) {
	s := strings.ToUpper(strings.TrimSpace(raw))
	s = strings.ReplaceAll(s, "-", "")
	s = strings.ReplaceAll(s, "_", "")
	if !strings.HasSuffix(s, "USDT") {
		return "", fmt.Errorf("bybit: symbol %q must end with USDT", raw)
	}
	base := strings.TrimSuffix(s, "USDT")
	return domain.NewCanonSymbol(base, marketType)
}

func (a *Adapter) ToRawSymbol(canon domain.CanonSymbol) string {
	if err := canon.Validate(); err != nil {
		return ""
	}
	base := canon.BaseAsset()
	if base == "" {
		return ""
	}
	return base + "USDT"
}

func (a *Adapter) FetchOrderBook(ctx context.Context, symbol domain.CanonSymbol) (*domain.OrderBookSnapshot, error) {
	mt, err := symbol.MarketType()
	if err != nil {
		return nil, err
	}
	category := "spot"
	if mt == domain.MarketTypeLinearPerp {
		category = "linear"
	}
	rawSym := a.ToRawSymbol(symbol)
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/v5/market/orderbook?category=" + category + "&symbol=" + rawSym + "&limit=25"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	var v5 struct {
		RetCode int `json:"retCode"`
		Result  struct {
			S         string     `json:"s"`
			B         [][]string `json:"b"`
			A         [][]string `json:"a"`
			Ts        int64      `json:"ts"`
			Vol24hUSD string     `json:"turnover24h"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &v5); err == nil && (len(v5.Result.B) > 0 || len(v5.Result.A) > 0) {
		bids, err := parseLevels(v5.Result.B)
		if err != nil {
			return nil, err
		}
		asks, err := parseLevels(v5.Result.A)
		if err != nil {
			return nil, err
		}
		ts := v5.Result.Ts
		if ts <= 0 {
			ts = time.Now().UnixMilli()
		}
		vol, _ := strconv.ParseFloat(v5.Result.Vol24hUSD, 64)
		if vol <= 0 {
			vol = a.fetchTickerTurnover24h(ctx, category, rawSym)
		}
		return &domain.OrderBookSnapshot{
			Exchange:      a.Name(),
			CanonSymbol:   symbol,
			RawSymbol:     rawSym,
			MarketType:    mt,
			Bids:          bids,
			Asks:          asks,
			Volume24hUSDT: vol,
			TimestampMs:   ts,
		}, nil
	}

	var direct domain.OrderBookSnapshot
	if err := json.Unmarshal(body, &direct); err != nil {
		return nil, fmt.Errorf("bybit: failed to decode orderbook: %w", err)
	}
	if direct.Exchange == "" {
		direct.Exchange = a.Name()
	}
	if direct.CanonSymbol == "" {
		direct.CanonSymbol = symbol
	}
	if direct.RawSymbol == "" {
		direct.RawSymbol = rawSym
	}
	if direct.MarketType == "" {
		direct.MarketType = mt
	}
	if direct.TimestampMs <= 0 {
		direct.TimestampMs = time.Now().UnixMilli()
	}
	return &direct, nil
}

func (a *Adapter) FetchFundingRate(ctx context.Context, symbol domain.CanonSymbol) (*domain.FundingSnapshot, error) {
	mt, err := symbol.MarketType()
	if err != nil {
		return nil, err
	}
	if mt != domain.MarketTypeLinearPerp {
		return nil, fmt.Errorf("bybit: funding rate only valid for linear_perp, got %s", symbol)
	}
	rawSym := a.ToRawSymbol(symbol)
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/v5/market/tickers?category=linear&symbol=" + rawSym
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}
	if snap, ok, err := a.parseV5Funding(body, symbol, rawSym); ok || err != nil {
		return snap, err
	}
	return a.parseDirectFunding(body, symbol, rawSym)
}

func (a *Adapter) parseV5Funding(body []byte, symbol domain.CanonSymbol, rawSym string) (*domain.FundingSnapshot, bool, error) {
	var v5 struct {
		Time   int64 `json:"time"`
		Result struct {
			List []struct {
				FundingRate         string `json:"fundingRate"`
				NextFundingTime     string `json:"nextFundingTime"`
				FundingIntervalHour string `json:"fundingIntervalHour"`
				IntervalHours       int    `json:"intervalHours"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &v5); err != nil || len(v5.Result.List) == 0 {
		return nil, false, nil
	}
	item := v5.Result.List[0]
	rate, err := strconv.ParseFloat(item.FundingRate, 64)
	if err != nil {
		return nil, true, fmt.Errorf("bybit: invalid fundingRate %q: %w", item.FundingRate, err)
	}
	interval := item.IntervalHours
	if interval == 0 && item.FundingIntervalHour != "" {
		interval, _ = strconv.Atoi(item.FundingIntervalHour)
	}
	nextSettle, _ := strconv.ParseInt(item.NextFundingTime, 10, 64)
	ts := v5.Time
	if ts <= 0 {
		ts = time.Now().UnixMilli()
	}
	snap := &domain.FundingSnapshot{
		Exchange:         a.Name(),
		CanonSymbol:      symbol,
		RawSymbol:        rawSym,
		FundingRate:      rate,
		IntervalHours:    interval,
		NextSettlementMs: nextSettle,
		TimestampMs:      ts,
	}
	if err := snap.Validate(); err != nil {
		return nil, true, fmt.Errorf("bybit: %w", err)
	}
	return snap, true, nil
}

func (a *Adapter) parseDirectFunding(body []byte, symbol domain.CanonSymbol, rawSym string) (*domain.FundingSnapshot, error) {
	var direct domain.FundingSnapshot
	if err := json.Unmarshal(body, &direct); err != nil {
		return nil, fmt.Errorf("bybit: failed to decode funding rate: %w", err)
	}
	if direct.Exchange == "" {
		direct.Exchange = a.Name()
	}
	if direct.CanonSymbol == "" {
		direct.CanonSymbol = symbol
	}
	if direct.RawSymbol == "" {
		direct.RawSymbol = rawSym
	}
	if direct.TimestampMs <= 0 {
		direct.TimestampMs = time.Now().UnixMilli()
	}
	if err := direct.Validate(); err != nil {
		return nil, fmt.Errorf("bybit: %w", err)
	}
	return &direct, nil
}

func (a *Adapter) FetchCollateralRatio(ctx context.Context) (*domain.CollateralSnapshot, error) {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/v5/account/collateral-info"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		// Public scanner without auth falls back to USDT 1.0 collateral ratio
		return &domain.CollateralSnapshot{
			Exchange:    a.Name(),
			AssetRatios: map[string]float64{"USDT": 1.0},
			TimestampMs: time.Now().UnixMilli(),
		}, nil
	}

	var v5 struct {
		RetCode int `json:"retCode"`
		Result  struct {
			List []struct {
				Currency        string `json:"currency"`
				CollateralRatio string `json:"collateralRatio"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &v5); err == nil && len(v5.Result.List) > 0 {
		ratios := make(map[string]float64, len(v5.Result.List))
		for _, item := range v5.Result.List {
			ccy := strings.ToUpper(strings.TrimSpace(item.Currency))
			if ccy == "" {
				continue
			}
			if r, err := strconv.ParseFloat(item.CollateralRatio, 64); err == nil {
				ratios[ccy] = r
			}
		}
		return &domain.CollateralSnapshot{
			Exchange:    a.Name(),
			AssetRatios: ratios,
			TimestampMs: time.Now().UnixMilli(),
		}, nil
	}

	var direct domain.CollateralSnapshot
	if err := json.Unmarshal(body, &direct); err == nil && len(direct.AssetRatios) > 0 {
		if direct.Exchange == "" {
			direct.Exchange = a.Name()
		}
		if direct.TimestampMs <= 0 {
			direct.TimestampMs = time.Now().UnixMilli()
		}
		return &direct, nil
	}
	return &domain.CollateralSnapshot{
		Exchange:    a.Name(),
		AssetRatios: map[string]float64{"USDT": 1.0},
		TimestampMs: time.Now().UnixMilli(),
	}, nil
}

func (a *Adapter) fetchTickerTurnover24h(ctx context.Context, category, rawSym string) float64 {
	cacheKey := category + ":" + rawSym
	nowMs := time.Now().UnixMilli()
	a.volMu.RLock()
	if entry, ok := a.volCache[cacheKey]; ok && nowMs < entry.expMs {
		a.volMu.RUnlock()
		return entry.vol
	}
	a.volMu.RUnlock()

	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/v5/market/tickers?category=" + category + "&symbol=" + rawSym
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return 0
	}
	var resp struct {
		Result struct {
			List []struct {
				Turnover24h string `json:"turnover24h"`
			} `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &resp); err == nil && len(resp.Result.List) > 0 {
		vol, _ := strconv.ParseFloat(resp.Result.List[0].Turnover24h, 64)
		if vol > 0 {
			a.volMu.Lock()
			a.volCache[cacheKey] = cachedVol{vol: vol, expMs: nowMs + 60_000}
			a.volMu.Unlock()
		}
		return vol
	}
	return 0
}

func parseLevels(raw [][]string) ([]domain.OrderBookLevel, error) {
	levels := make([]domain.OrderBookLevel, 0, len(raw))
	for _, entry := range raw {
		if len(entry) < 2 {
			continue
		}
		p, err := strconv.ParseFloat(entry[0], 64)
		if err != nil {
			return nil, err
		}
		q, err := strconv.ParseFloat(entry[1], 64)
		if err != nil {
			return nil, err
		}
		levels = append(levels, domain.OrderBookLevel{Price: p, Quantity: q})
	}
	return levels, nil
}
