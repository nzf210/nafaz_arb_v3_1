package engine

import (
	"sort"
	"sync"

	"nafaz_arb/internal/domain"
)

type ExchangePair struct {
	LongExchange  string
	ShortExchange string
}

type MatchResult struct {
	PollingTargets    map[string][]domain.MarketDescriptor
	MatchedBaseAssets []string
	PerpPerpRoutes    map[string][]ExchangePair
	SameCEXSpotPerp   map[string][]string
	CrossCEXSpotPerp  map[string][]ExchangePair
}

type MarketRegistry struct {
	mu              sync.RWMutex
	markets         map[string]map[domain.CanonSymbol]domain.MarketDescriptor
	lastDiscoveryMs map[string]int64
}

func NewMarketRegistry() *MarketRegistry {
	return &MarketRegistry{
		markets:         make(map[string]map[domain.CanonSymbol]domain.MarketDescriptor),
		lastDiscoveryMs: make(map[string]int64),
	}
}

func (r *MarketRegistry) IsEmpty() bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.markets) == 0
}

func (r *MarketRegistry) LastDiscoveryMs(exchange string) int64 {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.lastDiscoveryMs[exchange]
}

func (r *MarketRegistry) IsMarketActive(exchange string, sym domain.CanonSymbol) bool {
	r.mu.RLock()
	defer r.mu.RUnlock()
	bySym, ok := r.markets[exchange]
	if !ok {
		return false
	}
	desc, exists := bySym[sym]
	return exists && desc.Active
}

func (r *MarketRegistry) GetMarket(exchange string, sym domain.CanonSymbol) (domain.MarketDescriptor, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	bySym, ok := r.markets[exchange]
	if !ok {
		return domain.MarketDescriptor{}, false
	}
	desc, exists := bySym[sym]
	return desc, exists
}

func (r *MarketRegistry) ExchangeMarkets(exchange string) []domain.MarketDescriptor {
	r.mu.RLock()
	defer r.mu.RUnlock()
	bySym := r.markets[exchange]
	out := make([]domain.MarketDescriptor, 0, len(bySym))
	for _, desc := range bySym {
		out = append(out, desc)
	}
	sortDescriptors(out)
	return out
}

