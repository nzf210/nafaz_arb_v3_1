package engine

import (
	"errors"
	"fmt"
	"math"
	"sync"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/domain"
)

var (
	ErrInsufficientDepth = errors.New("insufficient orderbook depth for target notional")
	ErrEmptyOrderBook    = errors.New("orderbook has no bids or asks")
)

type ExchangeMeta struct {
	Name         string
	Healthy      bool
	Capabilities adapter.Capabilities
	SpotTakerFee float64
	PerpTakerFee float64
}

type VWAPResult struct {
	VWAPPrice float64
	MidPrice  float64
	Slippage  float64
	BaseQty   float64
}

type StateStore struct {
	mu          sync.RWMutex
	registry    *MarketRegistry
	exchanges   map[string]ExchangeMeta
	books       map[string]map[domain.CanonSymbol]*domain.OrderBookSnapshot
	fundings    map[string]map[domain.CanonSymbol]*domain.FundingSnapshot
	collaterals map[string]*domain.CollateralSnapshot
}

func NewStateStore() *StateStore {
	return &StateStore{
		exchanges:   make(map[string]ExchangeMeta),
		books:       make(map[string]map[domain.CanonSymbol]*domain.OrderBookSnapshot),
		fundings:    make(map[string]map[domain.CanonSymbol]*domain.FundingSnapshot),
		collaterals: make(map[string]*domain.CollateralSnapshot),
	}
}

func (s *StateStore) SetMarketRegistry(reg *MarketRegistry) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.registry = reg
}

func (s *StateStore) MarketRegistry() *MarketRegistry {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.registry
}

func (s *StateStore) AllExchanges() []ExchangeMeta {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]ExchangeMeta, 0, len(s.exchanges))
	for _, meta := range s.exchanges {
		out = append(out, meta)
	}
	return out
}

func (s *StateStore) RemoveMarket(exchange string, sym domain.CanonSymbol) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if bySym, ok := s.books[exchange]; ok {
		delete(bySym, sym)
	}
	if bySym, ok := s.fundings[exchange]; ok {
		delete(bySym, sym)
	}
}

func (s *StateStore) RetainExchangeMarkets(exchange string, active []domain.MarketDescriptor) {
	keep := make(map[domain.CanonSymbol]struct{}, len(active))
	for _, desc := range active {
		keep[desc.CanonSymbol] = struct{}{}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if bySym, ok := s.books[exchange]; ok {
		for sym := range bySym {
			if _, ok := keep[sym]; !ok {
				delete(bySym, sym)
			}
		}
	}
	if bySym, ok := s.fundings[exchange]; ok {
		for sym := range bySym {
			if _, ok := keep[sym]; !ok {
				delete(bySym, sym)
			}
		}
	}
}

func (s *StateStore) UpsertExchange(meta ExchangeMeta) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.exchanges[meta.Name] = meta
}

func (s *StateStore) SetExchangeHealthy(name string, healthy bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if meta, ok := s.exchanges[name]; ok {
		meta.Healthy = healthy
		s.exchanges[name] = meta
	}
}

func (s *StateStore) GetExchange(name string) (ExchangeMeta, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	meta, ok := s.exchanges[name]
	return meta, ok
}

func (s *StateStore) UpdateOrderBook(snap *domain.OrderBookSnapshot) {
	if snap == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.books[snap.Exchange]; !ok {
		s.books[snap.Exchange] = make(map[domain.CanonSymbol]*domain.OrderBookSnapshot)
	}
	s.books[snap.Exchange][snap.CanonSymbol] = snap
}

func (s *StateStore) GetOrderBook(exchange string, sym domain.CanonSymbol) (*domain.OrderBookSnapshot, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	bySym, ok := s.books[exchange]
	if !ok {
		return nil, false
	}
	snap, ok := bySym[sym]
	return snap, ok
}

func (s *StateStore) UpdateFunding(snap *domain.FundingSnapshot) {
	if snap == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.fundings[snap.Exchange]; !ok {
		s.fundings[snap.Exchange] = make(map[domain.CanonSymbol]*domain.FundingSnapshot)
	}
	s.fundings[snap.Exchange][snap.CanonSymbol] = snap
}

