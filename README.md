# `nafaz_arb` v3.1 — Panduan Mekanisme Profit & Ekstensi CEX Baru

Dokumen ini menjelaskan secara lengkap:
1. **Bagaimana sistem `nafaz_arb` v3.1 mencetak profit** (*Delta-Neutral Funding Rate Arbitrage*) beserta rumus matematika, filter pengaman, dan siklus hidup posisi.
2. **Panduan langkah demi langkah menambahkan bursa (CEX) baru** ke Go Scanner dan Python Executor.

---

## Bagian 1: Bagaimana Project Ini Menghasilkan Profit?

### 1.1 Konsep Dasar: *Delta-Neutral Funding Rate Arbitrage*

Di pasar *crypto perpetual futures* (kontrak berjangka tanpa tanggal kadaluarsa), bursa menggunakan mekanisme **Funding Rate** setiap beberapa jam (umumnya tiap **8 jam**, **4 jam**, atau **1 jam**) agar harga kontrak perpetual tetap dekat dengan harga Spot:
- Saat pasar *bullish* (**Funding Rate Positif**): Trader posisi **Long membayar** biaya funding kepada trader posisi **Short**.
- Saat pasar *bearish* (**Funding Rate Negatif**): Trader posisi **Short membayar** kepada posisi **Long**.

`nafaz_arb` v3.1 **tidak menebak arah harga naik atau turun**. Sistem membuka **dua posisi berlawanan dengan jumlah koin yang persis sama (1:1)** secara bersamaan:
- **Kaki A (Leg A — Long / Buy)**: Membeli aset di pasar Spot (`BUY_SPOT`) atau membuka Long Perpetual (`BUY_LONG`) di bursa dengan funding rendah/negatif.
- **Kaki B (Leg B — Short / Sell)**: Membuka Short Perpetual (`SELL_SHORT`) dengan jumlah koin identik di bursa dengan funding tinggi positif.

Karena kuantitas koin `Leg A == Leg B` (**Delta = 0**):
- Jika harga BTC naik $+\$1,000$: Keuntungan di Kaki A ($+\$1,000$) menutup kerugian di Kaki B ($-\$1,000$). Nilai modal bersih Anda tetap utuh.
- Jika harga BTC turun $-\$1,000$: Kerugian di Kaki A ($-\$1,000$) ditutup oleh keuntungan di Kaki B ($+\$1,000$).
- **Sumber Profit Murni**: Selama posisi ditahan melewati jam pembayaran funding, Anda terus memanen selisih pembayaran *Funding Rate* bersih setiap beberapa jam.

---

### 1.2 Tiga Rute Arbitrase Otomatis (*Auto-Route Matrix*)

Go Scanner (`scanner-go/internal/engine/route_builder.go`) secara otomatis mengevaluasi semua kombinasi bursa yang aktif (untuk $N$ bursa aktif pada 1 koin, scanner mengevaluasi **$N(N-1)$ pasangan arah Futures Long–Short** ditambah **$N$ rute Spot–Short** di setiap *tick*):

| Tipe Rute | Kaki A (Long) | Kaki B (Short) | Keunggulan & Syarat |
| :--- | :--- | :--- | :--- |
| **`perp_perp_cross_cex`** *(Futures Long – Futures Short)* | `BUY_LONG` Perp (`BASE/USDT:USDT`) di CEX A | `SELL_SHORT` Perp (`BASE/USDT:USDT`) di CEX B | **Mesin profit utama & paling fleksibel**. Bisa menghasilkan profit di **segala kondisi pasar** (bullish, bearish, maupun sideways) dengan memanfaatkan **selisih funding rate antar 2 CEX** atau **perbedaan jam pencairan (*interval mismatch* 1h vs 4h vs 8h)**. Tidak butuh Unified Margin dan bisa memakai leverage konservatif (1x–2x). |
| **`spot_perp_same_cex`** *(Spot Buy – Futures Short)* | `BUY_SPOT` (`BASE/USDT`) di CEX A | `SELL_SHORT` Perp (`BASE/USDT:USDT`) di CEX A | **Paling aman dari likuidasi silang**. Menggunakan *Unified Margin* di 1 bursa yang sama (OKX/Bybit). Koin Spot yang dibeli langsung menjadi jaminan (*collateral*) posisi Short Perp. Hanya aktif saat funding Perp **positif tinggi** dan rasio kolateral koin $\ge 0.85$. |
| **`spot_perp_cross_cex`** | `BUY_SPOT` di CEX A | `SELL_SHORT` Perp di CEX B | Membeli Spot di CEX A dan Short Perp di CEX B. (Default nonaktif di `scanner.base.yaml`, dapat diaktifkan via `enable_spot_perp_cross_cex: true`). |

