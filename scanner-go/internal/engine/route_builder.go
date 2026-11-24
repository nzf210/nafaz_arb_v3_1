package engine

import (
	"fmt"
	"math"
	"sort"

	"nafaz_arb/internal/domain"
)

type storeSnapshot struct {
	healthyList []ExchangeMeta
	baseAssets  []string
	books       map[string]map[domain.CanonSymbol]*domain.OrderBookSnapshot
	fundings    map[string]map[domain.CanonSymbol]*domain.FundingSnapshot
	collaterals map[string]*domain.CollateralSnapshot
}

func (e *MatrixEvaluator) snapshotStore() storeSnapshot {
	e.store.mu.RLock()
	defer e.store.mu.RUnlock()

	healthyList := make([]ExchangeMeta, 0, len(e.store.exchanges))
	for _, meta := range e.store.exchanges {
		if meta.Healthy {
			healthyList = append(healthyList, meta)
		}
	}
	sort.Slice(healthyList, func(i, j int) bool {
		return healthyList[i].Name < healthyList[j].Name
	})

	baseAssets := collectBaseAssets(e.store.books)
	booksCopy := cloneBooks(e.store.books)
	fundingsCopy := cloneFundings(e.store.fundings)
	collateralsCopy := make(map[string]*domain.CollateralSnapshot, len(e.store.collaterals))
	for ex, col := range e.store.collaterals {
		collateralsCopy[ex] = col
	}
	return storeSnapshot{
		healthyList: healthyList,
		baseAssets:  baseAssets,
		books:       booksCopy,
		fundings:    fundingsCopy,
		collaterals: collateralsCopy,
	}
}

func collectBaseAssets(books map[string]map[domain.CanonSymbol]*domain.OrderBookSnapshot) []string {
	baseAssetsMap := make(map[string]struct{})
	for _, m := range books {
		for sym := range m {
			if b := sym.BaseAsset(); b != "" {
				baseAssetsMap[b] = struct{}{}
			}
		}
	}
	baseAssets := make([]string, 0, len(baseAssetsMap))
	for b := range baseAssetsMap {
		baseAssets = append(baseAssets, b)
	}
	sort.Strings(baseAssets)
	return baseAssets
}

func cloneBooks(src map[string]map[domain.CanonSymbol]*domain.OrderBookSnapshot) map[string]map[domain.CanonSymbol]*domain.OrderBookSnapshot {
	dst := make(map[string]map[domain.CanonSymbol]*domain.OrderBookSnapshot, len(src))
	for ex, bm := range src {
		dst[ex] = make(map[domain.CanonSymbol]*domain.OrderBookSnapshot, len(bm))
		for sym, snap := range bm {
			dst[ex][sym] = snap
		}
	}
	return dst
}

func cloneFundings(src map[string]map[domain.CanonSymbol]*domain.FundingSnapshot) map[string]map[domain.CanonSymbol]*domain.FundingSnapshot {
	dst := make(map[string]map[domain.CanonSymbol]*domain.FundingSnapshot, len(src))
	for ex, fm := range src {
		dst[ex] = make(map[domain.CanonSymbol]*domain.FundingSnapshot, len(fm))
		for sym, snap := range fm {
			dst[ex][sym] = snap
		}
	}
	return dst
}

func (e *MatrixEvaluator) buildCandidatesForAsset(base string, snap storeSnapshot, nowMs int64) []routeCandidate {
	spotSym, _ := domain.NewCanonSymbol(base, domain.MarketTypeSpot)
	perpSym, _ := domain.NewCanonSymbol(base, domain.MarketTypeLinearPerp)
	var out []routeCandidate
	if e.cfg.RoutePolicies.EnableSpotPerpSameCEX {
		out = append(out, e.buildSameCEXSpotPerp(base, spotSym, perpSym, snap, nowMs)...)
	}
	if e.cfg.RoutePolicies.EnablePerpPerpCrossCEX {
		out = append(out, e.buildCrossCEXPerpPerp(base, perpSym, snap, nowMs)...)
	}
	if e.cfg.RoutePolicies.EnableSpotPerpCrossCEX {
		out = append(out, e.buildCrossCEXSpotPerp(base, spotSym, perpSym, snap, nowMs)...)
	}
	return out
}

func (e *MatrixEvaluator) buildSameCEXSpotPerp(
	base string,
	spotSym, perpSym domain.CanonSymbol,
	snap storeSnapshot,
	nowMs int64,
) []routeCandidate {
	var out []routeCandidate
	for _, cex := range snap.healthyList {
		if !cex.Capabilities.HasSpot || !cex.Capabilities.HasPerp || !cex.Capabilities.UnifiedMargin {
			continue
		}
		bookSpot := snap.books[cex.Name][spotSym]
		bookPerp := snap.books[cex.Name][perpSym]
		if bookSpot == nil || bookPerp == nil {
			continue
		}
		if !e.validateSameCEXInputs(base, cex.Name, bookSpot, bookPerp, snap.fundings[cex.Name][perpSym], snap.collaterals[cex.Name], nowMs) {
			continue
		}
		out = append(out, routeCandidate{
			routeType: domain.RouteSpotPerpSameCEX,
			baseAsset: base,
			cexA:      cex,
			cexB:      cex,
			actionA:   domain.ActionBuySpot,
			actionB:   domain.ActionSellShort,
			marketA:   domain.MarketTypeSpot,
			marketB:   domain.MarketTypeLinearPerp,
			bookA:     bookSpot,
			bookB:     bookPerp,
			fundingA:  nil,
			fundingB:  snap.fundings[cex.Name][perpSym],
			feeA:      cex.SpotTakerFee,
			feeB:      cex.PerpTakerFee,
		})
	}
	return out
}

