package mexc

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/domain"
)

type mexcSpotSymbolItem struct {
	Symbol               string `json:"symbol"`
	Status               string `json:"status"`
	BaseAsset            string `json:"baseAsset"`
	QuoteAsset           string `json:"quoteAsset"`
	IsSpotTradingAllowed *bool  `json:"isSpotTradingAllowed"`
}

type mexcContractDetailItem struct {
	Symbol       string  `json:"symbol"`
	BaseCoin     string  `json:"baseCoin"`
	QuoteCoin    string  `json:"quoteCoin"`
	SettleCoin   string  `json:"settleCoin"`
	ContractSize float64 `json:"contractSize"`
	State        *int    `json:"state"`
}

func (a *Adapter) FetchMarkets(ctx context.Context) ([]domain.MarketDescriptor, error) {
	nowMs := time.Now().UnixMilli()
	var out []domain.MarketDescriptor

	spotMarkets, spotErr := a.fetchSpotMarkets(ctx, nowMs)
	if spotErr == nil {
		out = append(out, spotMarkets...)
	}

	perpMarkets, perpErr := a.fetchContractMarkets(ctx, nowMs)
	if perpErr == nil {
		out = append(out, perpMarkets...)
	}

	if len(out) == 0 && spotErr != nil {
		return nil, spotErr
	}
	if len(out) == 0 && perpErr != nil {
		return nil, perpErr
	}
	return out, nil
}

func (a *Adapter) fetchSpotMarkets(ctx context.Context, nowMs int64) ([]domain.MarketDescriptor, error) {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v3/exchangeInfo"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	var resp struct {
		Symbols []mexcSpotSymbolItem `json:"symbols"`
	}
	if err := json.Unmarshal(body, &resp); err == nil && len(resp.Symbols) > 0 {
		var vols map[string]float64
		if len(resp.Symbols) > 20 {
			vols = a.prefetchBulkSpotVolumes(ctx, nowMs)
		}
		out := make([]domain.MarketDescriptor, 0, len(resp.Symbols))
		for _, item := range resp.Symbols {
			desc, ok := a.spotItemToDescriptor(item, nowMs)
			if ok && a.cfg.AllowsMarket(desc) {
				desc.Volume24hUSDT = vols[desc.RawSymbol]
				out = append(out, desc)
			}
		}
		return out, nil
	}

	return a.parseFallbackMarkets(body, domain.MarketTypeSpot, nowMs), nil
}

func (a *Adapter) prefetchBulkSpotVolumes(ctx context.Context, nowMs int64) map[string]float64 {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v3/ticker/24hr"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil
	}
	var list []struct {
		Symbol      string `json:"symbol"`
		QuoteVolume string `json:"quoteVolume"`
	}
	if json.Unmarshal(body, &list) != nil || len(list) == 0 {
		return nil
	}
	vols := make(map[string]float64, len(list))
	a.volMu.Lock()
	for _, d := range list {
		if vol, err := strconv.ParseFloat(d.QuoteVolume, 64); err == nil && vol > 0 {
			vols[d.Symbol] = vol
			a.volCache[string(domain.MarketTypeSpot)+":"+d.Symbol] = cachedVol{vol: vol, expMs: nowMs + 300_000}
		}
	}
	a.volMu.Unlock()
	return vols
}

func (a *Adapter) spotItemToDescriptor(item mexcSpotSymbolItem, nowMs int64) (domain.MarketDescriptor, bool) {
	rawSym := strings.ToUpper(strings.TrimSpace(item.Symbol))
	if rawSym == "" || !isMEXCSpotActive(item) {
		return domain.MarketDescriptor{}, false
	}
	quote := strings.ToUpper(strings.TrimSpace(item.QuoteAsset))
	if quote != "" && quote != domain.QuoteAssetUSDT {
		return domain.MarketDescriptor{}, false
	}
	canon, err := a.NormalizeSymbol(rawSym, domain.MarketTypeSpot)
	if err != nil {
		return domain.MarketDescriptor{}, false
	}
	return domain.MarketDescriptor{
		Exchange:           a.Name(),
		RawSymbol:          rawSym,
		CanonSymbol:        canon,
		BaseAsset:          canon.BaseAsset(),
		QuoteAsset:         domain.QuoteAssetUSDT,
		MarketType:         domain.MarketTypeSpot,
		Active:             true,
		ContractMultiplier: 1.0,
		DiscoveredAtMs:     nowMs,
	}, true
}