---

### 1.2.1 Bedah Mendalam: Bagaimana Futures Long – Futures Short (`perp_perp_cross_cex`) Mencetak Profit?

Berbeda dengan rute *Spot–Short* yang hanya bisa profit ketika pasar *bullish* (funding positif), rute **Futures Long vs Futures Short (`perp_perp_cross_cex`)** bekerja **dua arah** di seluruh pasangan CEX (`tryPerpPerpPair` di `route_builder.go` memeriksa `CEX_A -> CEX_B` sekaligus `CEX_B -> CEX_A`).

Ada **4 skenario nyata** di mana *Futures Long – Futures Short* menghasilkan uang:

#### Skenario A — *Double Payday* (Long Negatif + Short Positif)
Terjadi ketika sentimen trader ritel di dua bursa berbeda arah (misal di MEXC banyak yang Short sehingga funding **negatif**, sedangkan di Bybit banyak yang Long sehingga funding **positif**):
- **Kaki A (`BUY_LONG` Perp di MEXC)**: Funding $-0.05\%$ / 8 jam $\rightarrow$ Karena Anda **Long** saat funding negatif, **Anda DIBAYAR $+0.05\%$** tiap 8 jam ($+0.15\%$/hari).
- **Kaki B (`SELL_SHORT` Perp di Bybit)**: Funding $+0.04\%$ / 8 jam $\rightarrow$ Karena Anda **Short** saat funding positif, **Anda DIBAYAR $+0.04\%$** tiap 8 jam ($+0.12\%$/hari).
- **Total Profit Kotor 24 Jam**: $+0.15\% + 0.12\% = \mathbf{+0.27\% / \text{hari}}$ ($\approx 98.5\% \text{ APR}$) — **kedua kaki sama-sama menerima bayaran!**

#### Skenario B — *Positive Funding Spread* (Kedua Bursa Positif, Tapi Selisihnya Lebar)
Saat pasar *bullish*, semua bursa memiliki funding positif, namun satu bursa melonjak lebih tinggi:
- **Kaki A (`BUY_LONG` Perp di OKX)**: Funding $+0.01\%$ / 8 jam $\rightarrow$ Anda **membayar** $-0.03\%$/hari.
- **Kaki B (`SELL_SHORT` Perp di MEXC)**: Funding $+0.09\%$ / 8 jam $\rightarrow$ Anda **menerima** $+0.27\%$/hari.
- **Total Profit Kotor 24 Jam**: $+0.27\% - 0.03\% = \mathbf{+0.24\% / \text{hari}}$.

#### Skenario C — *Bear Market / Negative Funding Spread* (Kedua Bursa Negatif — Spot-Short TIDAK BISA, Futures Long-Short BISA!)
Saat pasar *crash/bearish*, seluruh bursa memiliki funding **negatif** (posisi Short harus membayar ke posisi Long). Rute *Spot–Short* otomatis mati di kondisi ini, **tetapi `perp_perp_cross_cex` justru panen** dengan membalik posisi **Long di bursa yang paling negatif**:
- **Kaki A (`BUY_LONG` Perp di Bybit)**: Funding $-0.12\%$ / 8 jam $\rightarrow$ Sebagai **Long**, Anda **DIBAYAR** $+0.36\%$/hari.
- **Kaki B (`SELL_SHORT` Perp di OKX)**: Funding $-0.02\%$ / 8 jam $\rightarrow$ Sebagai **Short**, Anda hanya **membayar** $-0.06\%$/hari.
- **Total Profit Kotor 24 Jam**: $+0.36\% - 0.06\% = \mathbf{+0.30\% / \text{hari}}$.

