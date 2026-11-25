# Product Requirements Document (PRD) & Roadmap — `nafaz_arb` v3.1

**Versi Dokumen:** 3.1.0  
**Status:** Baseline requirements; implementation gaps tracked in [Implementation Gap Analysis](./IMPLEMENTATION_GAP_ANALYSIS_2026-11-24.md)  
**Fokus Arsitektur:** 100% Pasar Global (Linear USDT), *Delta-Neutral* 1:1, *Modular Multi-CEX Scanner* (Golang) & *Executor/Watchdog* (Python)

---

## 1. Ringkasan Eksekutif & Tujuan Produk

`nafaz_arb` v3.1 adalah pembaruan arsitektur sistem arbitrase dan *carry* kripto yang berfokus penuh pada **pasar global berdenominasi USDT** (*linear pairs*), meninggalkan pasar *spot* lokal IDR yang memiliki keterbatasan likuiditas dan friksi konversi kurs.

### Sasaran Utama
1. **Profitabilitas Bersih yang Terukur:** Mengoptimalkan expected net PnL yang telah memperhitungkan fee, slippage, funding, financing, dan risiko eksekusi. Tidak ada target persentase bulanan yang dianggap terjamin; kelayakan strategi harus dibuktikan melalui data historis, shadow mode, dan realized PnL setelah biaya.
2. **Modularitas Lintas Bursa (*Plug-and-Play CEX*):** Memisahkan inti kalkulasi *arbitrage/carry* dari dialek API tiap bursa melalui kontrak `ExchangeAdapter` dan *registry* dinamis, sehingga penambahan CEX baru tidak mengubah logika *engine* utama.
3. **Skalabilitas & Keamanan Operasional:** Menerapkan *staleness gate* ketat per jenis data, pemisahan ritme *polling* untuk melindungi *rate limit*, evaluasi multi-profil dalam satu *tick*, serta deduplikasi sinyal terversi (`schema_version: "3.1"`) di Redis.

---

## 2. Ruang Lingkup (Scope)

### 2.1 Dalam Cakupan (*In-Scope*)
* **Instrumen Pasar:** Katalog market spot (`BASE/USDT`) dan kontrak perpetual linear (`BASE/USDT:USDT`) pada CEX global yang likuid. Discovery berasal dari katalog market exchange; allow/block list hanya menjadi filter kebijakan.
* **Tipe Rute Otomatis (*Auto-Generated Routes*):**
  * `spot_perp_same_cex`: *Buy Spot* + *Short Perp* 1:1 dalam satu akun *Unified Margin* (dengan validasi *collateral ratio* minimum).
  * `perp_perp_cross_cex`: *Long Perp* di CEX A + *Short Perp* di CEX B (termasuk pemanfaatan *funding interval mismatch* 1 jam, 4 jam, dan 8 jam).
  * `spot_perp_cross_cex`: *Buy Spot* di CEX A + *Short Perp* di CEX B (opsional, dinonaktifkan secara *default* dan dikendalikan via konfigurasi).
* **Komponen Sistem:**
  * `scanner` (Golang): *Ingestion* data pasar, normalisasi simbol & *funding*, kalkulasi *VWAP slippage*, *auto-route matrix*, *multi-profile evaluator*, dan *Redis publisher/deduplicator*.
  * `redis` (State & Event Bus): Distribusi sinyal (`core:*`), penyimpanan *metadata* bursa/kolateral (`meta:*`), serta *lock/cooldown* risiko.
  * `executor` & `watchdog` (Python `ccxt`): Validasi pra-order, eksekusi dua kaki 1:1, penanganan *partial fill*, kepatuhan *guardrail* bursa, pemantauan *margin/ADL*, dan notifikasi Telegram.

### 2.2 Di Luar Cakupan (*Out-of-Scope*)
* Konektor bursa *spot* lokal IDR, *oracle* kurs konversi IDR, dan logika *spread* IDR–USDT.
* Pembuatan rute manual (*hardcoded*) per pasangan bursa-koin.
* Strategi *directional* spekulatif atau *reactive hedging* setelah posisi terlanjur mengalami *floating loss*.

