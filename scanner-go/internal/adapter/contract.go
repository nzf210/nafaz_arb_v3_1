package adapter

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"sync"

	"nafaz_arb/internal/domain"
)

type Capabilities struct {
	HasSpot           bool
	HasPerp           bool
	HasCollateralInfo bool
	UnifiedMargin     bool
}

type ExchangeAdapter interface {
	Name() string
	Capabilities() Capabilities
	FetchMarkets(ctx context.Context) ([]domain.MarketDescriptor, error)
	FetchOrderBook(ctx context.Context, symbol domain.CanonSymbol) (*domain.OrderBookSnapshot, error)
	FetchFundingRate(ctx context.Context, symbol domain.CanonSymbol) (*domain.FundingSnapshot, error)
	FetchCollateralRatio(ctx context.Context) (*domain.CollateralSnapshot, error)
	NormalizeSymbol(raw string, marketType domain.MarketType) (domain.CanonSymbol, error)
	ToRawSymbol(canon domain.CanonSymbol) string
}

type Factory func(cfg domain.ExchangeConfig) (ExchangeAdapter, error)

type Registry struct {
	mu        sync.RWMutex
	factories map[string]Factory
}

func NewRegistry() *Registry {
	return &Registry{factories: make(map[string]Factory)}
}

func (r *Registry) Register(name string, factory Factory) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.factories[name] = factory
}

func (r *Registry) Create(name string, cfg domain.ExchangeConfig) (ExchangeAdapter, error) {
	r.mu.RLock()
	factory, ok := r.factories[name]
	r.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("adapter %q not registered", name)
	}
	return factory(cfg)
}

func (r *Registry) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]string, 0, len(r.factories))
	for k := range r.factories {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func ParseFallbackMarkets(
	body []byte,
	exchange string,
	mt domain.MarketType,
	nowMs int64,
	cfg domain.ExchangeConfig,
	toRaw func(domain.CanonSymbol) string,
) []domain.MarketDescriptor {
	var direct struct {
		Markets     []domain.MarketDescriptor `json:"markets"`
		AssetRatios map[string]float64        `json:"asset_ratios"`
	}
	if json.Unmarshal(body, &direct) != nil {
		return nil
	}
	if out := filterDirectMarkets(direct.Markets, exchange, mt, nowMs, cfg); len(out) > 0 {
		return out
	}
	return buildAssetRatioFallback(direct.AssetRatios, exchange, mt, nowMs, cfg, toRaw)
}

func filterDirectMarkets(
	markets []domain.MarketDescriptor,
	exchange string,
	mt domain.MarketType,
	nowMs int64,
	cfg domain.ExchangeConfig,
) []domain.MarketDescriptor {
	var out []domain.MarketDescriptor
	for _, m := range markets {
		if m.MarketType != mt {
			continue
		}
		if m.Exchange == "" {
			m.Exchange = exchange
		}
		if m.DiscoveredAtMs <= 0 {
			m.DiscoveredAtMs = nowMs
		}
		if cfg.AllowsMarket(m) {
			out = append(out, m)
		}
	}
	return out
}

func buildAssetRatioFallback(
	ratios map[string]float64,
	exchange string,
	mt domain.MarketType,
	nowMs int64,
	cfg domain.ExchangeConfig,
	toRaw func(domain.CanonSymbol) string,
) []domain.MarketDescriptor {
	var out []domain.MarketDescriptor
	for asset := range ratios {
		base := strings.ToUpper(strings.TrimSpace(asset))
		if base == "" || base == domain.QuoteAssetUSDT {
			continue
		}
		canon, err := domain.NormalizeMarketSymbol(base, domain.QuoteAssetUSDT, mt)
		if err != nil {
			continue
		}
		desc := domain.MarketDescriptor{
			Exchange:           exchange,
			RawSymbol:          toRaw(canon),
			CanonSymbol:        canon,
			BaseAsset:          base,
			QuoteAsset:         domain.QuoteAssetUSDT,
			MarketType:         mt,
			Active:             true,
			ContractMultiplier: 1.0,
			DiscoveredAtMs:     nowMs,
		}
		if cfg.AllowsMarket(desc) {
			out = append(out, desc)
		}
	}
	return out
}