#### Skenario D — *Interval Mismatch Arbitrage* (1 Jam vs 4 Jam vs 8 Jam)
Ketika koin sedang *volatile*, beberapa bursa mengubah interval pembayaran funding dari **8 jam** menjadi **4 jam** atau **1 jam**. Rumus `NormalizeGrossFunding24h` di Go Scanner secara otomatis menangkap peluang ini:
- Misal angka funding di layar terlihat mirip: CEX A $+0.02\%$ (tiap **8 jam**) vs CEX B $+0.02\%$ (tiap **1 jam**).
- Scanner menormalisasi ke 24 jam:
  - **Kaki A (`BUY_LONG` di CEX A — 8h)**: Membayar $0.02\% \times \frac{24}{8} = -0.06\%$/hari.
  - **Kaki B (`SELL_SHORT` di CEX B — 1h)**: Menerima $0.02\% \times \frac{24}{1} = +0.48\%$/hari.
  - **Selisih Bersih**: $+0.48\% - 0.06\% = \mathbf{+0.42\% / \text{hari}}$!

#### Manajemen Risiko Khusus Futures Long – Futures Short (`perp_perp_cross_cex`)
Karena kedua kaki berada di dua akun CEX terpisah (margin tidak menyatu seperti *Spot-Perp Unified Margin*), apabila harga koin terbang $+50\%$, kaki Long Perp di CEX A akan profit besar tetapi kaki Short Perp di CEX B akan mengalami *floating loss* yang menggerus margin CEX B. Untuk melindungi modal:
1. **Gunakan Leverage Rendah (1x–2x)**: Jangan gunakan leverage tinggi agar jarak likuidasi sangat jauh.
2. **Watchdog Margin Ratio (`check_margin_ratios`)**: Memantau margin tiap CEX secara terpisah; jika salah satu CEX menyentuh $>60\%$ keluar peringatan Telegram, dan jika $>75\%$ *Kill-Switch* aktif.
3. **Watchdog ADL (`check_adl_indicators`)**: Jika lawan transaksi Anda di salah satu CEX bangkrut dan lampu indikator *Auto-Deleveraging* menyentuh $\ge 4/5$, sistem langsung melakukan **Early Unwind** (menutup kedua kaki secara bersamaan dengan `reduceOnly=True`) sebelum posisi Anda diputus sepihak oleh bursa.
4. **Watchdog Auto-Exit Normalisasi 24h (`check_exit_conditions`)**: Memantau selisih funding bersih berbasis 24 jam (`short_24h - long_24h`). Begitu spread berbalik negatif atau `target_hold_days` tercapai, kedua kaki Futures ditutup serentak via Market Order `reduceOnly`.

---

### 1.3 Rumus Matematika Profit & Filter Kelayakan (`matrix_evaluator.go`)

Sistem **tidak pernah** masuk posisi hanya karena melihat angka funding rate tinggi di permukaan. Sebelum sinyal dikirim, Go Scanner menghitung biaya nyata dari kedalaman *Orderbook L2 (20 level)*:

#### 1. Normalisasi Gross Funding 24 Jam ($R_{\text{gross\_24h}}$)
Karena tiap bursa/koin bisa memiliki interval funding berbeda (1h, 4h, atau 8h), semua rate dinormalisasi ke basis **24 jam**:
$$R_{\text{gross\_24h}} = \underbrace{-r_{\text{long}} \times \frac{24}{I_{\text{long}}}}_{\text{Aktif di Perp-Perp (0 jika Spot)}} + \left(r_{\text{short}} \times \frac{24}{I_{\text{short}}}\right)$$
*Contoh 1 (Futures Long–Short `perp_perp_cross_cex`)*: Kaki A Long Perp di MEXC dengan funding $-0.03\%$ tiap 4 jam ($6\times$ sehari), Kaki B Short Perp di Bybit dengan funding $+0.02\%$ tiap 1 jam ($24\times$ sehari):
$$R_{\text{gross\_24h}} = \left(-(-0.03\%) \times \frac{24}{4}\right) + \left(0.02\% \times \frac{24}{1}\right) = +0.18\% + 0.48\% = +0.66\% \text{ per hari}$$
*Contoh 2 (Spot–Short `spot_perp_same_cex`)*: Kaki A Spot BTC (`0%`), Kaki B Short Perp BTC dengan funding $+0.04\%$ tiap 8 jam ($3\times$ sehari):
$$R_{\text{gross\_24h}} = 0 + \left(0.04\% \times \frac{24}{8}\right) = +0.12\% \text{ per hari}$$