---

## 3. Spesifikasi Model Matematika & Filter Kelayakan

### 3.1 Normalisasi *Funding Rate* ke Basis 24 Jam
Seluruh *funding rate* mentah $r_i$ (desimal per interval) dikonversi ke arus kas harian 24 jam berdasarkan interval *settlement* aktual $I_i$ (dalam jam, misal `1`, `4`, atau `8`):

$$\text{Gross Funding 24h} = \left(-r_{\text{long}} \times \frac{24}{I_{\text{long}}}\right) + \left(+r_{\text{short}} \times \frac{24}{I_{\text{short}}}\right)$$

*(Untuk kaki Spot pada rute `spot_perp_same_cex`, arus kas funding bernilai $0$).*

### 3.2 Kalkulasi *VWAP Slippage* & Biaya *Round-Trip*
* **VWAP Orderbook:** Harga rata-rata tertimbang volume dihitung dari kedalaman *orderbook* aktual (`asks` untuk *buy*, `bids` untuk *sell*) sesuai ukuran `notional_usdt` target, bukan sekadar *top-of-book*.
* **Total Friksi *Round-Trip* ($C_{\text{rt}}$):**

$$C_{\text{rt}} = C_{\text{fee,rt}} + C_{\text{slip,rt}}$$

Di mana $C_{\text{fee,rt}}$ mencakup biaya buka dan tutup pada kedua kaki, serta $C_{\text{slip,rt}} \approx 2 \times (\text{slip}_{\text{legA}} + \text{slip}_{\text{legB}})$.

### 3.3 *Net Daily Yield* & *Breakeven Days*
Biaya *round-trip* diamortisasi sepanjang durasi tahan rencana $D_{\text{hold}}$ (`target_hold_days`):

$$\text{Net Daily Yield} = \text{Gross Funding 24h} - \frac{C_{\text{rt}}}{D_{\text{hold}}}$$

$$\text{Breakeven Days} = \frac{C_{\text{rt}}}{\max(\text{Gross Funding 24h},\; \epsilon)}$$

Sinyal dinyatakan layak terbit apabila memenuhi seluruh kriteria profil aktif:
1. $\text{Gross Funding 24h} > 0$
2. $\text{Breakeven Days} \le D_{\text{max\_breakeven}}$
3. $\text{Net Daily Yield} \ge \text{Min\_Net\_Daily\_Yield}$
4. Likuiditas volume 24 jam, kedalaman *orderbook*, *basis spread* awal, dan *collateral ratio* (untuk `spot_perp_same_cex`) memenuhi ambang batas konfigurasi.

---

## 4. Spesifikasi Automatic Market Discovery, Symbol Normalization, & Cross-CEX Matching

### 4.1 Automatic Market Discovery Specification
Scanner **wajib** menemukan daftar market aktif secara otomatis langsung dari API publik resmi masing-masing CEX tanpa bergantung pada daftar pair manual (`symbol_allowlist`).

1. **Cakupan Market Discovery:**
   * Seluruh pasangan **Spot USDT** yang memenuhi kriteria aktif/trading.
   * Seluruh pasangan **USDT Linear Perpetual** yang memenuhi kriteria aktif/trading dan *settled in USDT*.
2. **Atribut `MarketDescriptor` Wajib:**
   * `Exchange`: Nama CEX (`okx`, `bybit`, `mexc`, dll.).
   * `RawSymbol`: Simbol asli bursa (`BTC-USDT`, `BTC-USDT-SWAP`, `BTCUSDT`, `BTC_USDT`).
   * `CanonSymbol`: Simbol kanonik hasil normalisasi (`BTC/USDT` atau `BTC/USDT:USDT`).
   * `BaseAsset`: Aset dasar uppercase (`BTC`, `ETH`, `SOL`).
   * `QuoteAsset`: Aset kuotasi uppercase (`USDT`).
   * `MarketType`: Tipe instrumen (`spot` atau `linear_perp`).
   * `Active`: Status ketersediaan trading (`true` hanya jika market berstatus live/trading aktif).
   * `ContractMultiplier`: Nilai pengali kontrak (`ctVal` / `contractSize`, default `1.0` untuk Spot).
   * `DiscoveredAtMs`: Timestamp discovery terakhir (epoch milliseconds).
