# Product Requirements Document (PRD) & Roadmap — `nafaz_arb` v3.1

**Versi Dokumen:** 3.1.0  
**Status:** Siap Review & Implementasi  
**Fokus Arsitektur:** 100% Pasar Global (Linear USDT), *Delta-Neutral* 1:1, *Modular Multi-CEX Scanner* (Golang) & *Executor/Watchdog* (Python)

---

## 1. Ringkasan Eksekutif & Tujuan Produk

`nafaz_arb` v3.1 adalah pembaruan arsitektur sistem arbitrase dan *carry* kripto yang berfokus penuh pada **pasar global berdenominasi USDT** (*linear pairs*), meninggalkan pasar *spot* lokal IDR yang memiliki keterbatasan likuiditas dan friksi konversi kurs.

### Sasaran Utama
1. **Imbal Hasil Terukur & Stabil:** Mengincar *return* bersih realistis di kisaran **4% – 7% per bulan** melalui strategi *delta-neutral* murni 1:1 (*Funding Rate Arbitrage* termasuk *interval mismatch* 1j/4j/8j serta *Spot–Perp Unified Margin*), bukan tebakan arah (*directional*) maupun *reactive partial-hedging*.
2. **Modularitas Lintas Bursa (*Plug-and-Play CEX*):** Memisahkan inti kalkulasi *arbitrage/carry* dari dialek API tiap bursa melalui kontrak `ExchangeAdapter` dan *registry* dinamis, sehingga penambahan CEX baru tidak mengubah logika *engine* utama.
3. **Skalabilitas & Keamanan Operasional:** Menerapkan *staleness gate* ketat per jenis data, pemisahan ritme *polling* untuk melindungi *rate limit*, evaluasi multi-profil dalam satu *tick*, serta deduplikasi sinyal terversi (`schema_version: "3.1"`) di Redis.

---

## 2. Ruang Lingkup (Scope)

### 2.1 Dalam Cakupan (*In-Scope*)
* **Instrumen Pasar:** Pasangan *spot* (`BASE/USDT`) dan kontrak *perpetual* linear (`BASE/USDT:USDT`) pada CEX global yang likuid.
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

## 4. Kebutuhan Fungsional (Functional Requirements)

| ID | Kebutuhan | Kriteria Penerimaan (*Acceptance Criteria*) |
| :--- | :--- | :--- |
| **FR-1** | **Modular CEX Adapter & Registry** | Menambah CEX baru cukup membuat paket adapter (`ExchangeAdapter`), mendaftarkannya di `Registry`, dan mengaktifkannya di file YAML tanpa mengubah paket `engine`. |
| **FR-2** | **Normalisasi Simbol & Interval** | Simbol mentah diterjemahkan ke `BASE/USDT` dan `BASE/USDT:USDT` sambil menyimpan `RawSymbol`; `IntervalHours` dibaca dinamis dan divalidasi $> 0$. |
| **FR-3** | **Auto-Route Matrix Generator** | Kandidat rute dibentuk otomatis dari *snapshot* CEX sehat berdasarkan `Capabilities` (`HasSpot`, `HasPerp`, `HasCollateralInfo`) dan *policy* rute aktif. |
| **FR-4** | **Multi-Profile Evaluation** | Satu proses *scanner* dapat mengevaluasi beberapa profil aturan (misal `funding_carry_core` dan `basis_convergence_fast`) di atas *snapshot* yang sama per *tick*. |
| **FR-5** | **Kontrak Sinyal & Deduplikasi Redis** | Sinyal membawa `schema_version: "3.1"`, `generated_at_ms`, dan `expires_at_ms`; sinyal identik ditahan selama `DedupCooldown` kecuali ada lonjakan `NetDailyYield` $\ge$ `MinYieldDelta`. |
| **FR-6** | **Eksekusi 1:1 & Risk Watchdog** | *Executor* memvalidasi `expires_at_ms`, membuka/menutup dua kaki secara seimbang 1:1, menangani *partial fill*, mematuhi *guardrail* bursa, dan mengirim *alert* Telegram. |

---

## 5. Kebutuhan Non-Fungsional & Keamanan (Non-Functional Requirements)

1. **Ritme Terpisah & Perlindungan *Rate Limit*:**
   * *Orderbook / Mark Price*: *Real-time* WebSocket (atau *fallback* REST `500–1.000 ms`).
   * *Funding Rate & Interval*: *Poll* REST tiap `15–60 detik` (dipercepat menjadi `5–10 detik` pada 2 menit terakhir menjelang jam *settlement*).
   * *Collateral Ratio & Metadata*: *Poll* REST tiap `15–60 menit`.
   * Setiap CEX dilindungi *token-bucket rate limiter* (maksimal 70%–80% dari batas resmi), *exponential backoff + jitter* saat HTTP `429`, serta *circuit breaker* untuk mengisolasi bursa yang bermasalah.
2. **Kesegaran Data (*Staleness Gate*):**
   * *Snapshot* yang melewati batas umur (`MaxBookAge` $\approx 2.500\text{ ms}$, `MaxFundingAge` $\approx 120\text{ s}$, `MaxCollateralAge` $\approx 2\text{ jam}$) otomatis dikecualikan dari evaluasi pada siklus tersebut.
3. **Observabilitas & Audit:**
   * *Structured JSON logging*, metrik kesehatan (*tick latency*, usia data per CEX, jumlah `429`, rasio sinyal lolos vs. alasan penolakan), dan notifikasi Telegram untuk anomali kritis.