#### 2. Kalkulasi Slippage VWAP & Biaya Round-Trip ($C_{\text{RT}}$)
Untuk membuka **dan** menutup rute (total 4 transaksi: *entry A, entry B, exit A, exit B*), sistem menyimulasikan pembelian sebesar `target_notional_usdt` (misal $\$1,000$) menembus buku order (*Volume-Weighted Average Price*):
$$\text{Slippage}_k = \frac{|\text{VWAP}_k - \text{MidPrice}_k|}{\text{MidPrice}_k}$$
$$C_{\text{RT}} = 2(\text{Fee}_A + \text{Fee}_B) + 2(\text{Slippage}_A + \text{Slippage}_B)$$

#### 3. Breakeven Days ($T_{\text{BE}}$) & Net Daily Yield ($Y_{\text{net}}$)
Berapa hari posisi harus ditahan sampai biaya transaksi + slippage balik modal?
$$T_{\text{BE}} = \frac{C_{\text{RT}}}{R_{\text{gross\_24h}}}, \qquad Y_{\text{net}} = R_{\text{gross\_24h}} - \frac{C_{\text{RT}}}{T_{\text{hold}}}$$

Sinyal **HANYA** diterbitkan ke Redis `core:signals` jika lolos **seluruh** gerbang berikut:
1. **Data Segar (*Freshness Gate*)**: Umur orderbook $\le 2500\text{ms}$, umur funding $\le 120\text{s}$.
2. **Likuiditas Tebal**: Volume 24 jam di kedua kaki $\ge \$5,000,000\text{ USDT}$ dan orderbook cukup tebal untuk menyerap ukuran modal.
3. **Balik Modal Cepat**: $T_{\text{BE}} \le \text{max\_breakeven\_days}$ (misal $\le 2.5$ hari untuk profil `funding_carry_core`).
4. **Profit Bersih di Atas Ambang**: $Y_{\text{net}} \ge \text{min\_net\_daily\_yield}$ (misal $\ge 0.10\%$ bersih/hari setelah dipotong semua biaya buka-tutup).

---

### 1.4 Siklus Hidup Posisi: Dari Entry hingga Auto-Exit (Realisasi Profit)

```text
[Go Scanner] --(Sinyal Valid < 2.5s)--> [Redis: core:signals]
                                               │
                                               ▼
[Python Consumer] --> Cek TTL, Kill-Switch, Lock Aset, Cek Saldo Free USDT
                                               │
                                               ▼
[DeltaNeutralExecutor] --> Hitung Qty 1:1 (LCM step_size) --> Order Leg A & Leg B
                                               │
                                  (Jika Leg B gagal: Emergency Unwind Leg A)
                                               │
                                               ▼
[PostgreSQL & Redis] --> Catat posisi OPEN di `active_positions` & `core:positions`
                                               │
                                               ▼
[RiskWatchdog Loop (tiap 15s)]
 ├── 1. Reconcile 1:1 Drift : Pastikan jumlah koin Leg A == Leg B di bursa
 ├── 2. Cek Margin Ratio    : >60% Warning Telegram, >75% Aktifkan Kill-Switch
 ├── 3. Cek Indikator ADL   : Jika lampu ADL >= 4/5 -> Early Unwind (tutup rute)
 └── 4. Cek Auto-Exit       : Jika Funding berbalik negatif ATAU target_hold_days tercapai
                              -> Unwind 1:1 kedua kaki (reduceOnly) -> Profit Terealisasi!
```