func (r *MarketRegistry) UpdateExchangeMarkets(
	exchange string,
	cfg domain.ExchangeConfig,
	discovered []domain.MarketDescriptor,
	nowMs int64,
) (added []domain.MarketDescriptor, removed []domain.MarketDescriptor) {
	next := make(map[domain.CanonSymbol]domain.MarketDescriptor, len(discovered))
	for _, desc := range discovered {
		if desc.Exchange == "" {
			desc.Exchange = exchange
		}
		if desc.DiscoveredAtMs <= 0 {
			desc.DiscoveredAtMs = nowMs
		}
		if !cfg.AllowsMarket(desc) {
			continue
		}
		next[desc.CanonSymbol] = desc
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	prev := r.markets[exchange]
	for sym, desc := range next {
		if _, existed := prev[sym]; !existed {
			added = append(added, desc)
		}
	}
	for sym, oldDesc := range prev {
		if _, stillThere := next[sym]; !stillThere {
			removed = append(removed, oldDesc)
		}
	}

	r.markets[exchange] = next
	r.lastDiscoveryMs[exchange] = nowMs

	sortDescriptors(added)
	sortDescriptors(removed)
	return added, removed
}

func (r *MarketRegistry) MatchUniverse(
	exchanges []ExchangeMeta,
	policies domain.RoutePolicyConfig,
) MatchResult {
	r.mu.RLock()
	snapshot := r.cloneActiveMarketsLocked()
	r.mu.RUnlock()

	healthyEx := filterHealthyExchanges(exchanges)
	baseSet := collectDiscoveredBaseAssets(snapshot, healthyEx)

	res := MatchResult{
		PollingTargets:   make(map[string][]domain.MarketDescriptor, len(healthyEx)),
		PerpPerpRoutes:   make(map[string][]ExchangePair),
		SameCEXSpotPerp:  make(map[string][]string),
		CrossCEXSpotPerp: make(map[string][]ExchangePair),
	}
	targetSets := make(map[string]map[domain.CanonSymbol]domain.MarketDescriptor, len(healthyEx))
	for _, ex := range healthyEx {
		targetSets[ex.Name] = make(map[domain.CanonSymbol]domain.MarketDescriptor)
	}

	matchedBases := make(map[string]struct{})
	for _, base := range baseSet {
		if matchBaseAsset(base, healthyEx, policies, snapshot, &res, targetSets) {
			matchedBases[base] = struct{}{}
		}
	}

	for base := range matchedBases {
		res.MatchedBaseAssets = append(res.MatchedBaseAssets, base)
	}
	sort.Strings(res.MatchedBaseAssets)

	for exName, bySym := range targetSets {
		list := make([]domain.MarketDescriptor, 0, len(bySym))
		for _, desc := range bySym {
			list = append(list, desc)
		}
		sortDescriptors(list)
		res.PollingTargets[exName] = list
	}
	return res
}

func (r *MarketRegistry) cloneActiveMarketsLocked() map[string]map[domain.CanonSymbol]domain.MarketDescriptor {
	out := make(map[string]map[domain.CanonSymbol]domain.MarketDescriptor, len(r.markets))
	for ex, bySym := range r.markets {
		cp := make(map[domain.CanonSymbol]domain.MarketDescriptor, len(bySym))
		for sym, desc := range bySym {
			if desc.Active {
				cp[sym] = desc
			}
		}
		out[ex] = cp
	}
	return out
}

func filterHealthyExchanges(exchanges []ExchangeMeta) []ExchangeMeta {
	out := make([]ExchangeMeta, 0, len(exchanges))
	for _, ex := range exchanges {
		if ex.Healthy {
			out = append(out, ex)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

func collectDiscoveredBaseAssets(
	snapshot map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
	healthyEx []ExchangeMeta,
) []string {
	set := make(map[string]struct{})
	for _, ex := range healthyEx {
		for _, desc := range snapshot[ex.Name] {
			if desc.BaseAsset != "" {
				set[desc.BaseAsset] = struct{}{}
			}
		}
	}
	out := make([]string, 0, len(set))
	for b := range set {
		out = append(out, b)
	}
	sort.Strings(out)
	return out
}

func matchBaseAsset(
	base string,
	healthyEx []ExchangeMeta,
	policies domain.RoutePolicyConfig,
	snapshot map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
	res *MatchResult,
	targetSets map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
) bool {
	spotSym, errSpot := domain.NewCanonSymbol(base, domain.MarketTypeSpot)
	perpSym, errPerp := domain.NewCanonSymbol(base, domain.MarketTypeLinearPerp)
	if errSpot != nil || errPerp != nil {
		return false
	}

	matched := false
	if policies.EnableSpotPerpSameCEX && matchSameCEXSpotPerp(base, spotSym, perpSym, healthyEx, snapshot, res, targetSets) {
		matched = true
	}
	if policies.EnablePerpPerpCrossCEX && matchCrossCEXPerpPerp(base, perpSym, healthyEx, snapshot, res, targetSets) {
		matched = true
	}
	if policies.EnableSpotPerpCrossCEX && matchCrossCEXSpotPerp(base, spotSym, perpSym, healthyEx, snapshot, res, targetSets) {
		matched = true
	}
	return matched
}

func matchSameCEXSpotPerp(
	base string,
	spotSym, perpSym domain.CanonSymbol,
	healthyEx []ExchangeMeta,
	snapshot map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
	res *MatchResult,
	targetSets map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
) bool {
	var venues []string
	for _, ex := range healthyEx {
		if !ex.Capabilities.HasSpot || !ex.Capabilities.HasPerp || !ex.Capabilities.UnifiedMargin {
			continue
		}
		spotDesc, hasSpot := snapshot[ex.Name][spotSym]
		perpDesc, hasPerp := snapshot[ex.Name][perpSym]
		if !hasSpot || !hasPerp {
			continue
		}
		venues = append(venues, ex.Name)
		targetSets[ex.Name][spotSym] = spotDesc
		targetSets[ex.Name][perpSym] = perpDesc
	}
	if len(venues) == 0 {
		return false
	}
	res.SameCEXSpotPerp[base] = venues
	return true
}

func matchCrossCEXPerpPerp(
	base string,
	perpSym domain.CanonSymbol,
	healthyEx []ExchangeMeta,
	snapshot map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
	res *MatchResult,
	targetSets map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
) bool {
	var perpVenues []ExchangeMeta
	for _, ex := range healthyEx {
		if !ex.Capabilities.HasPerp {
			continue
		}
		if _, ok := snapshot[ex.Name][perpSym]; ok {
			perpVenues = append(perpVenues, ex)
		}
	}
	if len(perpVenues) < 2 {
		return false
	}
	var pairs []ExchangePair
	for _, exA := range perpVenues {
		targetSets[exA.Name][perpSym] = snapshot[exA.Name][perpSym]
		for _, exB := range perpVenues {
			if exA.Name != exB.Name {
				pairs = append(pairs, ExchangePair{LongExchange: exA.Name, ShortExchange: exB.Name})
			}
		}
	}
	res.PerpPerpRoutes[base] = pairs
	return true
}

func matchCrossCEXSpotPerp(
	base string,
	spotSym, perpSym domain.CanonSymbol,
	healthyEx []ExchangeMeta,
	snapshot map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
	res *MatchResult,
	targetSets map[string]map[domain.CanonSymbol]domain.MarketDescriptor,
) bool {
	var pairs []ExchangePair
	for _, exA := range healthyEx {
		spotDesc, hasSpot := snapshot[exA.Name][spotSym]
		if !exA.Capabilities.HasSpot || !hasSpot {
			continue
		}
		for _, exB := range healthyEx {
			if exA.Name == exB.Name || !exB.Capabilities.HasPerp {
				continue
			}
			perpDesc, hasPerp := snapshot[exB.Name][perpSym]
			if !hasPerp {
				continue
			}
			pairs = append(pairs, ExchangePair{LongExchange: exA.Name, ShortExchange: exB.Name})
			targetSets[exA.Name][spotSym] = spotDesc
			targetSets[exB.Name][perpSym] = perpDesc
		}
	}
	if len(pairs) == 0 {
		return false
	}
	res.CrossCEXSpotPerp[base] = pairs
	return true
}

func sortDescriptors(list []domain.MarketDescriptor) {
	sort.Slice(list, func(i, j int) bool {
		if list[i].Exchange != list[j].Exchange {
			return list[i].Exchange < list[j].Exchange
		}
		return list[i].CanonSymbol < list[j].CanonSymbol
	})
}

func BuildAllowlistFallbackDescriptors(
	name string,
	hasSpot, hasPerp bool,
	toRaw func(domain.CanonSymbol) string,
	exCfg domain.ExchangeConfig,
	nowMs int64,
) []domain.MarketDescriptor {
	allow := exCfg.EffectiveDiscoveryConfig().Allowlist
	if len(allow) == 0 {
		return nil
	}
	var out []domain.MarketDescriptor
	for _, base := range allow {
		if hasSpot {
			if sym, err := domain.NormalizeMarketSymbol(base, domain.QuoteAssetUSDT, domain.MarketTypeSpot); err == nil {
				out = append(out, domain.MarketDescriptor{
					Exchange:       name,
					RawSymbol:      toRaw(sym),
					CanonSymbol:    sym,
					BaseAsset:      sym.BaseAsset(),
					QuoteAsset:     domain.QuoteAssetUSDT,
					MarketType:     domain.MarketTypeSpot,
					Active:         true,
					DiscoveredAtMs: nowMs,
				})
			}
		}
		if hasPerp {
			if sym, err := domain.NormalizeMarketSymbol(base, domain.QuoteAssetUSDT, domain.MarketTypeLinearPerp); err == nil {
				out = append(out, domain.MarketDescriptor{
					Exchange:       name,
					RawSymbol:      toRaw(sym),
					CanonSymbol:    sym,
					BaseAsset:      sym.BaseAsset(),
					QuoteAsset:     domain.QuoteAssetUSDT,
					MarketType:     domain.MarketTypeLinearPerp,
					Active:         true,
					DiscoveredAtMs: nowMs,
				})
			}
		}
	}
	return out
}

func SelectSynchronizedPollBatch(
	match MatchResult,
	minVolumeUSDT float64,
	maxBases, cursor int,
) (map[string][]domain.MarketDescriptor, int) {
	if maxBases <= 0 || len(match.MatchedBaseAssets) <= maxBases {
		return match.PollingTargets, 0
	}
	eligible := filterLiquidBases(match, minVolumeUSDT)
	if len(eligible) <= maxBases {
		return filterTargetsByBases(match.PollingTargets, eligible), 0
	}
	start := cursor % len(eligible)
	batch := make([]string, 0, maxBases)
	for i := 0; i < maxBases; i++ {
		batch = append(batch, eligible[(start+i)%len(eligible)])
	}
	nextCursor := (start + maxBases) % len(eligible)
	return filterTargetsByBases(match.PollingTargets, batch), nextCursor
}

func filterLiquidBases(match MatchResult, minVolumeUSDT float64) []string {
	if minVolumeUSDT <= 0 {
		return match.MatchedBaseAssets
	}
	volCount := make(map[string]int)
	anyVolReported := false
	for _, list := range match.PollingTargets {
		for _, d := range list {
			if d.Volume24hUSDT > 0 {
				anyVolReported = true
			}
			if d.Volume24hUSDT >= minVolumeUSDT {
				volCount[d.BaseAsset]++
			}
		}
	}
	if !anyVolReported {
		return match.MatchedBaseAssets
	}
	var out []string
	for _, b := range match.MatchedBaseAssets {
		if volCount[b] >= 2 {
			out = append(out, b)
		}
	}
	if len(out) == 0 {
		return match.MatchedBaseAssets
	}
	return out
}

func filterTargetsByBases(
	targets map[string][]domain.MarketDescriptor,
	bases []string,
) map[string][]domain.MarketDescriptor {
	keep := make(map[string]struct{}, len(bases))
	for _, b := range bases {
		keep[b] = struct{}{}
	}
	out := make(map[string][]domain.MarketDescriptor, len(targets))
	for ex, list := range targets {
		for _, d := range list {
			if _, ok := keep[d.BaseAsset]; ok {
				out[ex] = append(out[ex], d)
			}
		}
	}
	return out
}