func isMEXCSpotActive(item mexcSpotSymbolItem) bool {
	if item.IsSpotTradingAllowed != nil && !*item.IsSpotTradingAllowed {
		return false
	}
	st := strings.ToUpper(strings.TrimSpace(item.Status))
	return st == "" || st == "1" || st == "ENABLED" || st == "TRADING"
}

func (a *Adapter) fetchContractMarkets(ctx context.Context, nowMs int64) ([]domain.MarketDescriptor, error) {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v1/contract/detail"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	items := decodeMEXCContractList(body)
	if len(items) > 0 {
		var vols map[string]float64
		if len(items) > 20 {
			vols = a.prefetchBulkContractVolumes(ctx, nowMs)
		}
		out := make([]domain.MarketDescriptor, 0, len(items))
		for _, item := range items {
			desc, ok := a.contractItemToDescriptor(item, nowMs)
			if ok && a.cfg.AllowsMarket(desc) {
				desc.Volume24hUSDT = vols[desc.RawSymbol]
				out = append(out, desc)
			}
		}
		return out, nil
	}

	return a.parseFallbackMarkets(body, domain.MarketTypeLinearPerp, nowMs), nil
}

func (a *Adapter) prefetchBulkContractVolumes(ctx context.Context, nowMs int64) map[string]float64 {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v1/contract/ticker"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil
	}
	var resp struct {
		Data []struct {
			Symbol   string  `json:"symbol"`
			Amount24 float64 `json:"amount24"`
		} `json:"data"`
	}
	if json.Unmarshal(body, &resp) != nil || len(resp.Data) == 0 {
		return nil
	}
	vols := make(map[string]float64, len(resp.Data))
	a.volMu.Lock()
	for _, d := range resp.Data {
		if d.Amount24 > 0 {
			vols[d.Symbol] = d.Amount24
			a.volCache[string(domain.MarketTypeLinearPerp)+":"+d.Symbol] = cachedVol{vol: d.Amount24, expMs: nowMs + 300_000}
		}
	}
	a.volMu.Unlock()
	return vols
}

func decodeMEXCContractList(body []byte) []mexcContractDetailItem {
	var listResp struct {
		Data []mexcContractDetailItem `json:"data"`
	}
	if err := json.Unmarshal(body, &listResp); err == nil && len(listResp.Data) > 0 {
		return listResp.Data
	}
	var singleResp struct {
		Data mexcContractDetailItem `json:"data"`
	}
	if err := json.Unmarshal(body, &singleResp); err == nil && singleResp.Data.Symbol != "" {
		return []mexcContractDetailItem{singleResp.Data}
	}
	return nil
}

func (a *Adapter) contractItemToDescriptor(item mexcContractDetailItem, nowMs int64) (domain.MarketDescriptor, bool) {
	rawSym := strings.ToUpper(strings.TrimSpace(item.Symbol))
	if rawSym == "" {
		return domain.MarketDescriptor{}, false
	}
	if item.State != nil && *item.State != 0 {
		return domain.MarketDescriptor{}, false
	}
	quote := strings.ToUpper(strings.TrimSpace(item.QuoteCoin))
	if quote != "" && quote != domain.QuoteAssetUSDT {
		return domain.MarketDescriptor{}, false
	}
	settle := strings.ToUpper(strings.TrimSpace(item.SettleCoin))
	if settle != "" && settle != domain.QuoteAssetUSDT {
		return domain.MarketDescriptor{}, false
	}
	canon, err := a.NormalizeSymbol(rawSym, domain.MarketTypeLinearPerp)
	if err != nil {
		return domain.MarketDescriptor{}, false
	}

	mult := 1.0
	if item.ContractSize > 0 {
		mult = item.ContractSize
		a.contractSizeMu.Lock()
		a.contractSize[rawSym] = item.ContractSize
		a.contractSizeMu.Unlock()
	}

	return domain.MarketDescriptor{
		Exchange:           a.Name(),
		RawSymbol:          rawSym,
		CanonSymbol:        canon,
		BaseAsset:          canon.BaseAsset(),
		QuoteAsset:         domain.QuoteAssetUSDT,
		MarketType:         domain.MarketTypeLinearPerp,
		Active:             true,
		ContractMultiplier: mult,
		DiscoveredAtMs:     nowMs,
	}, true
}

func (a *Adapter) parseFallbackMarkets(body []byte, mt domain.MarketType, nowMs int64) []domain.MarketDescriptor {
	return adapter.ParseFallbackMarkets(body, a.Name(), mt, nowMs, a.cfg, a.ToRawSymbol)
}