3. **Endpoint Resmi Discovery per CEX:**
   * **OKX:**
     * Spot: `GET /api/v5/public/instruments?instType=SPOT` (filter `state == "live"`, `quoteCcy == "USDT"`).
     * Linear Perpetual: `GET /api/v5/public/instruments?instType=SWAP` (filter `state == "live"`, `ctType == "linear"`, `settleCcy == "USDT"`). Menyimpan `ctVal` ke cache multiplier adapter.
   * **Bybit:**
     * Spot: `GET /v5/market/instruments-info?category=spot` (filter `status == "Trading"`, `quoteCoin == "USDT"`).
     * Linear Perpetual: `GET /v5/market/instruments-info?category=linear` (filter `status == "Trading"`, `quoteCoin == "USDT"`, `settleCoin == "USDT"`, `contractType == "LinearPerpetual"`).
   * **MEXC:**
     * Spot: `GET /api/v3/exchangeInfo` (filter `quoteAsset == "USDT"`, `status == "1"` atau `"ENABLED"`, `isSpotTradingAllowed == true`).
     * Linear Perpetual: `GET /api/v1/contract/detail` (filter `state == 0`, `quoteCoin == "USDT"`, `settleCoin == "USDT"`). Menyimpan `contractSize` ke cache multiplier adapter.
4. **Continuous Discovery & Fault Isolation:**
   * Discovery berjalan otomatis saat startup scanner dan diperbarui secara berkala setiap `refresh_interval_sec` (default `300` detik).
   * Pair listing baru yang cocok antar-CEX otomatis masuk ke active polling universe tanpa restart manual.
   * Pair yang delisting atau tidak lagi berstatus aktif otomatis dihentikan polling-nya, dihapus dari active registry, dan dibersihkan dari `StateStore`.
   * Kegagalan discovery pada satu exchange (timeout, HTTP 429, 5xx, maintenance) dicatat pada log dan metrik kesehatan tanpa menghentikan proses scanner atau discovery exchange lain; registry mempertahankan snapshot terakhir yang valid atau melanjutkan dengan bursa sehat lainnya.

---

### 4.2 Automatic Symbol Normalization Specification
Seluruh CEX dinormalisasi ke dalam satu **Canonical Symbol System** berbasis tuple terstruktur `(BaseAsset, QuoteAsset, MarketType)`:

| Format Kanonik | Market Type (`domain.MarketType`) | Contoh Kanonik | Contoh Raw OKX | Contoh Raw Bybit | Contoh Raw MEXC |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `BASE/USDT` | `spot` | `BTC/USDT` | `BTC-USDT` | `BTCUSDT` | `BTCUSDT` |
| `BASE/USDT:USDT` | `linear_perp` | `BTC/USDT:USDT` | `BTC-USDT-SWAP` | `BTCUSDT` | `BTC_USDT` |

**Aturan Ketat Normalisasi:**
1. **Berbasis Struktur (`BaseAsset`, `QuoteAsset`, `MarketType`):** Normalisasi memvalidasi aset dasar, aset kuotasi (`USDT`), dan jenis pasar (`spot` vs `linear_perp`), bukan sekadar kemiripan substring teks.
2. **Isolasi Ketat Spot vs Perpetual:** Instrumen `spot` (`BASE/USDT`) dan `linear_perp` (`BASE/USDT:USDT`) **tidak pernah** disamakan meskipun memiliki `BaseAsset` dan `QuoteAsset` yang sama. Simbol mentah Spot (`BTC-USDT` di OKX atau `BTCUSDT` di MEXC) ditolak jika dinormalisasi sebagai `linear_perp`, dan sebaliknya simbol mentah Perpetual (`BTC-USDT-SWAP` di OKX atau `BTC_USDT` di MEXC) ditolak jika dinormalisasi sebagai `spot`.

---

