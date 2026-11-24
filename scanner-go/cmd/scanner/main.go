package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/adapter/bybit"
	"nafaz_arb/internal/adapter/mexc"
	"nafaz_arb/internal/adapter/okx"
	"nafaz_arb/internal/domain"
	"nafaz_arb/internal/engine"
	"nafaz_arb/internal/sink"
)

type logEntry struct {
	Time    string         `json:"time"`
	Level   string         `json:"level"`
	Message string         `json:"msg"`
	Fields  map[string]any `json:"fields,omitempty"`
}

func logJSON(level, msg string, fields map[string]any) {
	entry := logEntry{
		Time:    time.Now().UTC().Format(time.RFC3339Nano),
		Level:   level,
		Message: msg,
		Fields:  fields,
	}
	b, _ := json.Marshal(entry)
	fmt.Println(string(b))
}

func BuildDefaultRegistry() *adapter.Registry {
	reg := adapter.NewRegistry()
	reg.Register("okx", okx.NewAdapter)
	reg.Register("bybit", bybit.NewAdapter)
	reg.Register("mexc", mexc.NewAdapter)
	return reg
}

type Runner struct {
	ScannerCfg        *domain.ScannerConfig
	ExchangesCfg      *domain.ExchangesConfig
	Registry          *adapter.Registry
	Adapters          map[string]adapter.ExchangeAdapter
	Store             *engine.StateStore
	Evaluator         *engine.MatrixEvaluator
	Publisher         *sink.SignalPublisher
	 pollMu            sync.Mutex
	lastFundingPollMs map[string]int64
	lastCollatPollMs  map[string]int64
}

func NewRunner(scannerCfg *domain.ScannerConfig, exchangesCfg *domain.ExchangesConfig, reg *adapter.Registry, kv sink.KVStore) (*Runner, error) {
	store := engine.NewStateStore()
	adapters := make(map[string]adapter.ExchangeAdapter)

	for name, exCfg := range exchangesCfg.Exchanges {
		if !exCfg.Enabled {
			continue
		}
		ad, err := reg.Create(name, exCfg)
		if err != nil {
			return nil, fmt.Errorf("init adapter %s: %w", name, err)
		}
		adapters[name] = ad
		store.UpsertExchange(engine.ExchangeMeta{
			Name:         name,
			Healthy:      true,
			Capabilities: ad.Capabilities(),
			SpotTakerFee: exCfg.SpotTakerFee,
			PerpTakerFee: exCfg.PerpTakerFee,
		})
	}

	if kv == nil {
		if scannerCfg.Engine.DryRun {
			kv = sink.NewMemoryKVStore()
		} else {
			kv = sink.NewRESPClient(scannerCfg.Engine.RedisAddr, 2*time.Second)
		}
	}

	return &Runner{
		ScannerCfg:        scannerCfg,
		ExchangesCfg:      exchangesCfg,
		Registry:          reg,
		Adapters:          adapters,
		Store:             store,
		Evaluator:         engine.NewMatrixEvaluator(scannerCfg, store),
		Publisher:         sink.NewSignalPublisher(kv, scannerCfg.Engine),
		lastFundingPollMs: make(map[string]int64),
		lastCollatPollMs:  make(map[string]int64),
	}, nil
}

func (r *Runner) shouldPollCollateral(name string, intervalSec int, nowMs int64) bool {
	if intervalSec <= 0 {
		return true
	}
	r.pollMu.Lock()
	defer r.pollMu.Unlock()
	last := r.lastCollatPollMs[name]
	if last == 0 || (nowMs-last) >= int64(intervalSec)*1000 {
		r.lastCollatPollMs[name] = nowMs
		return true
	}
	return false
}

func (r *Runner) shouldPollFunding(key string, sym domain.CanonSymbol, exName string, cfg domain.PollingConfig, nowMs int64) bool {
	intervalSec := cfg.FundingIntervalSec
	if snap, ok := r.Store.GetFunding(exName, sym); ok && snap != nil && snap.NextSettlementMs > nowMs {
		if (snap.NextSettlementMs - nowMs) <= 2*60*1000 && cfg.FundingFastIntervalSec > 0 {
			intervalSec = cfg.FundingFastIntervalSec
		}
	}
	if intervalSec <= 0 {
		return true
	}
	r.pollMu.Lock()
	defer r.pollMu.Unlock()
	last := r.lastFundingPollMs[key]
	if last == 0 || (nowMs-last) >= int64(intervalSec)*1000 {
		r.lastFundingPollMs[key] = nowMs
		return true
	}
	return false
}

func (r *Runner) PollOnce(ctx context.Context) {
	pollTimeout := time.Duration(r.ScannerCfg.Engine.TickIntervalMs) * time.Millisecond
	if pollTimeout <= 0 {
		pollTimeout = 2 * time.Second
	}
	pollCtx, cancel := context.WithTimeout(ctx, pollTimeout)
	defer cancel()

	var wg sync.WaitGroup
	for name, ad := range r.Adapters {
		r.scheduleExchangePoll(pollCtx, &wg, name, ad)
	}
	wg.Wait()
}

