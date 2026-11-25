package okx

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/domain"
)

type okxInstrumentItem struct {
	InstID    string `json:"instId"`
	InstType  string `json:"instType"`
	BaseCcy   string `json:"baseCcy"`
	QuoteCcy  string `json:"quoteCcy"`
	SettleCcy string `json:"settleCcy"`
	CtType    string `json:"ctType"`
	CtVal     string `json:"ctVal"`
	State     string `json:"state"`
}

func (a *Adapter) FetchMarkets(ctx context.Context) ([]domain.MarketDescriptor, error) {
	nowMs := time.Now().UnixMilli()
	var out []domain.MarketDescriptor

	spotMarkets, spotErr := a.fetchInstrumentType(ctx, "SPOT", domain.MarketTypeSpot, nowMs)
	if spotErr == nil {
		out = append(out, spotMarkets...)
	}

	perpMarkets, perpErr := a.fetchInstrumentType(ctx, "SWAP", domain.MarketTypeLinearPerp, nowMs)
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

func (a *Adapter) fetchInstrumentType(
	ctx context.Context,
	instType string,
	mt domain.MarketType,
	nowMs int64,
) ([]domain.MarketDescriptor, error) {
	url := strings.TrimRight(a.cfg.BaseURL, "/") + "/api/v5/public/instruments?instType=" + instType
	body, _, err := a.client.GetJSON(ctx, url)
	if err != nil {
		return nil, err
	}

	var v5 struct {
		Data []okxInstrumentItem `json:"data"`
	}
	if err := json.Unmarshal(body, &v5); err == nil && len(v5.Data) > 0 {
		return a.parseOKXInstruments(v5.Data, mt, nowMs), nil
	}

	return a.parseFallbackMarkets(body, mt, nowMs), nil
}

func (a *Adapter) parseOKXInstruments(
	items []okxInstrumentItem,
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
	item okxInstrumentItem,
	mt domain.MarketType,
	nowMs int64,
) (domain.MarketDescriptor, bool) {
	rawSym := strings.ToUpper(strings.TrimSpace(item.InstID))
	if rawSym == "" {
		return domain.MarketDescriptor{}, false
	}
	state := strings.ToLower(strings.TrimSpace(item.State))
	active := state == "" || state == "live"
	if !active {
		return domain.MarketDescriptor{}, false
	}
	if mt == domain.MarketTypeLinearPerp && !isOKXLinearUSDT(item) {
		return domain.MarketDescriptor{}, false
	}
	if mt == domain.MarketTypeSpot && !isOKXSpotUSDT(item) {
		return domain.MarketDescriptor{}, false
	}

	canon, err := a.NormalizeSymbol(rawSym, mt)
	if err != nil {
		return domain.MarketDescriptor{}, false
	}

	mult := 1.0
	if mt == domain.MarketTypeLinearPerp {
		if parsed, parseErr := strconv.ParseFloat(item.CtVal, 64); parseErr == nil && parsed > 0 {
			mult = parsed
			a.ctValMu.Lock()
			a.ctValByID[rawSym] = parsed
			a.ctValMu.Unlock()
		}
	}

	return domain.MarketDescriptor{
		Exchange:           a.Name(),
		RawSymbol:          rawSym,
		CanonSymbol:        canon,
		BaseAsset:          canon.BaseAsset(),
		QuoteAsset:         domain.QuoteAssetUSDT,
		MarketType:         mt,
		Active:             true,
		ContractMultiplier: mult,
		DiscoveredAtMs:     nowMs,
	}, true
}

func isOKXLinearUSDT(item okxInstrumentItem) bool {
	ctType := strings.ToLower(strings.TrimSpace(item.CtType))
	if ctType != "" && ctType != "linear" {
		return false
	}
	settle := strings.ToUpper(strings.TrimSpace(item.SettleCcy))
	if settle != "" && settle != domain.QuoteAssetUSDT {
		return false
	}
	quote := strings.ToUpper(strings.TrimSpace(item.QuoteCcy))
	if quote != "" && quote != domain.QuoteAssetUSDT {
		return false
	}
	return true
}

func isOKXSpotUSDT(item okxInstrumentItem) bool {
	quote := strings.ToUpper(strings.TrimSpace(item.QuoteCcy))
	return quote == "" || quote == domain.QuoteAssetUSDT
}

func (a *Adapter) parseFallbackMarkets(body []byte, mt domain.MarketType, nowMs int64) []domain.MarketDescriptor {
	return adapter.ParseFallbackMarkets(body, a.Name(), mt, nowMs, a.cfg, a.ToRawSymbol)
}