func (e *MatrixEvaluator) validateSameCEXInputs(
	base, cexName string,
	bookSpot, bookPerp *domain.OrderBookSnapshot,
	fundingPerp *domain.FundingSnapshot,
	colSnap *domain.CollateralSnapshot,
	nowMs int64,
) bool {
	_ = cexName
	if !e.isBookFresh(bookSpot, nowMs) || !e.isBookFresh(bookPerp, nowMs) {
		e.incRejection("stale_book")
		return false
	}
	if fundingPerp == nil || !e.isFundingFresh(fundingPerp, nowMs) {
		e.incRejection("stale_funding")
		return false
	}
	if colSnap == nil || !e.isCollateralFresh(colSnap, nowMs) {
		e.incRejection("stale_collateral")
		return false
	}
	ratio, ok := colSnap.AssetRatios[base]
	if !ok || ratio < e.cfg.RoutePolicies.MinCollateralRatio {
		e.incRejection("collateral_ratio_low")
		return false
	}
	return true
}

func (e *MatrixEvaluator) buildCrossCEXPerpPerp(
	base string,
	perpSym domain.CanonSymbol,
	snap storeSnapshot,
	nowMs int64,
) []routeCandidate {
	var out []routeCandidate
	for _, cexA := range snap.healthyList {
		if !cexA.Capabilities.HasPerp || snap.books[cexA.Name][perpSym] == nil {
			continue
		}
		for _, cexB := range snap.healthyList {
			if cand, ok := e.tryPerpPerpPair(base, cexA, cexB, snap, perpSym, nowMs); ok {
				out = append(out, cand)
			}
		}
	}
	return out
}

func (e *MatrixEvaluator) tryPerpPerpPair(
	base string,
	cexA, cexB ExchangeMeta,
	snap storeSnapshot,
	perpSym domain.CanonSymbol,
	nowMs int64,
) (routeCandidate, bool) {
	if cexA.Name == cexB.Name || !cexB.Capabilities.HasPerp {
		return routeCandidate{}, false
	}
	bookA := snap.books[cexA.Name][perpSym]
	bookB := snap.books[cexB.Name][perpSym]
	if bookB == nil {
		return routeCandidate{}, false
	}
	if !e.isBookFresh(bookA, nowMs) || !e.isBookFresh(bookB, nowMs) {
		e.incRejection("stale_book")
		return routeCandidate{}, false
	}
	fundingA := snap.fundings[cexA.Name][perpSym]
	fundingB := snap.fundings[cexB.Name][perpSym]
	if fundingA == nil || fundingB == nil || !e.isFundingFresh(fundingA, nowMs) || !e.isFundingFresh(fundingB, nowMs) {
		e.incRejection("stale_funding")
		return routeCandidate{}, false
	}
	return routeCandidate{
		routeType: domain.RoutePerpPerpCrossCEX,
		baseAsset: base,
		cexA:      cexA,
		cexB:      cexB,
		actionA:   domain.ActionBuyLong,
		actionB:   domain.ActionSellShort,
		marketA:   domain.MarketTypeLinearPerp,
		marketB:   domain.MarketTypeLinearPerp,
		bookA:     bookA,
		bookB:     bookB,
		fundingA:  fundingA,
		fundingB:  fundingB,
		feeA:      cexA.PerpTakerFee,
		feeB:      cexB.PerpTakerFee,
	}, true
}

func (e *MatrixEvaluator) buildCrossCEXSpotPerp(
	base string,
	spotSym, perpSym domain.CanonSymbol,
	snap storeSnapshot,
	nowMs int64,
) []routeCandidate {
	var out []routeCandidate
	for _, cexA := range snap.healthyList {
		bookA := snap.books[cexA.Name][spotSym]
		if !cexA.Capabilities.HasSpot || bookA == nil {
			continue
		}
		for _, cexB := range snap.healthyList {
			if cand, ok := e.trySpotPerpPair(base, cexA, cexB, bookA, snap, perpSym, nowMs); ok {
				out = append(out, cand)
			}
		}
	}
	return out
}

