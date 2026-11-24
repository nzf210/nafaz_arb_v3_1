package mexc

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
	cfg            domain.ExchangeConfig
	client         *resilience.Client
	contractSizeMu sync.RWMutex
	contractSize   map[string]float64
	volMu          sync.RWMutex
	volCache       map[string]cachedVol
}

type cachedVol struct {
	vol   float64
	expMs int64
}

func NewAdapter(cfg domain.ExchangeConfig) (adapter.ExchangeAdapter, error) {
	if strings.TrimSpace(cfg.BaseURL) == "" {
		return nil, errors.New("mexc base_url is required")
	}
	return &Adapter{
		cfg:          cfg,
		client:       resilience.NewClientFromExchangeConfig(cfg),
		contractSize: make(map[string]float64),
		volCache:     make(map[string]cachedVol),
	}, nil
}

func (a *Adapter) Name() string {
	return "mexc"
}

func (a *Adapter) Capabilities() adapter.Capabilities {
	return adapter.Capabilities{
		HasSpot:           true,
		HasPerp:           true,
		HasCollateralInfo: false,
		UnifiedMargin:     a.cfg.UnifiedMargin,
	}
}

func (a *Adapter) Client() *resilience.Client {
	return a.client
}

func (a *Adapter) NormalizeSymbol(raw string, marketType domain.MarketType) (domain.CanonSymbol, error) {
	s := strings.ToUpper(strings.TrimSpace(raw))
	switch marketType {
	case domain.MarketTypeLinearPerp:
		if strings.HasSuffix(s, "_USDT") {
			base := strings.TrimSuffix(s, "_USDT")
			return domain.NewCanonSymbol(base, domain.MarketTypeLinearPerp)
		}
		if strings.HasSuffix(s, "USDT") {
			base := strings.TrimSuffix(s, "USDT")
			return domain.NewCanonSymbol(base, domain.MarketTypeLinearPerp)
		}
		return "", fmt.Errorf("mexc: unsupported linear_perp raw symbol %q", raw)
	case domain.MarketTypeSpot:
		if strings.HasSuffix(s, "_USDT") {
			base := strings.TrimSuffix(s, "_USDT")
			return domain.NewCanonSymbol(base, domain.MarketTypeSpot)
		}
		if strings.HasSuffix(s, "USDT") {
			base := strings.TrimSuffix(s, "USDT")
			return domain.NewCanonSymbol(base, domain.MarketTypeSpot)
		}
		return "", fmt.Errorf("mexc: unsupported spot raw symbol %q", raw)
	default:
		return "", fmt.Errorf("mexc: unsupported market type %q", marketType)
	}
}

func (a *Adapter) ToRawSymbol(canon domain.CanonSymbol) string {
	mt, err := canon.MarketType()
	if err != nil {
		return ""
	}
	base := canon.BaseAsset()
	if base == "" {
		return ""
	}
	if mt == domain.MarketTypeLinearPerp {
		return base + "_USDT"
	}
	return base + "USDT"
}

func (a *Adapter) FetchOrderBook(ctx context.Context, symbol domain.CanonSymbol) (*domain.OrderBookSnapshot, error) {
	mt, err := symbol.MarketType()
	if err != nil {
		return nil, err
	}
	rawSym := a.ToRawSymbol(symbol)
	path := "/api/v3/depth?symbol=" + rawSym + "&limit=20"
	if mt == domain.MarketTypeLinearPerp {
		path = "/api/v1/contract/depth/" + rawSym + "?limit=20"
	}
	url := strings.TrimRight(a.cfg.BaseURL, "/") + path
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}
	if snap := a.tryParseContractBook(ctx, body, symbol, rawSym, mt); snap != nil {
		return snap, nil
	}
	if snap, ok, err := a.tryParseSpotBook(ctx, body, symbol, rawSym, mt); ok || err != nil {
		return snap, err
	}
	return a.parseDirectBook(body, symbol, rawSym, mt)
}

