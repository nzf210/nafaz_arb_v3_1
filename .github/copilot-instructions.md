# Agent & Code Quality Instructions (`nafaz_arb` v3.1)

Seluruh perubahan kode pada repositori ini (`scanner-go/` dan `executor-py/`) wajib mematuhi aturan kualitas SonarQube berikut:

1. **Batas Panjang File ($\le 500$ baris)**:
   - Tidak boleh ada file sumber (`.go` atau `.py`) yang melebihi **500 baris**. Pecah file yang melampaui batas ke file pendamping dalam package/modul yang sama.
2. **Batas Percabangan Fungsi ($\le 16$ cabang / Cognitive Complexity $\le 15$)**:
   - Setiap fungsi atau method maksimal memiliki **16 titik percabangan** (Cyclomatic Complexity $\le 16$) dan **SonarQube Cognitive Complexity $\le 15$**.
   - Gunakan *guard clauses* (*early return*), hindari *nesting* `if/for/try` lebih dari 2 level, dan ekstrak sub-langkah menjadi fungsi helper kecil.
3. **Zero Duplicate String Literals**:
   - Definisikan konstanta untuk string literal yang diulang $\ge 3$ kali dalam satu file.
4. **Verifikasi SonarQube**:
   - Gunakan skill `sonarqube-code-quality` dan periksa dengan `sonarqube_analyzeFile` / `problems` setiap kali selesai memodifikasi file.