func (r *Runner) scheduleExchangePoll(ctx context.Context, wg *sync.WaitGroup, name string, ad adapter.ExchangeAdapter) {
	exCfg := r.ExchangesCfg.Exchanges[name]
	caps := ad.Capabilities()
	nowMs := time.Now().UnixMilli()

	if caps.HasCollateralInfo && r.shouldPollCollateral(name, exCfg.Polling.CollateralIntervalSec, nowMs) {
		wg.Add(1)
		go r.pollCollateral(ctx, wg, ad)
	}

	for _, base := range exCfg.SymbolAllowlist {
		if caps.HasSpot {
			wg.Add(1)
			go r.pollSpotSymbol(ctx, wg, ad, base)
		}
		if caps.HasPerp {
			wg.Add(1)
			go r.pollPerpSymbol(ctx, wg, name, ad, base, exCfg.Polling, nowMs)
		}
	}
	_ = r.Publisher.UpdateExchangeHealth(ctx, name, "healthy", nowMs, len(exCfg.SymbolAllowlist), 0)
}

func (r *Runner) pollCollateral(ctx context.Context, wg *sync.WaitGroup, ad adapter.ExchangeAdapter) {
	defer wg.Done()
	if col, err := ad.FetchCollateralRatio(ctx); err == nil && col != nil {
		r.Store.UpdateCollateral(col)
		_ = r.Publisher.UpdateCollateralSnapshot(ctx, col)
	}
}

func (r *Runner) pollSpotSymbol(ctx context.Context, wg *sync.WaitGroup, ad adapter.ExchangeAdapter, base string) {
	defer wg.Done()
	sym, err := domain.NewCanonSymbol(base, domain.MarketTypeSpot)
	if err != nil {
		return
	}
	if book, err := ad.FetchOrderBook(ctx, sym); err == nil && book != nil {
		r.Store.UpdateOrderBook(book)
	}
}

func (r *Runner) pollPerpSymbol(ctx context.Context, wg *sync.WaitGroup, exName string, ad adapter.ExchangeAdapter, base string, pollCfg domain.PollingConfig, nowMs int64) {
	defer wg.Done()
	sym, err := domain.NewCanonSymbol(base, domain.MarketTypeLinearPerp)
	if err != nil {
		return
	}
	if book, err := ad.FetchOrderBook(ctx, sym); err == nil && book != nil {
		r.Store.UpdateOrderBook(book)
	}
	if r.shouldPollFunding(exName+":"+string(sym), sym, exName, pollCfg, nowMs) {
		if fund, err := ad.FetchFundingRate(ctx, sym); err == nil && fund != nil {
			r.Store.UpdateFunding(fund)
		}
	}
}

func (r *Runner) RunTick(ctx context.Context, nowMs int64) (int, error) {
	signals := r.Evaluator.EvaluateTick(nowMs)
	published, err := r.Publisher.PublishBatch(ctx, signals)
	_ = r.Publisher.RecordRejectionStats(ctx, r.Evaluator.RejectionStats())
	return published, err
}

func main() {
	defaultScannerCfg := os.Getenv("SCANNER_CONFIG")
	if defaultScannerCfg == "" {
		defaultScannerCfg = "configs/scanner.base.yaml"
	}
	defaultExchangesCfg := os.Getenv("EXCHANGES_CONFIG")
	if defaultExchangesCfg == "" {
		defaultExchangesCfg = "configs/exchanges.yaml"
	}

	scannerCfgPath := flag.String("scanner-config", defaultScannerCfg, "path to scanner.base.yaml")
	exchangesCfgPath := flag.String("exchanges-config", defaultExchangesCfg, "path to exchanges.yaml")
	dryRunFlag := flag.Bool("dry-run", false, "force dry-run/shadow mode override")
	onceFlag := flag.Bool("once", false, "run single tick and exit")
	flag.Parse()

	scannerCfg, err := domain.LoadScannerConfig(*scannerCfgPath)
	if err != nil {
		logJSON("error", "failed to load scanner config", map[string]any{"err": err.Error()})
		os.Exit(1)
	}
	if *dryRunFlag {
		scannerCfg.Engine.DryRun = true
	}

	exchangesCfg, err := domain.LoadExchangesConfig(*exchangesCfgPath)
	if err != nil {
		logJSON("error", "failed to load exchanges config", map[string]any{"err": err.Error()})
		os.Exit(1)
	}

	runner, err := NewRunner(scannerCfg, exchangesCfg, BuildDefaultRegistry(), nil)
	if err != nil {
		logJSON("error", "failed to initialize runner", map[string]any{"err": err.Error()})
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	logJSON("info", "nafaz_arb v3.1 scanner started", map[string]any{
		"dry_run":   scannerCfg.Engine.DryRun,
		"exchanges": runner.Registry.Names(),
	})

	if *onceFlag {
		runner.PollOnce(ctx)
		pub, _ := runner.RunTick(ctx, time.Now().UnixMilli())
		logJSON("info", "single tick completed", map[string]any{"published_signals": pub})
		return
	}

	ticker := time.NewTicker(time.Duration(scannerCfg.Engine.TickIntervalMs) * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			logJSON("info", "shutting down scanner gracefully", nil)
			return
		case t := <-ticker.C:
			runner.PollOnce(ctx)
			pub, _ := runner.RunTick(ctx, t.UnixMilli())
			logJSON("info", "tick evaluated", map[string]any{"published": pub})
		}
	}
}