### 4.3 Automatic Cross-CEX Matching Specification
Matching engine di `MarketRegistry` secara otomatis menghitung irisan (*intersection*) market aktif yang tersedia pada dua atau lebih CEX (atau pasangan Spot+Perp pada CEX yang sama untuk Unified Margin).

#### Contoh Matriks Ketersediaan & Hasil Matching
Misalkan hasil discovery `linear_perp` (`BASE/USDT:USDT`) pada tiga CEX adalah:

| Asset | OKX (`linear_perp`) | Bybit (`linear_perp`) | MEXC (`linear_perp`) | Status Cross-CEX Matching (`perp_perp_cross_cex`) |
| :--- | :--- | :--- | :--- | :--- |
| **BTC** | Available | Available | Available | **Matched (3 CEX):** Rute 2 arah antara OKX↔Bybit, OKX↔MEXC, Bybit↔MEXC (6 arah rute). |
| **ETH** | Available | Available | Available | **Matched (3 CEX):** Rute 2 arah antara OKX↔Bybit, OKX↔MEXC, Bybit↔MEXC (6 arah rute). |
| **SOL** | Available | Available | Available | **Matched (3 CEX):** Rute 2 arah antara OKX↔Bybit, OKX↔MEXC, Bybit↔MEXC (6 arah rute). |
| **ABC** | Available | Unavailable | Available | **Matched (2 CEX: OKX & MEXC):** Hanya diproses antara OKX→MEXC dan MEXC→OKX. **Tidak pernah** dipaksakan membentuk rute yang melibatkan Bybit. |
| **XYZ** | Unavailable | Available | Available | **Matched (2 CEX: Bybit & MEXC):** Hanya diproses antara Bybit→MEXC dan MEXC→Bybit. **Tidak pernah** dipaksakan membentuk rute yang melibatkan OKX. |
| **ONLY1** | Available | Unavailable | Unavailable | **Unmatched (1 CEX):** Tidak menghasilkan rute cross-CEX dan tidak dipolling untuk strategi cross-CEX. |

#### Aturan Matching per Strategi Engine V3.1
Matching dilakukan terpisah sesuai kebijakan `route_policies` yang aktif:
1. **`perp_perp_cross_cex` (Perpetual vs Perpetual Lintas CEX):**
   * Syarat: `BASE/USDT:USDT` aktif pada $\ge 2$ CEX sehat yang memiliki `Capabilities.HasPerp == true`.
   * Menghasilkan kandidat rute dua arah (`CEX_A -> CEX_B` dan `CEX_B -> CEX_A`) hanya di antara CEX yang benar-benar menyediakan market tersebut.
2. **`spot_perp_same_cex` (Spot vs Perpetual Satu CEX Unified Margin):**
   * Syarat: `BASE/USDT` (Spot) **dan** `BASE/USDT:USDT` (Linear Perp) keduanya aktif pada CEX yang sama dengan `HasSpot && HasPerp && UnifiedMargin`.
3. **`spot_perp_cross_cex` (Spot CEX A vs Perpetual CEX B — Opsional):**
   * Syarat: `enable_spot_perp_cross_cex: true`, `BASE/USDT` aktif di CEX A (`HasSpot`), dan `BASE/USDT:USDT` aktif di CEX B (`HasPerp`, `A != B`).

---

### 4.4 Arsitektur Sistem & Alur Data (*End-to-End Data Flow*)