4. **Keamanan Dana & API Key:**
   * Seluruh API *key* produksi **wajib menonaktifkan izin *Withdraw*** dan disimpan di luar repositori kode.
   * Diterapkan batas *notional* per posisi, batas eksposur total, *kill-switch* global di Redis, serta *buffer* margin yang memadai di tiap akun.

---

## 6. Struktur Repositori Target (`nafaz_arb` v3.1)

```text
nafaz_arb_v3_1/
├── docs/
│   └── PRD_v3_1.md                 # Dokumen PRD & Roadmap ini
├── configs/
│   ├── scanner.base.yaml           # Konfigurasi engine, freshness, & profil evaluasi
│   └── exchanges.yaml              # Profil CEX, fee, rate-limit budget, & allow/blocklist
├── scanner-go/
│   ├── go.mod
│   ├── cmd/scanner/main.go         # Entrypoint, graceful shutdown, & dependency wiring
│   └── internal/
│       ├── domain/types.go         # Model kanonik USDT & skema sinyal v3.1
│       ├── adapter/
│       │   ├── contract.go         # Interface ExchangeAdapter & Registry
│       │   ├── resilience/         # Rate limiter, backoff+jitter, & circuit breaker
│       │   └── <cex>/              # Adapter terisolasi (okx, bybit, mexc, dll.)
│       ├── engine/
│       │   └── matrix_evaluator.go # Sharded store, staleness gate, auto-route, VWAP, & evaluator
│       └── sink/
│           └── redis_pub.go        # Deduplikasi & publisher Redis (core:* & meta:*)
└── executor-py/
    ├── consumer.py                 # Validasi expires_at_ms & routing sinyal per profile_id
    ├── execution.py                # Eksekusi 1:1 delta-neutral, partial-fill handler, & guardrail
    └── watchdog.py                 # Rekonsiliasi posisi, pemantau margin/ADL, & alert Telegram
```

---

## 7. Roadmap Eksekusi Menuju LIVE Production

### Fase 1 — Fondasi Scanner v3.1 & Kontrak Data
* [ ] Menyusun struktur direktori `nafaz_arb_v3_1` dan menginisialisasi modul Golang (`scanner-go`).
* [ ] Mengimplementasikan `internal/domain/types.go` dengan format simbol kanonik `BASE/USDT` dan `BASE/USDT:USDT`.
* [ ] Membangun *in-memory state store* dengan *staleness gate* (`MaxBookAge`, `MaxFundingAge`, `MaxCollateralAge`) dan *loader* konfigurasi YAML.
* [ ] Mengunci kontrak payload JSON sinyal v3.1 (`schema_version`, `signal_id`, `dedup_key`, `expires_at_ms`) serta *namespace* Redis (`core:*` & `meta:*`).

### Fase 2 — Adapter CEX Modular & *Resilience*
* [ ] Mengimplementasikan paket `resilience` (*token-bucket limiter* per CEX, *exponential backoff + jitter*, dan *circuit breaker*).
* [ ] Membangun kontrak `ExchangeAdapter` & `Registry` dinamis.
* [ ] Mengimplementasikan adapter CEX global prioritas (OKX, Bybit, MEXC) dengan ritme terpisah antara *orderbook*, *funding rate/interval*, dan *collateral ratio*.
* [ ] Menulis *contract test* otomatis untuk memverifikasi *mapping* simbol `Raw <-> Canon`, validitas `IntervalHours > 0`, dan perilaku saat simulasi `HTTP 429`.

### Fase 3 — *Auto-Route Matrix*, *Multi-Profile Evaluator*, & Observabilitas
* [ ] Mengaktifkan *auto-route generator* (`spot_perp_same_cex` dan `perp_perp_cross_cex`) serta kalkulator *VWAP slippage* dan *net funding 24h*.
* [ ] Menjalankan multi-profil evaluasi (`funding_carry_core` dan `basis_convergence_fast`) dalam satu *tick* dengan berbagi hasil hitung VWAP.
* [ ] Mengaktifkan deduplicator & *publisher* Redis beserta metrik audit alasan penolakan sinyal (*stale*, *slippage*, *breakeven*, *collateral*).

### Fase 4 — Integrasi Executor 1:1 & *Risk Watchdog* (Python)
* [ ] Membangun *consumer* sinyal v3.1 di Python yang memvalidasi `expires_at_ms` sebelum mengeksekusi order.
* [ ] Mengimplementasikan eksekusi *delta-neutral* 1:1, penyeimbangan otomatis saat terjadi *partial fill*, dan *guardrail* khusus tiap CEX.
* [ ] Mengaktifkan *watchdog* rekonsiliasi saldo/posisi aktual vs. Redis, pemantauan *margin ratio* / *ADL*, *kill-switch*, dan notifikasi Telegram.

### Fase 5 — *Dry-Run*, *Canary Live*, & *Scale-Up* Bertahap di VPS
* [ ] Menjalankan *pipeline* lengkap dalam mode **Dry-Run / Shadow** di VPS selama minimal 3–5 hari untuk memvalidasi kualitas sinyal, kestabilan koneksi 24/7, dan kepatuhan *rate limit*.
* [ ] Melakukan uji **Canary Live** dengan ukuran *notional* kecil pada rute prioritas untuk memverifikasi akurasi *slippage* aktual, pencatatan *funding* masuk, dan prosedur *close/unwind*.
* [ ] Melakukan evaluasi hasil bersih setelah biaya transaksi dan meningkatkan alokasi modal secara bertahap dengan tetap menjaga *buffer* margin dan dana darurat terpisah di luar bursa.