func (e *MatrixEvaluator) trySpotPerpPair(
	base string,
	cexA, cexB ExchangeMeta,
	bookA *domain.OrderBookSnapshot,
	snap storeSnapshot,
	perpSym domain.CanonSymbol,
	nowMs int64,
) (routeCandidate, bool) {
	if cexA.Name == cexB.Name || !cexB.Capabilities.HasPerp {
		return routeCandidate{}, false
	}
	bookB := snap.books[cexB.Name][perpSym]
	if bookB == nil {
		return routeCandidate{}, false
	}
	if !e.isBookFresh(bookA, nowMs) || !e.isBookFresh(bookB, nowMs) {
		e.incRejection("stale_book")
		return routeCandidate{}, false
	}
	fundingB := snap.fundings[cexB.Name][perpSym]
	if fundingB == nil || !e.isFundingFresh(fundingB, nowMs) {
		e.incRejection("stale_funding")
		return routeCandidate{}, false
	}
	return routeCandidate{
		routeType: domain.RouteSpotPerpCrossCEX,
		baseAsset: base,
		cexA:      cexA,
		cexB:      cexB,
		actionA:   domain.ActionBuySpot,
		actionB:   domain.ActionSellShort,
		marketA:   domain.MarketTypeSpot,
		marketB:   domain.MarketTypeLinearPerp,
		bookA:     bookA,
		bookB:     bookB,
		fundingA:  nil,
		fundingB:  fundingB,
		feeA:      cexA.SpotTakerFee,
		feeB:      cexB.PerpTakerFee,
	}, true
}

func (e *MatrixEvaluator) evaluateCandidateForProfile(
	cand routeCandidate,
	prof domain.ProfileConfig,
	gross24h, longRate float64,
	longInterval int,
	nowMs int64,
	getVWAP func(*domain.OrderBookSnapshot, domain.LegAction, float64) (VWAPResult, error),
) (domain.SignalPayload, bool) {
	if gross24h <= 0 {
		e.incRejection("negative_gross")
		return domain.SignalPayload{}, false
	}
	if cand.bookA.Volume24hUSDT < prof.MinVolume24hUSDT || cand.bookB.Volume24hUSDT < prof.MinVolume24hUSDT {
		e.incRejection("low_volume")
		return domain.SignalPayload{}, false
	}
	vwapA, errA := getVWAP(cand.bookA, cand.actionA, prof.TargetNotionalUSDT)
	vwapB, errB := getVWAP(cand.bookB, cand.actionB, prof.TargetNotionalUSDT)
	if errA != nil || errB != nil {
		e.incRejection("insufficient_depth")
		return domain.SignalPayload{}, false
	}
	if prof.MaxInitialBasisSpread > 0 {
		basisSpread := math.Abs(vwapA.VWAPPrice-vwapB.VWAPPrice) / vwapA.VWAPPrice
		if basisSpread > prof.MaxInitialBasisSpread {
			e.incRejection("high_basis_spread")
			return domain.SignalPayload{}, false
		}
	}
	metrics := ComputeMetrics(gross24h, cand.feeA, cand.feeB, vwapA.Slippage, vwapB.Slippage, prof.TargetHoldDays)
	if metrics.BreakevenDays > prof.MaxBreakevenDays {
		e.incRejection("high_breakeven")
		return domain.SignalPayload{}, false
	}
	if metrics.NetDailyYield < prof.MinNetDailyYield {
		e.incRejection("low_net_yield")
		return domain.SignalPayload{}, false
	}

	dedupKey := fmt.Sprintf("%s:%s:%s:%s:%s", prof.ID, cand.routeType, cand.baseAsset, cand.cexA.Name, cand.cexB.Name)
	sigID := fmt.Sprintf("sig_%s_%s_%s_%s_%d", prof.ID, cand.cexA.Name, cand.cexB.Name, cand.baseAsset, nowMs)
	return domain.SignalPayload{
		SchemaVersion:      domain.SchemaVersionV31,
		SignalID:           sigID,
		DedupKey:           dedupKey,
		ProfileID:          prof.ID,
		RouteType:          cand.routeType,
		BaseAsset:          cand.baseAsset,
		TargetNotionalUSDT: prof.TargetNotionalUSDT,
		Legs: domain.SignalLegs{
			LegA: domain.SignalLeg{
				Exchange:      cand.cexA.Name,
				MarketType:    cand.marketA,
				RawSymbol:     cand.bookA.RawSymbol,
				CanonSymbol:   cand.bookA.CanonSymbol,
				Action:        cand.actionA,
				VWAPPrice:     vwapA.VWAPPrice,
				FundingRate:   longRate,
				IntervalHours: longInterval,
			},
			LegB: domain.SignalLeg{
				Exchange:      cand.cexB.Name,
				MarketType:    cand.marketB,
				RawSymbol:     cand.bookB.RawSymbol,
				CanonSymbol:   cand.bookB.CanonSymbol,
				Action:        cand.actionB,
				VWAPPrice:     vwapB.VWAPPrice,
				FundingRate:   cand.fundingB.FundingRate,
				IntervalHours: cand.fundingB.IntervalHours,
			},
		},
		Metrics:       metrics,
		GeneratedAtMs: nowMs,
		ExpiresAtMs:   nowMs + e.cfg.Engine.SignalValidityMs,
	}, true
}