```text
[Exchange Public REST APIs (OKX / Bybit / MEXC)]
       │
       ▼ (1. Startup & Periodic Refresh tiap refresh_interval_sec)
[Market Discovery (Adapter.FetchMarkets)]
       │
       ▼ (2. Filter active/trading, quote_assets=[USDT], markets=[spot, linear_perp], blocklist, optional allowlist)
[Market Validation & Symbol Normalization]
       │  (RawSymbol -> MarketDescriptor{BaseAsset, QuoteAsset, MarketType, CanonSymbol})
       ▼ (3. Upsert ke Dynamic Market Registry & Evict Delisted Markets)
[Cross-CEX Matching Engine (MarketRegistry)]
       │  (Hitung irisan Perp-Perp >=2 CEX, Spot-Perp Same-CEX, & Spot-Perp Cross-CEX)
       ▼ (4. Hasilkan Active Polling Universe per CEX — hanya market yang memiliki pasangan valid)
[Dynamic Polling Scheduler (Bounded Worker Pool + Token-Bucket Rate Limiter)]
       │  (Deduplicated per Exchange+CanonSymbol; Orderbook 750ms, Funding 30s / Fast 5s, Collateral 1800s)
       ▼ (5. Simpan snapshot segar & bersihkan snapshot market yang delisted)
[In-Memory StateStore (OrderBooks, Fundings, Collaterals)]
       │
       ▼ (6. Evaluasi per Tick: Staleness Gate -> Auto-Route Matrix -> Shared VWAP -> Multi-Profile Filter)
[MatrixEvaluator (funding_carry_core & basis_convergence_fast)]
       │
       ▼ (7. Cooldown Deduplication + MinYieldDelta Spike Override)
[SignalPublisher -> Redis (core:signals, core:dedup:*, meta:exchange:*, meta:rejection_stats)]
```

#### Contoh Transformasi Data End-to-End
1. **Discovery Payload (OKX SWAP):** `{"instId":"BTC-USDT-SWAP","ctType":"linear","state":"live","settleCcy":"USDT","ctVal":"0.01"}`
2. **Normalized `MarketDescriptor`:**
   ```json
   {
     "exchange": "okx",
     "raw_symbol": "BTC-USDT-SWAP",
     "canon_symbol": "BTC/USDT:USDT",
     "base_asset": "BTC",
     "quote_asset": "USDT",
     "market_type": "linear_perp",
     "active": true,
     "contract_multiplier": 0.01
   }
   ```
3. **Cross-CEX Match Result for `BTC`:**
   * `perp_perp_cross_cex`: `BTC/USDT:USDT` matched on `["bybit", "mexc", "okx"]`.
   * `spot_perp_same_cex`: `BTC/USDT` + `BTC/USDT:USDT` matched on `["bybit", "okx"]`.
4. **Polling Target Set:**
   * `okx`: `["BTC/USDT", "BTC/USDT:USDT"]`
   * `bybit`: `["BTC/USDT", "BTC/USDT:USDT"]`
   * `mexc`: `["BTC/USDT:USDT"]` (Spot `BTC/USDT` pada MEXC tidak dipolling kecuali `enable_spot_perp_cross_cex: true` karena MEXC `unified_margin: false`).

---

### 4.5 Referensi Konfigurasi Scanner (`configs/exchanges.yaml`)

Operator tidak lagi diwajibkan menulis `symbol_allowlist`. Konfigurasi menggunakan blok `market_discovery`:

```yaml
exchanges:
  okx:
    enabled: true
    base_url: "https://www.okx.com"
    ws_url: "wss://ws.okx.com:8443/ws/v5/public"
    spot_taker_fee: 0.0008
    perp_taker_fee: 0.0005
    rate_limit_rps: 15
    max_retries: 3
    circuit_breaker_threshold: 5
    unified_margin: true
    market_discovery:
      enabled: true
      markets:
        - spot
        - linear_perpetual
      quote_assets:
        - USDT
      refresh_interval_sec: 300
      blocklist: []
    polling:
      book_interval_ms: 750
      funding_interval_sec: 30
      funding_fast_interval_sec: 5
      collateral_interval_sec: 1800
      max_concurrent_polls: 8
```

* `market_discovery.enabled`: Mengaktifkan automatic discovery dari API CEX (default `true`).
* `market_discovery.markets`: Filter tipe market yang diambil (`spot`, `linear_perpetual` / `linear_perp`).
* `market_discovery.quote_assets`: Filter aset kuotasi/settlement (`USDT`).
* `market_discovery.refresh_interval_sec`: Interval pembaruan katalog market berkala (detik).
* `market_discovery.blocklist`: Daftar base asset atau simbol kanonik yang dikecualikan dari discovery.
* `market_discovery.allowlist` / `symbol_allowlist`: Opsional; jika dikosongkan (`[]`), seluruh market aktif hasil discovery digunakan. Jika diisi, hanya berfungsi sebagai filter pembatas opsional.

