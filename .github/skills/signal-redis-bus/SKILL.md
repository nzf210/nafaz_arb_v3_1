---
name: signal-redis-bus
description: "Pedoman kontrak payload sinyal arbitrase v3.1, namespace Redis (core:* dan meta:*), deduplikasi sinyal, dan event bus pada nafaz_arb v3.1. Gunakan saat: mendefinisikan skema JSON sinyal, mengimplementasikan Redis publisher di Go (sink/redis_pub.go), deduplikasi sinyal, konfigurasi channel Redis, atau validasi kontrak data antarmuka scanner-executor."
user-invocable: true
---

# Signal Contract & Redis Event Bus (`nafaz_arb` v3.1)

Pedoman kontrak data antara subsistem `scanner-go` (publisher) dan `executor-py` (consumer) menggunakan Redis sebagai *state store* dan *event bus*.

## 1. Skema JSON Sinyal v3.1 (`schema_version: "3.1"`)

Setiap sinyal yang diterbitkan oleh *scanner* wajib mematuhi skema terversi v3.1:

```json
{
  "schema_version": "3.1",
  "signal_id": "sig_okx_bybit_btc_1700000000123",
  "dedup_key": "perp_perp:BTC:okx:bybit",
  "profile_id": "funding_carry_core",
  "route_type": "perp_perp_cross_cex",
  "base_asset": "BTC",
  "target_notional_usdt": 1000.0,
  "legs": {
    "leg_a": {
      "exchange": "okx",
      "market_type": "linear_perp",
      "raw_symbol": "BTC-USDT-SWAP",
      "canon_symbol": "BTC/USDT:USDT",
      "action": "BUY_LONG",
      "vwap_price": 65000.5,
      "funding_rate": -0.0001,
      "interval_hours": 8
    },
    "leg_b": {
      "exchange": "bybit",
      "market_type": "linear_perp",
      "raw_symbol": "BTCUSDT",
      "canon_symbol": "BTC/USDT:USDT",
      "action": "SELL_SHORT",
      "vwap_price": 65010.2,
      "funding_rate": 0.0005,
      "interval_hours": 8
    }
  },
  "metrics": {
    "gross_funding_24h": 0.0018,
    "round_trip_cost": 0.0012,
    "net_daily_yield": 0.001628,
    "breakeven_days": 0.67
  },
  "generated_at_ms": 1700000000123,
  "expires_at_ms": 1700000002623
}
```

## 2. Struktur Namespace Redis

Redis memisahkan data operasional (*core*) dan metadata pendukung (*meta*):

### Namespace `core:*` (Trading & Execution)
- `core:signals` (Redis Stream atau Pub/Sub): Channel utama distribusi sinyal yang siap diproses oleh *executor*.
- `core:dedup:<dedup_key>`: Key string berisi timestamp publikasi terakhir dan yield tersimpan dengan TTL `DedupCooldown` (default: 300 detik).
- `core:lock:<base_asset>`: Distributed lock mencegah eksekusi paralel pada aset dasar yang sama.
- `core:kill_switch`: Boolean flag global. Jika `true`, executor langsung membatalkan semua aktivitas order baru.

### Namespace `meta:*` (Observabilitas & Registry)
- `meta:exchange:<cex>`: Hash informasi kesehatan CEX (`last_ping_ms`, `status`, `active_symbols_count`, `http_429_count`).
- `meta:collateral:<cex>`: Snapshot haircut ratio kolateral untuk validasi *unified margin*.
- `meta:rejection_stats`: Counter metrik sinyal yang gugur berdasarkan alasan (`stale_book`, `high_slippage`, `low_yield`, `negative_gross`).

## 3. Logika Deduplikasi Sinyal (FR-5)

Agar *executor* tidak kebanjiran sinyal duplikat pada setiap *tick* (~1-2 detik):
1. **Dedup Key**: Dibentuk dari format `<route_type>:<base_asset>:<cex_a>:<cex_b>`.
2. **Pengecekan State**:
   - Jika `core:dedup:<dedup_key>` belum ada di Redis, simpan yield dan terbitkan sinyal. Pasang TTL = `DedupCooldown` (misal 5 menit).
   - Jika key sudah ada: Bandingkan `new_net_yield - old_net_yield`. Sinyal hanya diterbitkan ulang sebelum cooldown habis jika terjadi lonjakan yield $\ge$ `MinYieldDelta` (misal 0.5% kenaikan yield).
3. **Penyusutan Memory**: Seluruh deduplication key wajib memiliki TTL otomatis agar memory Redis bersih.

## 4. Validasi Masa Berlaku (*TTL / Expiration*)

- *Scanner* menetapkan `expires_at_ms = generated_at_ms + MaxSignalValidityMs` (umumnya 2.500 ms).
- *Executor* wajib memvalidasi `now_ms() < expires_at_ms` sebelum mengirim order ke exchange API. Sinyal yang kadaluarsa langsung dibuang dengan log peringatan.
