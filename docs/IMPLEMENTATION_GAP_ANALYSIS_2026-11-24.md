# NAFAZ ARB V3.1 — Implementation Gap Analysis, Repository Audit & Execution Plan

## Phase 1 — Complete Repository Audit Report (2026-11-25)

**Audit date:** 2026-11-25
**Audited source ref:** `master` (`d7ec339`)
**Working branch:** `feat/dynamic-market-discovery`
**Implementation status:** Automatic Market Discovery, Strict Symbol Normalization, Cross-CEX Matching (`MarketRegistry`), and Dynamic Bounded Polling implemented and verified via automated Go & Python test suites.

### 1.1 Audited Repository Structure & Component Status

| Component / Path | Audited Role | Baseline Implementation Reality (`master`) | Gap / Discrepancy Identified |
| :--- | :--- | :--- | :--- |
| `configs/exchanges.yaml` | Exchange endpoints, fees, rate limits, symbol selection | Hardcodes `symbol_allowlist: ["BTC", "ETH", "SOL"]` for `okx`, `bybit`, and `mexc`. | Forces manual pair listing; lacks `market_discovery` block (`enabled`, `markets`, `quote_assets`, `refresh_interval_sec`, `blocklist`, optional `allowlist`). |
| `configs/scanner.base.yaml` | Engine tick, freshness thresholds, route policies, profiles | Configures `tick_interval_ms: 1000`, `freshness`, `route_policies`, and 2 evaluation profiles. | Matches V3.1 engine specification; no discrepancy in math/profile parameters. |
| `docs/PRD_v3_1.md` & `prd_dan_roadmap_nafaz_arb_v3_1.md` | Product Requirements Document | `prd_dan_roadmap_nafaz_arb_v3_1.md` is an older duplicate out of sync with `docs/PRD_v3_1.md`. Neither contains complete specs for Automatic Market Discovery, Canonical Symbol Normalization rules, Cross-CEX Matching rules, or Dynamic Polling lifecycle. | Both PRD files must be unified and expanded with explicit specs for Automatic Market Discovery, Symbol Normalization, Cross-CEX Matching, Config Reference, and Data Flow. |
| `README.md` | Architecture & operator guide | Steps 1 & 3 instruct operators to configure `symbol_allowlist: ["BTC", "ETH", "SOL"]` and omit `FetchMarkets` from `ExchangeAdapter`. | Contradicts automatic market discovery architecture; must document the 9-stage discovery-to-publisher flow and current implementation status. |
| `scanner-go/internal/domain/types.go` | Canonical symbols, snapshots, signal payload, config structs | Defines `CanonSymbol` (`BASE/USDT`, `BASE/USDT:USDT`), `OrderBookSnapshot`, `FundingSnapshot`, `CollateralSnapshot`, `SignalPayload`, `ExchangeConfig`. | Missing `MarketDescriptor` domain model, `MarketDiscoveryConfig` struct, and structured `(base, quote, marketType)` normalization helper. |
| `scanner-go/internal/adapter/contract.go` | `ExchangeAdapter` interface & dynamic `Registry` | Defines `Name`, `Capabilities`, `FetchOrderBook`, `FetchFundingRate`, `FetchCollateralRatio`, `NormalizeSymbol`, `ToRawSymbol`. | Missing `FetchMarkets(ctx context.Context) ([]domain.MarketDescriptor, error)` in `ExchangeAdapter`. |
| `scanner-go/internal/adapter/okx/okx.go` | OKX v5 REST adapter | Fetches orderbook (`/api/v5/market/books`), funding (`/api/v5/public/funding-rate`), collateral (`/api/v5/public/discount-rate-interest-free-quota`), and lazy `ctVal`. | Missing `FetchMarkets` via `/api/v5/public/instruments?instType=SPOT` & `instType=SWAP`. Also `NormalizeSymbol` accepts `-USDT` (Spot suffix) when `marketType == MarketTypeLinearPerp`, risking Spot/Perp confusion. |
| `scanner-go/internal/adapter/bybit/bybit.go` | Bybit v5 REST adapter | Fetches orderbook (`/v5/market/orderbook`), funding (`/v5/market/tickers`), collateral (`/v5/account/collateral-info`). | Missing `FetchMarkets` via `/v5/market/instruments-info?category=spot` & `category=linear`. |
| `scanner-go/internal/adapter/mexc/mexc.go` | MEXC Spot v3 & Contract v1 REST adapter | Fetches orderbook (`/api/v3/depth` & `/api/v1/contract/depth/{sym}`), funding (`/api/v1/contract/funding_rate/{sym}`). | Missing `FetchMarkets` via `/api/v3/exchangeInfo` (Spot) and `/api/v1/contract/detail` (Linear Perp). Also `NormalizeSymbol` allows `BTC_USDT` (Perp format) in Spot and `BTCUSDT` (Spot format) in Perp. |
| `scanner-go/internal/adapter/resilience/resilience.go` | Token-bucket rate limiter, backoff+jitter, circuit breaker | Working token-bucket `RateLimiter`, `CircuitBreaker`, `Client.GetJSON`, and `PollingInterval`. | Solid foundation; keep intact and reuse for discovery and polling calls. |
| `scanner-go/internal/engine/matrix_evaluator.go` & `route_builder.go` | `StateStore`, VWAP, funding normalization, auto-route matrix, multi-profile evaluator | Evaluates `spot_perp_same_cex`, `perp_perp_cross_cex`, and `spot_perp_cross_cex` from `StateStore.books` and `StateStore.fundings`. | `StateStore` lacks market eviction (`RemoveMarket` / `RetainMarkets`) when markets delist or become inactive. Route building scans all books in store rather than an explicitly matched active market universe. |
| `scanner-go/cmd/scanner/main.go` | Runner orchestration & polling loop | `scheduleExchangePoll` loops over `exCfg.SymbolAllowlist`, spawns unbounded goroutines, and marks exchange `"healthy"` unconditionally. | No automatic market discovery, no `MarketRegistry`, no Cross-CEX matcher, no periodic discovery refresh, no delisting cleanup, no bounded polling concurrency, and no duplicate-task guard. |
| `scanner-go/internal/sink/redis_pub.go` | Signal deduplicator & Redis publisher (`core:*`, `meta:*`) | Working v3.1 signal validation, cooldown deduplication with `MinYieldDelta` override, and `meta:*` health/rejection hashes. | Preserve existing V3.1 behavior. |