---

## 5. Kebutuhan Fungsional (Functional Requirements)

| ID | Kebutuhan | Kriteria Penerimaan (*Acceptance Criteria*) |
| :--- | :--- | :--- |
| **FR-1** | **Modular CEX Adapter & Automatic Market Discovery** | Setiap adapter mengimplementasikan `FetchMarkets(ctx)` dari endpoint resmi CEX; scanner menemukan pair aktif secara otomatis tanpa memerlukan `symbol_allowlist` manual. |
| **FR-2** | **Normalisasi Simbol Kanonik & Interval** | Simbol mentah dinormalisasi berdasarkan `(BaseAsset, QuoteAsset, MarketType)` ke `BASE/USDT` (Spot) dan `BASE/USDT:USDT` (Linear Perp) tanpa mencampur Spot dan Perp; `IntervalHours` divalidasi $> 0$. |
| **FR-3** | **Dynamic Market Registry & Cross-CEX Matching** | `MarketRegistry` menyimpan katalog market aktif per CEX, mencocokkan irisan pair antar-CEX ($\ge 2$ CEX untuk cross-CEX), memfilter pair tunggal, serta menghapus pair delisting secara dinamis. |
| **FR-4** | **Dynamic Bounded Polling** | Polling hanya memproses market yang lolos Cross-CEX/Same-CEX matching dengan batas konkurensi, rate limiting, deduplikasi task, dan isolasi error per CEX. |
| **FR-5** | **Auto-Route Matrix & Multi-Profile Evaluation** | Kandidat rute दो arah (`CEX_A -> CEX_B` dan `CEX_B -> CEX_A`) dievaluasi di atas `MatrixEvaluator` V3.1 dengan pembagian cache VWAP untuk `funding_carry_core` dan `basis_convergence_fast`. |
| **FR-6** | **Kontrak Sinyal & Deduplikasi Redis** | Sinyal membawa `schema_version: "3.1"`, `generated_at_ms`, dan `expires_at_ms`; sinyal identik ditahan selama `DedupCooldown` kecuali ada lonjakan `NetDailyYield` $\ge$ `MinYieldDelta`. |
| **FR-7** | **Eksekusi 1:1 & Risk Watchdog** | *Executor* memvalidasi `expires_at_ms`, melacak order/fill/cancel per kaki, menangani *partial fill*, dan hanya menyatakan posisi `CLOSED` setelah kedua kaki diverifikasi flat di exchange. |

---

## 6. Kebutuhan Non-Fungsional & Keamanan (Non-Functional Requirements)

1. **Ritme Terpisah & Perlindungan *Rate Limit*:**
   * *Market Discovery*: Startup + *periodic refresh* REST tiap `refresh_interval_sec` (`300 detik`).
   * *Orderbook / Mark Price*: *Real-time* WebSocket (atau *fallback* REST `500–1.000 ms`) hanya pada *Active Polling Universe* hasil Cross-CEX Matching.
   * *Funding Rate & Interval*: *Poll* REST tiap `15–60 detik` (dipercepat menjadi `5–10 detik` pada 2 menit terakhir menjelang jam *settlement*).
   * *Collateral Ratio & Metadata*: *Poll* REST tiap `15–60 menit`.
   * Setiap CEX dilindungi *token-bucket rate limiter* (maksimal 70%–80% dari batas resmi), *exponential backoff + jitter* saat HTTP `429`, serta *circuit breaker* untuk mengisolasi bursa yang bermasalah.
2. **Kesegaran Data (*Staleness Gate*):**
   * *Snapshot* yang melewati batas umur (`MaxBookAge` $\approx 2.500\text{ ms}$, `MaxFundingAge` $\approx 120\text{ s}$, `MaxCollateralAge` $\approx 2\text{ jam}$) otomatis dikecualikan dari evaluasi pada siklus tersebut.