func (s *StateStore) GetFunding(exchange string, sym domain.CanonSymbol) (*domain.FundingSnapshot, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	bySym, ok := s.fundings[exchange]
	if !ok {
		return nil, false
	}
	snap, ok := bySym[sym]
	return snap, ok
}

func (s *StateStore) UpdateCollateral(snap *domain.CollateralSnapshot) {
	if snap == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.collaterals[snap.Exchange] = snap
}

func (s *StateStore) GetCollateral(exchange string) (*domain.CollateralSnapshot, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	snap, ok := s.collaterals[exchange]
	return snap, ok
}

func ComputeVWAP(book *domain.OrderBookSnapshot, action domain.LegAction, targetNotionalUSDT float64) (VWAPResult, error) {
	if book == nil || len(book.Bids) == 0 || len(book.Asks) == 0 {
		return VWAPResult{}, ErrEmptyOrderBook
	}
	if targetNotionalUSDT <= 0 {
		return VWAPResult{}, errors.New("targetNotionalUSDT must be > 0")
	}

	bestBid := book.Bids[0].Price
	bestAsk := book.Asks[0].Price
	if bestBid <= 0 || bestAsk <= 0 {
		return VWAPResult{}, ErrEmptyOrderBook
	}
	midPrice := (bestBid + bestAsk) / 2.0

	var levels []domain.OrderBookLevel
	switch action {
	case domain.ActionBuySpot, domain.ActionBuyLong:
		levels = book.Asks
	case domain.ActionSellShort:
		levels = book.Bids
	default:
		return VWAPResult{}, fmt.Errorf("unsupported action %q", action)
	}

	remainingNotional := targetNotionalUSDT
	var accumNotional, accumQty float64

	for _, lvl := range levels {
		if lvl.Price <= 0 || lvl.Quantity <= 0 {
			continue
		}
		lvlNotional := lvl.Price * lvl.Quantity
		if lvlNotional >= remainingNotional {
			takeQty := remainingNotional / lvl.Price
			accumNotional += remainingNotional
			accumQty += takeQty
			remainingNotional = 0
			break
		}
		accumNotional += lvlNotional
		accumQty += lvl.Quantity
		remainingNotional -= lvlNotional
	}

	if remainingNotional > 1e-6 || accumQty <= 0 {
		return VWAPResult{}, ErrInsufficientDepth
	}

	vwap := accumNotional / accumQty
	slippage := math.Abs(vwap-midPrice) / midPrice

	return VWAPResult{
		VWAPPrice: vwap,
		MidPrice:  midPrice,
		Slippage:  slippage,
		BaseQty:   accumQty,
	}, nil
}

func NormalizeGrossFunding24h(longRate float64, longIntervalHours int, isSpotLong bool, shortRate float64, shortIntervalHours int) float64 {
	var longTerm float64
	if !isSpotLong && longIntervalHours > 0 {
		longTerm = -longRate * (24.0 / float64(longIntervalHours))
	}
	var shortTerm float64
	if shortIntervalHours > 0 {
		shortTerm = shortRate * (24.0 / float64(shortIntervalHours))
	}
	return longTerm + shortTerm
}

func ComputeMetrics(grossFunding24h, feeLegA, feeLegB, slipLegA, slipLegB, targetHoldDays float64) domain.SignalMetrics {
	cFeeRT := 2.0*feeLegA + 2.0*feeLegB
	cSlipRT := 2.0 * (slipLegA + slipLegB)
	cRT := cFeeRT + cSlipRT

	netDailyYield := grossFunding24h - (cRT / targetHoldDays)
	denom := math.Max(grossFunding24h, 1e-6)
	breakevenDays := cRT / denom

	return domain.SignalMetrics{
		GrossFunding24h: grossFunding24h,
		RoundTripCost:   cRT,
		NetDailyYield:   netDailyYield,
		BreakevenDays:   breakevenDays,
	}
}

type MatrixEvaluator struct {
	cfg            *domain.ScannerConfig
	store          *StateStore
	rejectionMu    sync.Mutex
	rejectionStats map[string]int64
}

func NewMatrixEvaluator(cfg *domain.ScannerConfig, store *StateStore) *MatrixEvaluator {
	return &MatrixEvaluator{
		cfg:            cfg,
		store:          store,
		rejectionStats: make(map[string]int64),
	}
}