func (a *Adapter) tryParseContractBook(
	ctx context.Context, body []byte, symbol domain.CanonSymbol, rawSym string, mt domain.MarketType,
) *domain.OrderBookSnapshot {
	var contractResp struct {
		Data struct {
			Asks      [][]float64 `json:"asks"`
			Bids      [][]float64 `json:"bids"`
			Timestamp int64       `json:"timestamp"`
			Volume24h float64     `json:"volume24h"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &contractResp); err != nil || (len(contractResp.Data.Bids) == 0 && len(contractResp.Data.Asks) == 0) {
		return nil
	}
	ts := contractResp.Data.Timestamp
	if ts <= 0 {
		ts = time.Now().UnixMilli()
	}
	vol := contractResp.Data.Volume24h
	if vol <= 0 {
		vol = a.fetchTickerVolume24h(ctx, rawSym, mt)
	}
	mult := a.contractMultiplier(ctx, rawSym)
	return &domain.OrderBookSnapshot{
		Exchange:      a.Name(),
		CanonSymbol:   symbol,
		RawSymbol:     rawSym,
		MarketType:    mt,
		Bids:          parseFloatLevels(contractResp.Data.Bids, mult),
		Asks:          parseFloatLevels(contractResp.Data.Asks, mult),
		Volume24hUSDT: vol,
		TimestampMs:   ts,
	}
}

func (a *Adapter) tryParseSpotBook(
	ctx context.Context, body []byte, symbol domain.CanonSymbol, rawSym string, mt domain.MarketType,
) (*domain.OrderBookSnapshot, bool, error) {
	var spotResp struct {
		Bids      [][]string `json:"bids"`
		Asks      [][]string `json:"asks"`
		Timestamp int64      `json:"timestamp"`
		Volume24h float64    `json:"volume_24h_usdt"`
	}
	if err := json.Unmarshal(body, &spotResp); err != nil || (len(spotResp.Bids) == 0 && len(spotResp.Asks) == 0) {
		return nil, false, nil
	}
	bids, err := parseStringLevels(spotResp.Bids)
	if err != nil {
		return nil, true, err
	}
	asks, err := parseStringLevels(spotResp.Asks)
	if err != nil {
		return nil, true, err
	}
	ts := spotResp.Timestamp
	if ts <= 0 {
		ts = time.Now().UnixMilli()
	}
	vol := spotResp.Volume24h
	if vol <= 0 {
		vol = a.fetchTickerVolume24h(ctx, rawSym, mt)
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
	}, true, nil
}

func (a *Adapter) parseDirectBook(
	body []byte, symbol domain.CanonSymbol, rawSym string, mt domain.MarketType,
) (*domain.OrderBookSnapshot, error) {
	var direct domain.OrderBookSnapshot
	if err := json.Unmarshal(body, &direct); err != nil {
		return nil, fmt.Errorf("mexc: failed to decode orderbook: %w", err)
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
		return nil, fmt.Errorf("mexc: funding rate only valid for linear_perp, got %s", symbol)
	}
	rawSym := a.ToRawSymbol(symbol)
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v1/contract/funding_rate/" + rawSym
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	var v1 struct {
		Success bool `json:"success"`
		Data    struct {
			Symbol         string  `json:"symbol"`
			FundingRate    float64 `json:"fundingRate"`
			NextSettleTime int64   `json:"nextSettleTime"`
			CollectCycle   int     `json:"collectCycle"`
			Timestamp      int64   `json:"timestamp"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &v1); err == nil && (v1.Success || v1.Data.Symbol != "" || v1.Data.CollectCycle != 0) {
		ts := v1.Data.Timestamp
		if ts <= 0 {
			ts = time.Now().UnixMilli()
		}
		snap := &domain.FundingSnapshot{
			Exchange:         a.Name(),
			CanonSymbol:      symbol,
			RawSymbol:        rawSym,
			FundingRate:      v1.Data.FundingRate,
			IntervalHours:    v1.Data.CollectCycle,
			NextSettlementMs: v1.Data.NextSettleTime,
			TimestampMs:      ts,
		}
		if err := snap.Validate(); err != nil {
			return nil, fmt.Errorf("mexc: %w", err)
		}
		return snap, nil
	}

	var direct domain.FundingSnapshot
	if err := json.Unmarshal(body, &direct); err != nil {
		return nil, fmt.Errorf("mexc: failed to decode funding rate: %w", err)
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
		return nil, fmt.Errorf("mexc: %w", err)
	}
	return &direct, nil
}

func (a *Adapter) FetchCollateralRatio(ctx context.Context) (*domain.CollateralSnapshot, error) {
	return &domain.CollateralSnapshot{
		Exchange:    a.Name(),
		AssetRatios: map[string]float64{"USDT": 1.0},
		TimestampMs: time.Now().UnixMilli(),
	}, nil
}

func (a *Adapter) fetchTickerVolume24h(ctx context.Context, rawSym string, mt domain.MarketType) float64 {
	cacheKey := string(mt) + ":" + rawSym
	nowMs := time.Now().UnixMilli()
	a.volMu.RLock()
	if entry, ok := a.volCache[cacheKey]; ok && nowMs < entry.expMs {
		a.volMu.RUnlock()
		return entry.vol
	}
	a.volMu.RUnlock()

	var vol float64
	if mt == domain.MarketTypeLinearPerp {
		url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v1/contract/ticker?symbol=" + rawSym
		body, _, err := a.client.GetJSON(ctx, url)
		if err != nil {
			return 0
		}
		var resp struct {
			Data struct {
				Amount24 float64 `json:"amount24"`
			} `json:"data"`
		}
		if err := json.Unmarshal(body, &resp); err == nil {
			vol = resp.Data.Amount24
		}
	} else {
		url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v3/ticker/24hr?symbol=" + rawSym
		body, _, err := a.client.GetJSON(ctx, url)
		if err != nil {
			return 0
		}
		var resp struct {
			QuoteVolume string `json:"quoteVolume"`
		}
		if err := json.Unmarshal(body, &resp); err == nil {
			vol, _ = strconv.ParseFloat(resp.QuoteVolume, 64)
		}
	}
	if vol > 0 {
		a.volMu.Lock()
		a.volCache[cacheKey] = cachedVol{vol: vol, expMs: nowMs + 60_000}
		a.volMu.Unlock()
	}
	return vol
}

func (a *Adapter) contractMultiplier(ctx context.Context, rawSym string) float64 {
	a.contractSizeMu.RLock()
	val, ok := a.contractSize[rawSym]
	a.contractSizeMu.RUnlock()
	if ok && val > 0 {
		return val
	}
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v1/contract/detail?symbol=" + rawSym
	body, _, err := a.client.GetJSON(ctx, url)
	if err == nil {
		var resp struct {
			Data struct {
				ContractSize float64 `json:"contractSize"`
			} `json:"data"`
		}
		if json.Unmarshal(body, &resp) == nil && resp.Data.ContractSize > 0 {
			a.contractSizeMu.Lock()
			a.contractSize[rawSym] = resp.Data.ContractSize
			a.contractSizeMu.Unlock()
			return resp.Data.ContractSize
		}
	}
	return 1.0
}

func parseFloatLevels(raw [][]float64, mult float64) []domain.OrderBookLevel {
	if mult <= 0 {
		mult = 1.0
	}
	levels := make([]domain.OrderBookLevel, 0, len(raw))
	for _, entry := range raw {
		if len(entry) < 2 {
			continue
		}
		levels = append(levels, domain.OrderBookLevel{Price: entry[0], Quantity: entry[1] * mult})
	}
	return levels
}

func parseStringLevels(raw [][]string) ([]domain.OrderBookLevel, error) {
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