---

## Bagian 2: Bagaimana Menambah CEX Baru?

Arsitektur `nafaz_arb` v3.1 dirancang **modular (*Plug-and-Play*)**. Menambah bursa baru (misalnya **Binance**, **Gate.io**, atau **KuCoin**) hanya memerlukan **4 langkah** tanpa perlu mengubah logika matematika di `engine/` maupun logika eksekusi di `execution.py`.

---

### Langkah 1: Buat Adapter Go di `scanner-go/internal/adapter/<nama_cex>/`

Setiap bursa di Go Scanner wajib mengimplementasikan interface [`adapter.ExchangeAdapter`](./scanner-go/internal/adapter/contract.go):

```go
type ExchangeAdapter interface {
    Name() string
    Capabilities() Capabilities
    FetchOrderBook(ctx context.Context, symbol domain.CanonSymbol) (*domain.OrderBookSnapshot, error)
    FetchFundingRate(ctx context.Context, symbol domain.CanonSymbol) (*domain.FundingSnapshot, error)
    FetchCollateralRatio(ctx context.Context) (*domain.CollateralSnapshot, error)
    NormalizeSymbol(raw string, marketType domain.MarketType) (domain.CanonSymbol, error)
    ToRawSymbol(canon domain.CanonSymbol) string
}
```

#### Contoh Template `scanner-go/internal/adapter/binance/binance.go`:
Gunakan [`resilience.HTTPExecutor`](./scanner-go/internal/adapter/resilience/resilience.go) yang sudah tersedia untuk mendapatkan *Rate Limiter*, *Circuit Breaker*, dan *Exponential Backoff* secara otomatis:

```go
package binance

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"nafaz_arb/internal/adapter"
	"nafaz_arb/internal/adapter/resilience"
	"nafaz_arb/internal/domain"
)

type Adapter struct {
	cfg  domain.ExchangeConfig
	exec *resilience.HTTPExecutor
}

func NewAdapter(cfg domain.ExchangeConfig) (adapter.ExchangeAdapter, error) {
	if cfg.BaseURL == "" {
		cfg.BaseURL = "https://api.binance.com"
	}
	exec := resilience.NewHTTPExecutor(&http.Client{Timeout: 5 * time.Second}, resilience.Config{
		RateLimitRPS:            cfg.RateLimitRPS,
		MaxRetries:              cfg.MaxRetries,
		CircuitBreakerThreshold: cfg.CircuitBreakerThreshold,
	})
	return &Adapter{cfg: cfg, exec: exec}, nil
}

func (a *Adapter) Name() string { return "binance" }

func (a *Adapter) Capabilities() adapter.Capabilities {
	return adapter.Capabilities{
		HasSpot:           true,
		HasPerp:           true,
		HasCollateralInfo: a.cfg.UnifiedMargin,
		UnifiedMargin:     a.cfg.UnifiedMargin,
	}
}

func (a *Adapter) NormalizeSymbol(raw string, marketType domain.MarketType) (domain.CanonSymbol, error) {
	s := strings.ToUpper(strings.TrimSpace(raw))
	if !strings.HasSuffix(s, "USDT") {
		return "", fmt.Errorf("binance: unsupported quote in %q", raw)
	}
	base := strings.TrimSuffix(s, "USDT")
	return domain.NewCanonSymbol(base, marketType)
}

func (a *Adapter) ToRawSymbol(canon domain.CanonSymbol) string {
	return canon.BaseAsset() + "USDT"
}

// Implementasikan FetchOrderBook, FetchFundingRate, dan FetchCollateralRatio
// (Lihat scanner-go/internal/adapter/bybit/bybit.go sebagai referensi lengkap).
```

> **Aturan Kualitas Wajib**:
> - Panjang file maksimal **500 baris** dan *Cognitive Complexity* per fungsi maksimal **15** (sesuai skill `sonarqube-code-quality`).
> - Pastikan `FetchOrderBook` mengisi `Volume24hUSDT` (ambil dari endpoint ticker 24h jika endpoint orderbook tidak menyertakan volume).
> - Pastikan `FetchFundingRate` memvalidasi `IntervalHours > 0` dan `NextFundingMs > 0`.

