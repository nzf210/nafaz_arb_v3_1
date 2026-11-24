package adapter

import (
	"context"
	"fmt"
	"sort"
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