3. **Observabilitas & Audit:**
   * *Structured JSON logging*, metrik kesehatan (*tick latency*, usia data per CEX, jumlah `429`, jumlah market aktif hasil matching, rasio sinyal lolos vs. alasan penolakan), dan notifikasi Telegram untuk anomali kritis.
4. **Keamanan Dana & API Key:**
   * Seluruh API *key* produksi **wajib menonaktifkan izin *Withdraw*** dan disimpan di luar repositori kode.
   * Diterapkan batas *notional* per posisi, batas eksposur total, *kill-switch* global di Redis, serta *buffer* margin yang memadai di tiap akun.

---

## 7. Struktur Repositori Target (`nafaz_arb` v3.1)

```text
nafaz_arb_v3_1/
├── docs/
│   ├── PRD_v3_1.md                 # Dokumen PRD & Spesifikasi Arsitektur v3.1
│   └── IMPLEMENTATION_GAP_ANALYSIS_2026-11-24.md
├── configs/
│   ├── scanner.base.yaml           # Konfigurasi engine, freshness, & profil evaluasi
│   └── exchanges.yaml              # Profil CEX, fee, rate-limit budget, & market_discovery
├── scanner-go/
│   ├── go.mod
│   ├── cmd/scanner/main.go         # Entrypoint, continuous discovery, dynamic polling, & tick loop
│   └── internal/
│       ├── domain/types.go         # Model kanonik USDT, MarketDescriptor, & skema sinyal v3.1
│       ├── adapter/
│       │   ├── contract.go         # Interface ExchangeAdapter (termasuk FetchMarkets) & Registry
│       │   ├── resilience/         # Rate limiter, backoff+jitter, & circuit breaker
│       │   └── <cex>/              # Adapter terisolasi (okx, bybit, mexc, dll.)
│       ├── engine/
│       │   ├── market_registry.go  # Dynamic Market Registry & Cross-CEX Matching Engine
│       │   ├── matrix_evaluator.go # StateStore, staleness gate, VWAP, & multi-profile evaluator
│       │   └── route_builder.go    # Auto-Route Matrix generator
│       └── sink/
│           └── redis_pub.go        # Deduplikasi & publisher Redis (core:* & meta:*)
└── executor-py/
    ├── consumer.py                 # Validasi expires_at_ms & routing sinyal per profile_id
    ├── execution.py                # Eksekusi 1:1 delta-neutral, partial-fill handler, & guardrail
    └── watchdog.py                 # Rekonsiliasi posisi, pemantau margin/ADL, & alert Telegram
```

---

## 8. Pengujian & Kriteria Penerimaan (*Testing & Acceptance Criteria*)

Seluruh implementasi scanner wajib diverifikasi dengan automated test menggunakan mock exchange API yang mencakup 14 kriteria penerimaan berikut:
1. **Automatic Discovery per Adapter:** `FetchMarkets` mengambil market aktif dari OKX, Bybit, dan MEXC tanpa daftar pair manual.
2. **Normalisasi Simbol Lintas Format:** Format `BTCUSDT`, `BTC-USDT`, `BTC-USDT-SWAP`, `BTC_USDT` dinormalisasi secara deterministik ke `BASE/USDT` atau `BASE/USDT:USDT`.
3. **Pemisahan Ketat Spot vs Perpetual:** Simbol Spot tidak pernah dinormalisasi atau dicocokkan sebagai Perpetual, dan sebaliknya.
4. **Cross-CEX Pair Matching ($\ge 2$ CEX):** Pair yang tersedia pada $\ge 2$ CEX berhasil masuk ke *matched universe*.
5. **Isolasi Pair Tunggal (Single-CEX Filtering):** Pair yang hanya tersedia di 1 CEX tidak menghasilkan rute cross-CEX dan tidak membebani polling cross-CEX.
6. **Evaluasi Dua Arah (*Bidirectional Routes*):** Pair yang cocok pada CEX A dan CEX B dievaluasi untuk kedua arah (`CEX_A -> CEX_B` dan `CEX_B -> CEX_A`).
7. **Penambahan Market Baru Secara Dinamis:** Listing baru pada siklus discovery berikutnya otomatis masuk ke registry dan dipolling tanpa restart.
8. **Penghapusan Market Delisted (*Delisting Eviction*):** Market yang delisted/nonaktif dihapus dari registry, dihentikan polling-nya, dan dibersihkan dari `StateStore`.
9. **Dukungan Blocklist & Optional Allowlist:** Aset pada `blocklist` dikecualikan; `allowlist` kosong berarti semua market aktif, atau menjadi filter opsional jika diisi.
10. **Isolasi Kegagalan API (*Fault Isolation*):** Kegagalan HTTP/timeout pada satu CEX saat discovery atau polling tidak menghentikan scanner.
11. **Kepatuhan Rate Limiting & Resilience:** HTTP 429 ditangani oleh rate limiter, retry backoff, dan circuit breaker.
12. **Bebas Duplikasi Task Polling:** Dynamic polling menduplikasi target per `(Exchange, CanonSymbol)` tanpa goroutine ganda untuk market yang sama.
13. **Konsistensi `MatrixEvaluator` V3.1:** Rumus VWAP, gross funding 24h, round-trip cost, breakeven days, net daily yield, staleness gate, dan collateral gate tetap konsisten.
14. **Kompatibilitas Dry-Run Mode:** End-to-end pipeline dalam mode `--dry-run` tetap berjalan penuh tanpa ketergantungan Redis eksternal.