#### Uji Adapter Baru dengan *Shared Contract Test Suite*:
Buat file `scanner-go/internal/adapter/binance/binance_test.go` dan panggil `adapter.RunContractSuite(t, ad, spotSym, perpSym)` untuk memverifikasi kepatuhan kontrak secara otomatis.

---

### Langkah 2: Daftarkan Adapter di Registry Go Scanner

Buka [`scanner-go/cmd/scanner/main.go`](./scanner-go/cmd/scanner/main.go) dan tambahkan 1 baris pada fungsi `BuildDefaultRegistry()`:

```go
func BuildDefaultRegistry() *adapter.Registry {
	reg := adapter.NewRegistry()
	reg.Register("okx", okx.NewAdapter)
	reg.Register("bybit", bybit.NewAdapter)
	reg.Register("mexc", mexc.NewAdapter)
	reg.Register("binance", binance.NewAdapter) // <-- Tambahkan di sini
	return reg
}
```

---

### Langkah 3: Tambahkan Konfigurasi Bursa di `configs/exchanges.yaml`

Buka [`configs/exchanges.yaml`](./configs/exchanges.yaml) dan tambahkan blok konfigurasi bursa baru:

```yaml
  binance:
    enabled: true
    base_url: "https://api.binance.com"
    ws_url: "wss://stream.binance.com:9443/ws"
    spot_taker_fee: 0.0010
    perp_taker_fee: 0.0005
    rate_limit_rps: 15
    max_retries: 3
    circuit_breaker_threshold: 5
    symbol_allowlist:
      - "BTC"
      - "ETH"
      - "SOL"
    symbol_blocklist: []
    unified_margin: true
    polling:
      book_interval_ms: 750
      funding_interval_sec: 30
      funding_fast_interval_sec: 5
      collateral_interval_sec: 1800
```

Begitu terdaftar di `BuildDefaultRegistry()` dan `enabled: true` di `exchanges.yaml`, **Auto-Route Matrix** di Go Scanner akan otomatis memindai semua pasangan rute antara bursa baru tersebut dengan OKX, Bybit, dan MEXC!

---

### Langkah 4: Daftarkan Client `ccxt` di Python Executor (`executor-py/main.py`)

Karena `executor-py` menggunakan library standar `ccxt` (yang sudah mendukung $>100$ bursa crypto dengan format simbol kanonis `BASE/USDT` dan `BASE/USDT:USDT`), Anda hanya perlu menambahkan kredensial ENV di [`executor-py/main.py`](./executor-py/main.py):

1. Tambahkan spesifikasi di `build_ccxt_exchanges()` pada [`executor-py/main.py`](./executor-py/main.py):
```python
        "binance": {
            "cls": ccxt.binance,
            "apiKey": os.getenv("BINANCE_API_KEY", ""),
            "secret": os.getenv("BINANCE_API_SECRET", ""),
        },
```
2. Tambahkan `"BINANCE_API_KEY"` ke tuple pengecekan `run_preflight()` di [`executor-py/main.py`](./executor-py/main.py) dan ke file [`.env.example`](./.env.example).
3. *(Opsional)* Jika bursa baru memiliki struktur payload *Unified Account Margin Ratio* khusus di `fetch_balance()["info"]`, tambahkan fungsi helper `_<cex>_margin_ratio(self, info)` di [`executor-py/watchdog.py`](./executor-py/watchdog.py) (jika tidak, watchdog otomatis menggunakan fallback standar `used["USDT"] / total["USDT"]`).

---

### Checklist Verifikasi Setelah Menambah CEX Baru

Jalankan perintah berikut sebelum *deploy*:
```bash
# 1. Jalankan Contract & Unit Test Go
cd scanner-go && go test ./...

# 2. Jalankan Unit & Integration Test Python
cd ../executor-py && pytest -q

# 3. Verifikasi Preflight Koneksi Live / Container
podman compose up --build -d
podman compose exec executor python main.py --preflight
```