func (e *MatrixEvaluator) incRejection(reason string) {
	e.rejectionMu.Lock()
	e.rejectionStats[reason]++
	e.rejectionMu.Unlock()
}

func (e *MatrixEvaluator) RejectionStats() map[string]int64 {
	e.rejectionMu.Lock()
	defer e.rejectionMu.Unlock()
	out := make(map[string]int64, len(e.rejectionStats))
	for k, v := range e.rejectionStats {
		out[k] = v
	}
	return out
}

func (e *MatrixEvaluator) isBookFresh(book *domain.OrderBookSnapshot, nowMs int64) bool {
	if book == nil {
		return false
	}
	age := nowMs - book.TimestampMs
	return age <= e.cfg.Freshness.MaxBookAgeMs
}

func (e *MatrixEvaluator) isFundingFresh(funding *domain.FundingSnapshot, nowMs int64) bool {
	if funding == nil {
		return false
	}
	age := nowMs - funding.TimestampMs
	return age <= e.cfg.Freshness.MaxFundingAgeSec*1000
}

func (e *MatrixEvaluator) isCollateralFresh(col *domain.CollateralSnapshot, nowMs int64) bool {
	if col == nil {
		return false
	}
	age := nowMs - col.TimestampMs
	return age <= e.cfg.Freshness.MaxCollateralAgeSec*1000
}

type routeCandidate struct {
	routeType domain.RouteType
	baseAsset string
	cexA      ExchangeMeta
	cexB      ExchangeMeta
	actionA   domain.LegAction
	actionB   domain.LegAction
	marketA   domain.MarketType
	marketB   domain.MarketType
	bookA     *domain.OrderBookSnapshot
	bookB     *domain.OrderBookSnapshot
	fundingA  *domain.FundingSnapshot
	fundingB  *domain.FundingSnapshot
	feeA      float64
	feeB      float64
}

type vwapCacheKey struct {
	exchange   string
	symbol     domain.CanonSymbol
	action     domain.LegAction
	notionalE2 int64
}

type vwapCacheEntry struct {
	res VWAPResult
	err error
}

type evaluationOutput struct {
	signals   []domain.SignalPayload
	vwapCalls int
}

func (e *MatrixEvaluator) EvaluateTick(nowMs int64) []domain.SignalPayload {
	out := e.evaluateTickInternal(nowMs)
	return out.signals
}

func (e *MatrixEvaluator) evaluateTickInternal(nowMs int64) evaluationOutput {
	snap := e.snapshotStore()

	var candidates []routeCandidate
	for _, base := range snap.baseAssets {
		candidates = append(candidates, e.buildCandidatesForAsset(base, snap, nowMs)...)
	}

	vwapCache := make(map[vwapCacheKey]vwapCacheEntry)
	vwapCalls := 0
	getVWAP := func(book *domain.OrderBookSnapshot, action domain.LegAction, notional float64) (VWAPResult, error) {
		key := vwapCacheKey{
			exchange:   book.Exchange,
			symbol:     book.CanonSymbol,
			action:     action,
			notionalE2: int64(math.Round(notional * 100)),
		}
		if entry, ok := vwapCache[key]; ok {
			return entry.res, entry.err
		}
		vwapCalls++
		res, err := ComputeVWAP(book, action, notional)
		vwapCache[key] = vwapCacheEntry{res: res, err: err}
		return res, err
	}

	var signals []domain.SignalPayload
	for _, cand := range candidates {
		isSpotLong := cand.marketA == domain.MarketTypeSpot
		var longRate float64
		var longInterval int
		if !isSpotLong && cand.fundingA != nil {
			longRate = cand.fundingA.FundingRate
			longInterval = cand.fundingA.IntervalHours
		}
		gross24h := NormalizeGrossFunding24h(longRate, longInterval, isSpotLong, cand.fundingB.FundingRate, cand.fundingB.IntervalHours)

		for _, prof := range e.cfg.Profiles {
			if payload, ok := e.evaluateCandidateForProfile(cand, prof, gross24h, longRate, longInterval, nowMs, getVWAP); ok {
				signals = append(signals, payload)
			}
		}
	}

	return evaluationOutput{
		signals:   signals,
		vwapCalls: vwapCalls,
	}
}
