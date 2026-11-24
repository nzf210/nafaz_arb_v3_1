---
name: executor-watchdog
description: "Pedoman implementasi subsistem eksekusi Python (ccxt) dan risk watchdog untuk nafaz_arb v3.1. Gunakan saat: membangun consumer sinyal Redis, eksekusi delta-neutral 1:1 dua kaki, penanganan partial fill, kepatuhan guardrail CEX (tick/step size), rekonsiliasi posisi/saldo, pemantauan margin ratio / ADL, kill switch, dan bot notifikasi Telegram."
user-invocable: true
---

# Executor 1:1 & Risk Watchdog Guide (`nafaz_arb` v3.1)

Pedoman arsitektur untuk subsistem `executor-py` (Python berbasis pustaka `ccxt`) yang bertugas mengonsumsi sinyal arbitrase v3.1, mengeksekusi order *delta-neutral* 1:1, serta memantau kesehatan margin dan risiko likuidasi secara terus-menerus.

## 1. Komponen Subsistem `executor-py`

Sesuai struktur PRD v3.1:
```text
executor-py/
├── consumer.py   # Validasi expires_at_ms & routing sinyal per profile_id
├── execution.py  # Eksekusi 1:1 delta-neutral, partial-fill handler, & guardrail
└── watchdog.py   # Rekonsiliasi posisi, pemantau margin/ADL, & alert Telegram
```

## 2. Validasi Pra-Order (`consumer.py`)

Sebelum mengeksekusi order dari sinyal yang masuk:
1. **Validasi `expires_at_ms`:**
   ```python
   import time

   now_ms = int(time.time() * 1000)
   if now_ms >= signal["expires_at_ms"]:
       logger.warning("Signal expired. Skipped: %s", signal["signal_id"])
       return
   ```
2. **Cek Global Kill-Switch:**
   Periksa flag `core:kill_switch` di Redis. Jika bernilai `true`, tolak sinyal baru.
3. **Distributed Asset Lock:**
   Ambil kunci `core:lock:<base_asset>` via Redis SETNX dengan TTL singkat (~10 detik) untuk mencegah tabrakan eksekusi pada koin yang sama.

## 3. Eksekusi Delta-Neutral 1:1 & Penanganan Partial Fill (`execution.py`)

### 3.1 Eksekusi Dua Kaki Seimbang
- Kedua kaki (Leg A dan Leg B) harus dieksekusi secara simultan menggunakan *asyncio / thread pool* atau sekuensial cepat (kaki yang lebih tidak likuid dieksekusi lebih dulu).
- Pastikan ukuran notional dan kuantitas koin benar-benar berimbang 1:1 agar portofolio tetap *delta-neutral*.

### 3.2 Penanganan *Partial Fill*
Jika Leg A terisi $60\%$ dan Leg B gagal atau mengalami *time out*:
1. **Langkah 1**: Segera batalkan (*cancel*) sisa pesanan Leg A yang belum terisi (*unfilled*).
2. **Langkah 2**: Sesuaikan pesanan Leg B agar nilainya tepat sama dengan kuantitas aktual Leg A yang berhasil terisi.
3. **Langkah 3**: Jika Leg B sama sekali tidak dapat dieksekusi setelah batas percobaan (*retry limit*), lakukan *emergency market close / unwind* pada Leg A untuk menghindari risiko *directional exposure* tak terproteksi.

### 3.3 Kepatuhan Guardrail Bursa
- **Precision / Tick Size**: Bulatkan harga order sesuai `price_filter` CEX.
- **Lot / Step Size**: Bulatkan kuantitas kontrak sesuai `lot_size_filter` CEX.
- **Minimum Notional**: Validasi nilai order $\ge$ batas minimal bursa (misal $5 atau $10 USDT).

## 4. Pemantauan Risiko & Rekonsiliasi (`watchdog.py`)

### 4.1 Rekonsiliasi Saldo & Posisi
- Lakukan sinkronisasi periodik (tiap 1–5 menit) antara posisi terbuka aktual di bursa vs *state* posisi di database/Redis.
- Laporkan *drift* atau ketidakseimbangan ukuran kontrak antar kaki.

### 4.2 Pemantauan Rasio Margin & ADL (Auto-Deleveraging)
- Pantau *Margin Ratio* di akun *unified margin* atau akun *perpetual*:
  - **Threshold Waspada**: Kirim peringatan Telegram jika margin usage $> 60\%$.
  - **Threshold Kritis**: Hentikan pembukaan posisi baru jika margin usage $> 75\%$.
- Pantau indikator lampu ADL (1 s.d. 5 baris). Jika posisi masuk zona risiko pemotongan ADL tinggi, pertimbangkan untuk menutup rute lebih awal (*early unwind*).

### 4.3 Bot Notifikasi Telegram
Kirim notifikasi terstruktur untuk:
- Pembukaan dan penutupan rute berhasil (beserta kalkulasi realisasi *yield*).
- Anomali eksekusi (*partial fill unwind*, kegagalan order, HTTP `429`).
- Status *watchdog* harian dan aktivasi *kill-switch*.
