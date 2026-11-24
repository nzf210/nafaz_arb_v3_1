package okx

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/adapter/resilience"
	"nafaz_arb/internal/domain"
)

type Adapter struct {
	cfg       domain.ExchangeConfig
	client    *resilience.Client
	ctValMu   sync.RWMutex
	ctValByID map[string]float64
	volMu     sync.RWMutex
	volCache  map[string]cachedVol
}

type cachedVol struct {
	vol   float64
	expMs int64
}

func NewAdapter(cfg domain.ExchangeConfig) (adapter.ExchangeAdapter, error) {
	if strings.TrimSpace(cfg.BaseURL) == "" {
		return nil, errors.New("okx base_url is required")
	}
	return &Adapter{
		cfg:       cfg,
		client:    resilience.NewClientFromExchangeConfig(cfg),
		ctValByID: make(map[string]float64),
		volCache:  make(map[string]cachedVol),
	}, nil
}

func (a *Adapter) Name() string {
	return "okx"
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

const (
	okxSwapSuffix = "-USDT-SWAP"
	okxSpotSuffix = "-USDT"
)

func (a *Adapter) NormalizeSymbol(raw string, marketType domain.MarketType) (domain.CanonSymbol, error) {
	s := strings.ToUpper(strings.TrimSpace(raw))
	switch marketType {
	case domain.MarketTypeLinearPerp:
		if strings.HasSuffix(s, okxSwapSuffix) {
			base := strings.TrimSuffix(s, okxSwapSuffix)
			return domain.NewCanonSymbol(base, domain.MarketTypeLinearPerp)
		}
		if strings.HasSuffix(s, okxSpotSuffix) {
			base := strings.TrimSuffix(s, okxSpotSuffix)
			return domain.NewCanonSymbol(base, domain.MarketTypeLinearPerp)
		}
		return "", fmt.Errorf("okx: unsupported linear_perp raw symbol %q", raw)
	case domain.MarketTypeSpot:
		if strings.HasSuffix(s, okxSpotSuffix) {
			base := strings.TrimSuffix(s, okxSpotSuffix)
			return domain.NewCanonSymbol(base, domain.MarketTypeSpot)
		}
		return "", fmt.Errorf("okx: unsupported spot raw symbol %q", raw)
	default:
		return "", fmt.Errorf("okx: unsupported market type %q", marketType)
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
		return base + okxSwapSuffix
	}
	return base + okxSpotSuffix
}

func (a *Adapter) FetchOrderBook(ctx context.Context, symbol domain.CanonSymbol) (*domain.OrderBookSnapshot, error) {
	mt, err := symbol.MarketType()
	if err != nil {
		return nil, err
	}
	rawSym := a.ToRawSymbol(symbol)
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v5/market/books?instId=" + rawSym + "&sz=20"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	// Support OKX v5 response or direct OrderBookSnapshot JSON payload
	var v5 struct {
		Code string `json:"code"`
		Data []struct {
			Asks      [][]string `json:"asks"`
			Bids      [][]string `json:"bids"`
			Ts        string     `json:"ts"`
			VolCcy24h string     `json:"volCcy24h"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &v5); err == nil && len(v5.Data) > 0 {
		d := v5.Data[0]
		mult := 1.0
		if mt == domain.MarketTypeLinearPerp {
			mult = a.contractMultiplier(ctx, rawSym)
		}
		bids, err := parseLevels(d.Bids, mult)
		if err != nil {
			return nil, err
		}
		asks, err := parseLevels(d.Asks, mult)
		if err != nil {
			return nil, err
		}
		ts, _ := strconv.ParseInt(d.Ts, 10, 64)
		if ts <= 0 {
			ts = time.Now().UnixMilli()
		}
		vol, _ := strconv.ParseFloat(d.VolCcy24h, 64)
		if vol <= 0 {
			vol = a.fetchTickerVolume24hUSDT(ctx, rawSym, mt)
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
		return nil, fmt.Errorf("okx: failed to decode orderbook: %w", err)
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
		return nil, fmt.Errorf("okx: funding rate only valid for linear_perp, got %s", symbol)
	}
	rawSym := a.ToRawSymbol(symbol)
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v5/public/funding-rate?instId=" + rawSym
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
		Data []struct {
			FundingRate     string `json:"fundingRate"`
			FundingTime     string `json:"fundingTime"`
			NextFundingTime string `json:"nextFundingTime"`
			IntervalHours   int    `json:"intervalHours"`
			Ts              string `json:"ts"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &v5); err != nil || len(v5.Data) == 0 {
		return nil, false, nil
	}
	d := v5.Data[0]
	rate, err := strconv.ParseFloat(d.FundingRate, 64)
	if err != nil {
		return nil, true, fmt.Errorf("okx: invalid fundingRate %q: %w", d.FundingRate, err)
	}
	fTime, _ := strconv.ParseInt(d.FundingTime, 10, 64)
	nTime, _ := strconv.ParseInt(d.NextFundingTime, 10, 64)
	interval := d.IntervalHours
	if interval == 0 && nTime > fTime && fTime > 0 {
		interval = int(math.Round(float64(nTime-fTime) / float64(3600*1000)))
	}
	nextSettle := nTime
	if nextSettle <= 0 {
		nextSettle = fTime
	}
	ts, _ := strconv.ParseInt(d.Ts, 10, 64)
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
		return nil, true, fmt.Errorf("okx: %w", err)
	}
	return snap, true, nil
}

func (a *Adapter) parseDirectFunding(body []byte, symbol domain.CanonSymbol, rawSym string) (*domain.FundingSnapshot, error) {
	var direct domain.FundingSnapshot
	if err := json.Unmarshal(body, &direct); err != nil {
		return nil, fmt.Errorf("okx: failed to decode funding rate: %w", err)
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
		return nil, fmt.Errorf("okx: %w", err)
	}
	return &direct, nil
}

func (a *Adapter) FetchCollateralRatio(ctx context.Context) (*domain.CollateralSnapshot, error) {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v5/public/discount-rate-interest-free-quota"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	var v5 struct {
		Data []struct {
			Ccy          string `json:"ccy"`
			DiscountRate string `json:"discountRate"`
			DiscountInfo []struct {
				DiscountRate string `json:"discountRate"`
			} `json:"discountInfo"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &v5); err == nil && len(v5.Data) > 0 {
		ratios := make(map[string]float64, len(v5.Data))
		for _, item := range v5.Data {
			ccy := strings.ToUpper(strings.TrimSpace(item.Ccy))
			if ccy == "" {
				continue
			}
			rateStr := item.DiscountRate
			if rateStr == "" && len(item.DiscountInfo) > 0 {
				rateStr = item.DiscountInfo[0].DiscountRate
			}
			if r, err := strconv.ParseFloat(rateStr, 64); err == nil {
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
	if err := json.Unmarshal(body, &direct); err != nil {
		return nil, fmt.Errorf("okx: failed to decode collateral ratio: %w", err)
	}
	if direct.Exchange == "" {
		direct.Exchange = a.Name()
	}
	if direct.TimestampMs <= 0 {
		direct.TimestampMs = time.Now().UnixMilli()
	}
	return &direct, nil
}

func (a *Adapter) fetchTickerVolume24hUSDT(ctx context.Context, rawSym string, mt domain.MarketType) float64 {
	nowMs := time.Now().UnixMilli()
	a.volMu.RLock()
	if entry, ok := a.volCache[rawSym]; ok && nowMs < entry.expMs {
		a.volMu.RUnlock()
		return entry.vol
	}
	a.volMu.RUnlock()

	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v5/market/ticker?instId=" + rawSym
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return 0
	}
	var resp struct {
		Data []struct {
			Last      string `json:"last"`
			Vol24h    string `json:"vol24h"`
			VolCcy24h string `json:"volCcy24h"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &resp); err == nil && len(resp.Data) > 0 {
		d := resp.Data[0]
		volCcy, _ := strconv.ParseFloat(d.VolCcy24h, 64)
		vol := volCcy
		if mt != domain.MarketTypeSpot {
			if last, _ := strconv.ParseFloat(d.Last, 64); volCcy > 0 && last > 0 {
				vol = volCcy * last
			}
		}
		if vol > 0 {
			a.volMu.Lock()
			a.volCache[rawSym] = cachedVol{vol: vol, expMs: nowMs + 60_000}
			a.volMu.Unlock()
		}
		return vol
	}
	return 0
}

func (a *Adapter) contractMultiplier(ctx context.Context, rawSym string) float64 {
	a.ctValMu.RLock()
	val, ok := a.ctValByID[rawSym]
	a.ctValMu.RUnlock()
	if ok && val > 0 {
		return val
	}
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v5/public/instruments?instType=SWAP&instId=" + rawSym
	body, _, err := a.client.GetJSON(ctx, url)
	if err == nil {
		var resp struct {
			Data []struct {
				CtVal string `json:"ctVal"`
			} `json:"data"`
		}
		if json.Unmarshal(body, &resp) == nil && len(resp.Data) > 0 {
			if parsed, parseErr := strconv.ParseFloat(resp.Data[0].CtVal, 64); parseErr == nil && parsed > 0 {
				a.ctValMu.Lock()
				a.ctValByID[rawSym] = parsed
				a.ctValMu.Unlock()
				return parsed
			}
		}
	}
	return 1.0
}

func parseLevels(raw [][]string, mult float64) ([]domain.OrderBookLevel, error) {
	if mult <= 0 {
		mult = 1.0
	}
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
		levels = append(levels, domain.OrderBookLevel{Price: p, Quantity: q * mult})
	}
	return levels, nil
}
