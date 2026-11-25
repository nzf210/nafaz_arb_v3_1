package bybit

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/domain"
)

type bybitInstrumentItem struct {
	Symbol       string `json:"symbol"`
	BaseCoin     string `json:"baseCoin"`
	QuoteCoin    string `json:"quoteCoin"`
	SettleCoin   string `json:"settleCoin"`
	ContractType string `json:"contractType"`
	Status       string `json:"status"`
}

func (a *Adapter) FetchMarkets(ctx context.Context) ([]domain.MarketDescriptor, error) {
	nowMs := time.Now().UnixMilli()
	var out []domain.MarketDescriptor

	spotMarkets, spotErr := a.fetchCategoryMarkets(ctx, "spot", domain.MarketTypeSpot, nowMs)
	if spotErr == nil {
		out = append(out, spotMarkets...)
	}

	perpMarkets, perpErr := a.fetchCategoryMarkets(ctx, "linear", domain.MarketTypeLinearPerp, nowMs)
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

func (a *Adapter) fetchCategoryMarkets(
	ctx context.Context,
	category string,
	mt domain.MarketType,
	nowMs int64,
) ([]domain.MarketDescriptor, error) {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/v5/market/instruments-info?category=" + category + "&limit=1000"
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	var v5 struct {
		RetCode int `json:"retCode"`
		Result  struct {
			List []bybitInstrumentItem `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(body, &v5); err == nil && len(v5.Result.List) > 0 {
		return a.parseBybitInstruments(v5.Result.List, mt, nowMs), nil
	}

	return a.parseFallbackMarkets(body, mt, nowMs), nil
}

func (a *Adapter) parseBybitInstruments(
	items []bybitInstrumentItem,
	mt domain.MarketType,
	nowMs int64,
) []domain.MarketDescriptor {
	out := make([]domain.MarketDescriptor, 0, len(items))
	for _, item := range items {
		desc, ok := a.toMarketDescriptor(item, mt, nowMs)
		if ok && a.cfg.AllowsMarket(desc) {
			out = append(out, desc)
		}
	}
	return out
}

func (a *Adapter) toMarketDescriptor(
	item bybitInstrumentItem,
	mt domain.MarketType,
	nowMs int64,
) (domain.MarketDescriptor, bool) {
	rawSym := strings.ToUpper(strings.TrimSpace(item.Symbol))
	if rawSym == "" || !isBybitActiveUSDT(item, mt) {
		return domain.MarketDescriptor{}, false
	}

	canon, err := a.NormalizeSymbol(rawSym, mt)
	if err != nil {
		return domain.MarketDescriptor{}, false
	}

	return domain.MarketDescriptor{
		Exchange:           a.Name(),
		RawSymbol:          rawSym,
		CanonSymbol:        canon,
		BaseAsset:          canon.BaseAsset(),
		QuoteAsset:         domain.QuoteAssetUSDT,
		MarketType:         mt,
		Active:             true,
		ContractMultiplier: 1.0,
		DiscoveredAtMs:     nowMs,
	}, true
}

func isBybitActiveUSDT(item bybitInstrumentItem, mt domain.MarketType) bool {
	status := strings.ToLower(strings.TrimSpace(item.Status))
	if status != "" && status != "trading" {
		return false
	}
	quote := strings.ToUpper(strings.TrimSpace(item.QuoteCoin))
	if quote != "" && quote != domain.QuoteAssetUSDT {
		return false
	}
	if mt == domain.MarketTypeLinearPerp {
		settle := strings.ToUpper(strings.TrimSpace(item.SettleCoin))
		if settle != "" && settle != domain.QuoteAssetUSDT {
			return false
		}
		ct := strings.ToLower(strings.TrimSpace(item.ContractType))
		if ct != "" && ct != "linearperpetual" {
			return false
		}
	}
	return true
}

func (a *Adapter) parseFallbackMarkets(body []byte, mt domain.MarketType, nowMs int64) []domain.MarketDescriptor {
	return adapter.ParseFallbackMarkets(body, a.Name(), mt, nowMs, a.cfg, a.ToRawSymbol)
}
