---
name: arb-engine
description: "Pedoman implementasi model matematika, VWAP orderbook slippage, normalisasi funding rate, staleness gate, dan auto-route evaluator di Go untuk nafaz_arb v3.1. Gunakan saat: kalkulasi net daily yield, breakeven days, VWAP slippage, penanganan interval mismatch 1h/4h/8h, filter kelayakan sinyal, auto-route matrix, atau multi-profile evaluation."
user-invocable: true
---

# Arbitrage Engine & Math Specification (`nafaz_arb` v3.1)

Pedoman teknis untuk paket `internal/engine/` pada `scanner-go`. Modul ini bertanggung jawab atas validasi kesegaran data (*staleness gate*), pembentukan matriks rute otomatis, kalkulasi kedalaman *orderbook* (VWAP), serta evaluasi kelayakan *yield*.

## 1. Kesegaran Data (*Staleness Gate*)

Sebelum kalkulasi dimulai pada setiap *tick*, periksa usia *snapshot* data pasar:
- `MaxBookAge`: $\le 2.500\text{ ms}$ (buang jika lebih lawas).
- `MaxFundingAge`: $\le 120\text{ detik}$.
- `MaxCollateralAge`: $\le 2\text{ jam}$ (untuk validasi kolateral *unified margin*).

*Rule*: Jika salah satu kaki rute melewati batas usia di atas, gugurkan rute tersebut dari evaluasi *tick* berjalan tanpa membuang siklus kalkulasi pasangan lain.

## 2. Auto-Route Matrix Generator (FR-3)

Generator membentuk kandidat pasangan rute secara dinamis dari CEX yang berstatus *healthy*:
1. `spot_perp_same_cex`:
   - Syarat CEX: `Capabilities.UnifiedMargin == true` dan `HasSpot && HasPerp`.
   - Posisi: *Buy Spot* + *Short Perp* 1:1.
   - Validasi: *Collateral haircut ratio* aset spot harus memenuhi `MinCollateralRatio`.
2. `perp_perp_cross_cex`:
   - Syarat: Kaki A (Long Perp) dan Kaki B (Short Perp) pada CEX berbeda.
   - Menangani perbedaan interval (*mismatch* 1j, 4j, 8j).
3. `spot_perp_cross_cex`:
   - Syarat: Buy Spot di CEX A + Short Perp di CEX B.
   - *Default*: Dinonaktifkan via konfigurasi YAML; aktifkan hanya jika diizinkan eksplisit.

## 3. Formulasi Model Matematika (Bagian 3 PRD)

### 3.1 Normalisasi Gross Funding 24 Jam
Konversi *funding rate* mentah $r_i$ (desimal) ke basis 24 jam dengan interval $I_i$ (jam):
$$\text{Gross Funding 24h} = \left(-r_{\text{long}} \times \frac{24}{I_{\text{long}}}\right) + \left(+r_{\text{short}} \times \frac{24}{I_{\text{short}}}\right)$$
*(Catatan: Kaki Spot pada `spot_perp_same_cex` memiliki funding = 0).*

### 3.2 Kalkulasi VWAP Orderbook & Slippage
Jangan gunakan *top-of-book* (Best Bid/Ask). Hitung VWAP berdasarkan target `notional_usdt` (misal $1.000 USDT):
$$\text{VWAP}_{\text{buy}} = \frac{\sum (P_i \times Q_i)}{\sum Q_i} \quad \text{dari asks (sampai akumulasi notional terpenuhi)}$$
$$\text{VWAP}_{\text{sell}} = \frac{\sum (P_i \times Q_i)}{\sum Q_i} \quad \text{dari bids (sampai akumulasi notional terpenuhi)}$$
$$\text{Slippage}_{\text{leg}} = \frac{|\text{VWAP} - P_{\text{mid}}|}{P_{\text{mid}}}$$

### 3.3 Biaya Round-Trip ($C_{\text{rt}}$) & Amortisasi
$$C_{\text{fee,rt}} = \text{Fee}_{\text{open,A}} + \text{Fee}_{\text{close,A}} + \text{Fee}_{\text{open,B}} + \text{Fee}_{\text{close,B}}$$
$$C_{\text{slip,rt}} \approx 2 \times (\text{Slippage}_{\text{legA}} + \text{Slippage}_{\text{legB}})$$
$$C_{\text{rt}} = C_{\text{fee,rt}} + C_{\text{slip,rt}}$$

Amortisasikan terhadap rencana durasi tahan $D_{\text{hold}}$ (`target_hold_days`):
$$\text{Net Daily Yield} = \text{Gross Funding 24h} - \frac{C_{\text{rt}}}{D_{\text{hold}}}$$
$$\text{Breakeven Days} = \frac{C_{\text{rt}}}{\max(\text{Gross Funding 24h},\; 10^{-6})}$$

## 4. Evaluasi Multi-Profil (FR-4)

Evaluator mengevaluasi beberapa profil secara paralel atau sekuensial per *tick* menggunakan hasil hitung VWAP yang sama (menghindari duplikasi kalkulasi):
- `funding_carry_core`: Mengutamakan kestabilan arus kas carry dengan target tahan lebih panjang ($D_{\text{hold}} \approx 7\text{ hari}$).
- `basis_convergence_fast`: Mengutamakan konvergensi basis spread tinggi dengan target durasi pendek ($D_{\text{hold}} \approx 1\text{ hari}$).

### Kriteria Kelayakan Sinyal (*Filter Gate*)
Sinyal hanya lolos jika:
1. $\text{Gross Funding 24h} > 0$
2. $\text{Breakeven Days} \le D_{\text{max\_breakeven}}$
3. $\text{Net Daily Yield} \ge \text{Min\_Net\_Daily\_Yield}$
4. Volume 24 jam & kedalaman buku order memenuhi ambang batas profil.