---

## 9. Status Implementasi & Peta Jalan (*Implementation Status Matrix*)

Untuk menjaga akurasi dokumentasi terhadap source code dan pengujian:

| Komponen / Fitur | Status Saat Ini | Bukti Verifikasi / Catatan |
| :--- | :--- | :--- |
| **Model Matematika V3.1 (`ComputeVWAP`, `NormalizeGrossFunding24h`, `ComputeMetrics`)** | **Implemented** | `scanner-go/internal/engine/matrix_evaluator.go` & `matrix_evaluator_test.go` |
| **Staleness Gate, Collateral Gate, & Multi-Profile VWAP Cache Sharing** | **Implemented** | `scanner-go/internal/engine/route_builder.go` & `matrix_evaluator_test.go` |
| **Resilience Layer (Token-Bucket Rate Limiter, Backoff+Jitter, Circuit Breaker)** | **Implemented** | `scanner-go/internal/adapter/resilience/resilience.go` & `resilience_test.go` |
| **Signal Deduplication & Redis Publisher (`core:signals`, `meta:*`)** | **Implemented** | `scanner-go/internal/sink/redis_pub.go` & `redis_pub_test.go` |
| **Python Executor 1:1 Fail-Closed Balance, Verified Unwind & Risk Watchdog** | **Implemented** | `executor-py/execution.py`, `watchdog.py`, `test_executor_safety.py` |
| **Automatic Market Discovery (`FetchMarkets` pada OKX, Bybit, MEXC)** | **Implemented** | `scanner-go/internal/adapter/*/discovery.go` & `okx_test.go`, `bybit_test.go`, `mexc_test.go` |
| **Dynamic Market Registry & Automatic Cross-CEX Matching Engine** | **Implemented** | `scanner-go/internal/engine/market_registry.go` & `market_registry_test.go` |
| **Dynamic Bounded Polling, Continuous Refresh & Delisting Cleanup** | **Implemented** | `scanner-go/cmd/scanner/main.go`, `matrix_evaluator.go`, & `main_test.go` |
| **WebSocket Streaming Orderbook Ingestion & Redis Streams Durable Bus** | **Planned (Future Roadmap)** | Saat ini menggunakan REST polling terproteksi rate-limiter dan Redis Pub/Sub + KV dedup. |

### Release Safety Gates
Production release tetap mensyaratkan:
- live balance dan order-state checks fail closed;
- partial fills, cancel races, dan emergency unwinds direkonsiliasi terhadap state exchange;
- posisi tidak boleh ditandai `CLOSED` sebelum kedua kaki terverifikasi flat;
- seluruh unit test & integration test Go dan Python lulus (`go test ./...` & `pytest`).