### 1.2 Files Identified for Modification / Creation

1. **PRD & Documentation (Phase 2 & Phase 3)**:
   - `docs/IMPLEMENTATION_GAP_ANALYSIS_2026-11-24.md`
   - `docs/PRD_v3_1.md`
   - `prd_dan_roadmap_nafaz_arb_v3_1.md`
   - `README.md`
2. **Configuration (Phase 4)**:
   - `configs/exchanges.yaml`
3. **Go Scanner Source Code & Tests (Phase 5, 6, 7)**:
   - `scanner-go/internal/domain/types.go` & `scanner-go/internal/domain/types_test.go`
   - `scanner-go/internal/adapter/contract.go` & `scanner-go/internal/adapter/contract_test.go`
   - `scanner-go/internal/adapter/okx/okx.go` & `scanner-go/internal/adapter/okx/okx_test.go`
   - `scanner-go/internal/adapter/bybit/bybit.go` & `scanner-go/internal/adapter/bybit/bybit_test.go`
   - `scanner-go/internal/adapter/mexc/mexc.go` & `scanner-go/internal/adapter/mexc/mexc_test.go`
   - `scanner-go/internal/engine/market_registry.go` & `scanner-go/internal/engine/market_registry_test.go`
   - `scanner-go/internal/engine/matrix_evaluator.go`, `scanner-go/internal/engine/route_builder.go`, & `scanner-go/internal/engine/matrix_evaluator_test.go`
   - `scanner-go/cmd/scanner/main.go` & `scanner-go/cmd/scanner/main_test.go`

---

## Prior Static Audit Baseline (2026-11-24)

**Audit date:** 2026-11-24  
**Audited source ref:** `master` at the time of audit  
**Working branch:** `feat/market-discovery-and-instrument-registry-v2`  
**Status:** Initial static audit; no runtime test result is implied by this document.

## Executive decision

