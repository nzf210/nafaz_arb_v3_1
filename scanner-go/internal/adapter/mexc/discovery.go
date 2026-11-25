package mexc

import (
	"context"
	"encoding/json"
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
		out := make([]domain.MarketDescriptor, 0, len(resp.Symbols))
		for _, item := range resp.Symbols {
			desc, ok := a.spotItemToDescriptor(item, nowMs)
			if ok && a.cfg.AllowsMarket(desc) {
				out = append(out, desc)
			}
		}
		return out, nil
	}

	return a.parseFallbackMarkets(body, domain.MarketTypeSpot, nowMs), nil
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
		out := make([]domain.MarketDescriptor, 0, len(items))
		for _, item := range items {
			desc, ok := a.contractItemToDescriptor(item, nowMs)
			if ok && a.cfg.AllowsMarket(desc) {
				out = append(out, desc)
			}
		}
		return out, nil
	}

	return a.parseFallbackMarkets(body, domain.MarketTypeLinearPerp, nowMs), nil
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
