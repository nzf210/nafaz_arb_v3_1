---
name: cex-adapter
description: "Pedoman implementasi, registrasi, resilience, dan pengujian modular CEX adapter di Go (scanner-go) untuk nafaz_arb v3.1. Gunakan saat: menambah adapter bursa CEX baru (OKX, Bybit, MEXC, Binance), implementasi ExchangeAdapter interface, normalisasi simbol BASE/USDT, rate limiter, circuit breaker, atau contract testing CEX."
user-invocable: true
---

# Modular CEX Adapter Guide (`nafaz_arb` v3.1)

Pedoman arsitektur untuk membangun konektor CEX modular (*plug-and-play*) pada subsistem `scanner-go` tanpa memodifikasi *engine* kalkulasi arbitrase.

## 1. Arsitektur Kontrak `ExchangeAdapter`

Setiap adapter CEX harus mengimplementasikan kontrak interface standar di `internal/adapter/contract.go`:

```go
package adapter

import (
	"context"
	"time"
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
```

## 2. Aturan Normalisasi Simbol & Interval (FR-2)

1. **Format Kanonik USDT:**
   - Spot: `BASE/USDT` (contoh: `BTC/USDT`, `ETH/USDT`).
   - Linear Perp: `BASE/USDT:USDT` (contoh: `BTC/USDT:USDT`).
   - Simpan `RawSymbol` asli bursa (misal OKX `BTC-USDT-SWAP` atau Bybit `BTCUSDT`).
2. **Validasi `IntervalHours`:**
   - Interval funding rate bursa harus dibaca dinamis (`1`, `4`, `8` jam).
   - Wajib validasi `IntervalHours > 0`. Jika bursa tidak mengembalikan interval atau bernilai `<= 0`, tolak snapshot dan catat error.

## 3. Ritme Polling & Rate Limiter Terpisah (NFR-1)

Gunakan pola ritme independen untuk melindungi kuota API:
- **Orderbook / Mark Price**: WebSocket stream atau REST polling `500ms – 1000ms`.
- **Funding Rate & Interval**: REST polling `15s – 60s`. Akselerasi ke `5s – 10s` pada 2 menit sebelum jam settlement.
- **Collateral / Account Ratio**: REST polling `15m – 60m`.

### Paket `resilience`
Setiap adapter wajib dibungkus layer resilience (`internal/adapter/resilience/`):
- **Token-Bucket Rate Limiter**: Budget maksimal 70%–80% dari batas resmi CEX.
- **Exponential Backoff + Jitter**: Digunakan saat mendeteksi HTTP `429 Too Many Requests`.
- **Circuit Breaker**: Isolasi CEX jika error rate berturut-turut melebihi ambang batas (contoh: 5x timeout berturut-turut), transisi ke status *Half-Open* setelah cooldown period.

## 4. Prosedur Menambah CEX Baru (FR-1)

1. **Buat Direktori Adapter**:
   Buat package baru di `internal/adapter/<cex>/` (misal `okx`, `bybit`, `mexc`).
2. **Implementasikan Method**:
   Implementasikan seluruh interface `ExchangeAdapter`. Pastikan penanganan error HTTP dan JSON parsing terisolasi di package tersebut.
3. **Daftarkan di `Registry`**:
   Daftarkan constructor adapter pada dynamic registry di `cmd/scanner/main.go` atau `internal/adapter/registry.go`:
   ```go
   registry.Register("okx", okx.NewAdapter)
   ```
4. **Konfigurasi YAML**:
   Tambahkan profil kredensial, endpoint, dan rate limit budget di `configs/exchanges.yaml`. Tidak boleh ada *hardcoded* endpoint di dalam kode adapter.

## 5. Standar Contract Testing

Setiap adapter wajib memiliki unit test kontrak otomatis (`<cex>_test.go`):
1. **Mapping Dua Arah**: Uji `NormalizeSymbol` dan `ToRawSymbol` untuk seluruh sampel simbol umum.
2. **Interval Positif**: Pastikan parser funding mengembalikan `IntervalHours > 0`.
3. **Resilience Mock**: Uji bahwa HTTP `429` memicu backoff dan tidak membocorkan error unhandled ke engine.
