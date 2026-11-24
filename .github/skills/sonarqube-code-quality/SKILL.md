---
name: sonarqube-code-quality
description: "Standar kualitas kode dan batas kompleksitas SonarQube untuk Go (scanner-go) dan Python (executor-py). Gunakan saat: menulis, mengubah, merefaktor, atau meninjau kode baru maupun lama untuk memastikan panjang file maksimal 500 baris, fungsi percabangan/cyclomatic complexity maksimal 16 (cognitive complexity <= 15), dan bebas duplikasi literal."
user-invocable: true
---
# SonarQube Code Quality & Complexity Rules (`nafaz_arb` v3.1)

Standar wajib ketika menulis atau memodifikasi kode di `scanner-go` (Go) maupun `executor-py` (Python) agar selalu lulus analisis statis SonarQube.

## 1. Batas Kuantitatif Wajib (Hard Limits)

1. **Maksimal Panjang File: $\le 500$ Baris**
   - Setiap file `.go` atau `.py` **tidak boleh melebihi 500 baris** (`wc -l <= 500`).
   - Jika sebuah file mendekati 450–500 baris, pecah berdasarkan tanggung jawab (*cohesive helpers* / sub-modul dalam package yang sama, misal `matrix_evaluator.go` + `route_scoring.go`).
2. **Maksimal Percabangan per Fungsi: $\le 16$ (Cognitive Complexity $\le 15$)**
   - Jumlah titik percabangan (*branching points*: `if`, `else if`, `for`, `while`, `case`, `except`, operator boolean `&&`/`||`/`and`/`or`, ternary) dalam satu fungsi/method **maksimal 16**, dan **SonarQube Cognitive Complexity maksimal 15**.
   - Hindari *nested `if/for/try`* lebih dari 2 tingkat; setiap tingkat *nesting* menambah penalti kognitif secara bertingkat di SonarQube (`+1` untuk level 1, `+2` untuk level 2, `+3` untuk level 3, dst.).

## 2. Pola Refactoring untuk Menekan Percabangan

1. **Early Return / Guard Clauses**
   - Ganti `if cond { ... } else { ... }` bersarang dengan *guard clause* yang langsung `return` atau `continue`.
2. **Ekstraksi Helper Terfokus**
   - Pisahkan ekstraksi data, validasi, dan orkestrasi I/O ke fungsi *private* kecil (`_extract_*`, `_validate_*`, `evaluatePair*`).
3. **Konstanta untuk Literal Berulang**
   - Jika string literal (mis. `"core:positions"`, `":USDT"`, header HTTP) dipakai $\ge 3$ kali dalam satu file, definisikan sebagai konstanta tingkat modul/package.
4. **Penanganan Exception Python**
   - Gunakan `logger.exception(...)` di dalam blok `except` saat mencatat kegagalan, atau ekstrak helper *safe parser* (`_safe_float`) agar fungsi utama bebas dari blok `try/except` berulang.

## 3. Verifikasi Sebelum Selesai

Sebelum menyatakan perubahan kode selesai:
- Jalankan `wc -l` pada seluruh file yang disentuh dan pastikan semuanya $\le 500$ baris.
- Jalankan tool `sonarqube_analyzeFile` dan `problems` pada file yang dimodifikasi untuk memastikan **0 error Cognitive Complexity** dan **0 code smell kritis**.