Do not expand live market coverage or increase live notional until the execution and position lifecycle can prove that every order state and every residual exposure is reconciled with the exchanges. A funding spread is an estimate, not guaranteed income. The previous PRD's 4–7% monthly return range is not a guaranteed or validated system capability and must not be used as a production promise.

## Repository topology finding

The repository default branch `main` contained only `LICENSE` at audit time. The application source, configuration, tests, and PRD were on `master`. Implementation work therefore starts from `master`, not `main`. Keep this distinction explicit until the repository owner chooses a deliberate default-branch migration.

## Confirmed findings from source inspection

### P0 — Executor safety

1. `executor-py/execution.py::check_sufficient_balance` previously treated missing clients, missing free-USDT fields, and balance-fetch exceptions as sufficient balance. This is fail-open behavior for live execution.
2. `_resolve_filled_base` previously treated an absent fill quantity as the full requested amount when the exchange order response did not contain a usable fill state. This can create a false hedge assumption.
3. `_create_close_order` previously retried a perpetual close without `reduceOnly` after a `TypeError`. A rejected reduce-only request must not silently become an order that can reverse exposure.
4. `unwind_route` returns `UNWOUND_1TO1` after order submission without independently confirming both exchange-side fills and residual exposure.
5. `executor-py/watchdog.py::check_adl_indicators` and `check_exit_conditions` call `mark_position_closed` whenever an unwind callback returns, without requiring verified zero exposure on both legs. The callback's return status is not a substitute for reconciliation.
6. `round_price` ignored its side argument and always rounded down. This can produce invalid or less aggressive prices for sell orders.

### P1 — Signal delivery and deduplication

1. `scanner-go/internal/sink/redis_pub.go::PublishSignal` uses separate GET and SET operations for deduplication. Concurrent publishers can race.
2. The dedup key is based on route, base asset, and venue pair; the profile identity is not part of the key. One profile may suppress another profile's signal.
3. The dedup key is written before the Pub/Sub publish. A publish failure can leave a cooldown marker for a signal that was never delivered.
4. Redis Pub/Sub is ephemeral. It does not provide durable consumer acknowledgement, retry, pending-entry recovery, or a dead-letter queue.

### P1 — Market discovery and data ingestion

1. `scanner-go/cmd/scanner/main.go::scheduleExchangePoll` iterates configured `SymbolAllowlist`; it does not enumerate the exchange's complete market catalogue.
2. `configs/exchanges.yaml` currently restricts all three enabled exchanges to BTC, ETH, and SOL.
3. `ws_url` and `book_interval_ms` are configured, but the inspected scanner ingestion path calls REST `FetchOrderBook`; no WebSocket order-book subscription path was found in the inspected entrypoint.
4. Exchange health is updated to `healthy` at scheduling time, not after measuring successful market-data retrieval. A failed polling cycle can therefore still report a healthy exchange.
5. Adapter capabilities are broad exchange-level flags. They do not yet express per-market support for order types, contract semantics, margin/position modes, precision, minimum amount/notional, and settlement asset.

### P2 — Strategy and economics

1. `basis_convergence_fast` is configured as a profile, but the inspected watchdog exit logic is funding-spread reversal or maximum holding time. A genuine basis-convergence strategy needs entry basis, target basis, adverse basis stop, and realized PnL lifecycle.
2. Funding is annualized/normalized from a current snapshot; a production carry decision also needs funding history/forecast uncertainty and the probability of capturing the next settlement.
3. Fee estimates use configured venue taker fees. Account-specific fee tiers and actual maker/taker fills need realized-fee reconciliation.
4. The PRD's monthly return target is not supported by a backtest/live sample in this repository. Replace it with measurable acceptance criteria and report realized net PnL only.

## Changes already applied on this branch

- `round_price`: buy prices round down and sell prices round up to the configured tick.
- Live balance verification: missing client, missing free-USDT data, and fetch exceptions now reject execution (fail closed).
- Unknown fill amount: no longer assumed to equal the requested order quantity.
- Unknown order status: preserved as `unknown`, not silently labeled `closed`.
- Perpetual close: no longer retries without `reduceOnly` if the client rejects that parameter.
- Route unwind now attempts both legs independently, checks close fill quantities, and verifies perpetual positions are flat before returning `UNWOUND_VERIFIED`; otherwise it returns `UNWIND_PENDING` with per-leg diagnostics.
- Watchdog only marks a position closed after an explicit `UNWOUND_VERIFIED` plus `verified_flat=true`; uncertain unwind results keep the position recorded and activate the kill switch.
- Added regression coverage for partial close fills and watchdog retention of unverified positions.
- Added focused regression tests for side-aware price rounding, unverifiable balances, unsafe reduce-only fallback, and unknown fill state.

**Verification caveat:** these commits were written through GitHub's contents API. The Python test suite has not yet been executed in a runtime in this session. CI must pass before merge or live deployment.

## Ordered implementation roadmap

### PR-0 — Executor safety invariants (current priority)
- Poll and reconcile order status until terminal state or bounded timeout; handle partial fills and cancel acknowledgements.
- Persist order IDs, requested/filled/remaining quantities, client order IDs, and per-leg state before/after mutations.
- Verify emergency unwind fills; keep position in `UNWIND_PENDING` / `RECONCILIATION_REQUIRED` when uncertain.
- Make `CLOSED` impossible until both legs are verified flat from exchange positions/open orders.
- Treat missing exchange data and unsupported reduce-only semantics as explicit unsafe states.
- Add fault-injection tests for timeouts, partial fill, cancel race, duplicate response, API outage, and restart recovery.

### PR-A1 — Dynamic market catalogue discovery
- Extend adapter contract with market catalogue enumeration.
- Discover spot and linear perpetual markets from enabled venues; keep allow/block lists as policy filters, not as discovery source.
- Validate quote/settlement asset, trading status, market type, volume, depth, spread, minimum notional, precision, contract multiplier, and execution capability.
- Cache metadata with timestamps and invalidate stale listings.
- Add adapter contract tests and fixture-based parsing tests per venue.

### PR-A2 — Canonical instrument identity and pair registry
- Replace base-symbol-only matching with canonical instrument identity: venue, market type, base, quote, settlement, contract size, multiplier, tick/step, minimums, and position semantics.
- Match economic exposure and quantity conversion, not string equality.
- Keep original entry/exit venue-symbol identity in the position record.
- Reject incompatible settlement or contract semantics before generating signals.

### PR-B1/B2 — Strategy separation and unified economics
- Separate funding carry and basis convergence evaluators/lifecycles.
- Calculate entry and exit fees, funding cash flows, slippage, conversion/financing costs, and expected holding costs.
- Reconcile estimated PnL against exchange-reported realized PnL and fees.
- Use account-specific fee tiers where available.

### PR-C1/C2 — Durable signal pipeline and coordinator
- Replace Pub/Sub-only delivery with Redis Streams consumer groups, acknowledgements, bounded retry, pending recovery, and DLQ.
- Make deduplication atomic and profile-aware; only reserve after durable publication.
- Add idempotent order mutation keys and coordinator fencing/lease renewal.

### PR-D1/D2 — Position state machine and exit engine
- Durable lifecycle: CREATED → ADMITTED → ENTRY_PENDING → HEDGING → OPEN → EXIT_PENDING → CLOSING → RECONCILING → CLOSED, plus explicit recovery states.
- Exit on net realized/unrealized PnL after fees, basis target/stop, funding deterioration, max hold, liquidity/margin/exchange risk.
- Never mark closed based only on an unwind callback response.

### PR-E1/E2 — Capital allocation and market data
- Risk-aware per-venue/per-asset capital allocation and margin buffers.
- WebSocket-first order books with sequence-gap detection, reconnect/resubscribe, freshness gates, and REST snapshot fallback.
- Keep REST polling for funding and metadata with rate-limit budgets.

### PR-F1/F2/F3 — Observability, fault injection, shadow/canary
- Structured metrics for feed freshness, API errors, order lifecycle latency, hedge drift, unresolved residual exposure, estimated-vs-realized PnL.
- Run deterministic integration/fault-injection tests.
- Shadow mode first; canary only after all safety invariants and operational runbooks pass.

## Release gates

- No live rollout while any P0 invariant is unverified.
- CI: all Go tests, Python tests, lint/type checks, and security/code-quality checks green.
- No test should assert a position is closed merely because an order submission returned successfully.
- Canary sizing must be bounded by risk limits and explicitly monitored; never infer profitability from funding-rate snapshots alone.
