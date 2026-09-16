# 🕵️ Detektif Hoax AI

**Analisa Hoax Indonesia dengan Google Gemini**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Streamlit](https://img.shields.io/badge/streamlit-1.31+-FF4B4B.svg)](https://streamlit.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Aplikasi web berbasis **Python + Streamlit** yang menggunakan
**Google Gemini API** (SDK `google-genai` terbaru) untuk membantu
membedah apakah sebuah teks berita, artikel, atau broadcast WA
cenderung mengandung pola bahasa **disinformasi/hoax** atau tidak.

> ⚠️ Aplikasi ini HANYA melakukan **analisis awal linguistik**
> menggunakan Large Language Model (LLM). Ini **BUKAN** pengganti
> pengecekan fakta resmi seperti [TurnBackHoax.id](https://turnbackhoax.id/)
> atau kantor berita terverifikasi.

---

## ✨ Fitur Utama

- 🔍 **Analisis Bahasa** — Mendeteksi pola clickbait, emosi berlebihan, dan klaim absolut.
- 🌐 **Verifikasi Fakta via Google Search (Grounding)** — Gemini mencari sumber kredibel di internet untuk **memverifikasi klaim faktual secara real-time** sebelum memberi skor. Mengurangi false-positive untuk berita yang sebenarnya benar tapi pakai bahasa dramatis.
- 📊 **Probabilitas Hoax** — Skor 0–100% dengan indikator warna (Hijau/Kuning/Merah).
- 🧾 **Saran Cek Fakta** — Rekomendasi langkah verifikasi yang bisa dilakukan user.
- 🔗 **Sumber Web (Citations)** — Menampilkan URL situs yang dipakai Gemini untuk verifikasi (mis. Kompas, BBC, TurnBackHoax.id).
- 🧠 **Structured Output** — Memaksa Gemini mengembalikan JSON valid via **Pydantic**.
- 🆕 **📚 TurnBackHoax Widget** — Otomatis cari artikel terkait di TurnBackHoax.id (web scraping dengan BeautifulSoup, auto-detect hoax type: SALAH/PENIPUAN/FAKTA/LEBIKAN).
- 🆕 **📰 Multi-Source News Widget** — Cari artikel terkait di **6 media Indonesia sekaligus**: Detik, Antara, Liputan6 (termasuk cek-fakta), Republika, Suara, Okezone. Toggle on/off per sumber via sidebar. Artikel cek-fakta diprioritaskan.
- 🆕 **🔗 Share Hasil** — Bagikan analisis ke Twitter/WhatsApp atau copy formatted text ke clipboard.
- 🆕 **📋 Riwayat Analisis** — Tab khusus menampilkan 10 analisis terakhir, **persistent ke file JSON** (`~/.hoaxraja/history.json`).
- 🆕 **📚 Tab Edukasi** — Tips mengenali hoax + direktori 6 situs cek fakta terpercaya (lokal & internasional).
- 🆕 **⚡ Retry Logic** — Auto-retry API call dengan exponential backoff (3 attempts) untuk error 429/500/503.
- 🆕 **✅ Validasi Input** — Cek panjang teks (min 10, max 10.000 karakter).
- 🆕 **🎨 Custom CSS** — Tampilan lebih menarik dengan gradient cards, badges berwarna, dan modern layout.
- 🆕 **📝 Logging** — Proper Python logger dengan timestamp untuk debugging.
- 🛡️ **Error Handling** — Custom exceptions, validasi API Key, dan penanganan error spesifik per jenis (rate limit, JSON parse, schema validation, dll).

> 💡 **Tips:** Toggle "🌐 Verifikasi Fakta via Google Search" di UI untuk mengaktifkan/menonaktifkan grounding. Nonaktifkan jika ingin analisis pola bahasa saja (lebih cepat, tanpa internet).

---

## 🛠️ Tech Stack (2026)

| Komponen | Versi / Library |
|---|---|
| Bahasa | Python 3.10+ |
| UI Framework | [Streamlit](https://streamlit.io/) |
| AI SDK | [`google-genai`](https://pypi.org/project/google-genai/) (SDK baru, **bukan** `google-generativeai`) |
| Model | `gemini-3.5-flash` (fallback ke `gemini-3.8-flash` jika tersedia) |
| Validasi | [Pydantic](https://docs.pydantic.dev/) v2 |
| Config | `python-dotenv` |
| Web Scraping | `requests`, `BeautifulSoup4`, `lxml` (untuk TurnBackHoax widget) |
| Caching | `@st.cache_data` dengan TTL 5-10 menit |
| Config | `python-dotenv` |

---

## 📂 Struktur Proyek

```
HoaxRaja/
├── app.py              # File utama aplikasi Streamlit
├── requirements.txt    # Daftar dependency
├── .env.example        # Template API Key
└── README.md           # Dokumentasi (file ini)
```

---

## 🚀 Cara Menjalankan

### 1. Clone / Download Proyek Ini

```bash
cd HoaxRaja
```

### 2. Buat Virtual Environment (Disarankan)

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Siapkan API Key

1. Ambil API Key gratis di: **https://aistudio.google.com/app/apikey**
2. Salin file `.env.example` menjadi `.env`
3. Isi API Key kamu:

```
GEMINI_API_KEY=AIzaSy...your_key_here
```

### 5. Jalankan Aplikasi

```bash
streamlit run app.py
```

Browser akan otomatis terbuka di `http://localhost:8501` 🎉

---

## 🧪 Cara Pakai

1. Buka aplikasi di browser.
2. Tempelkan teks berita/artikel/chat WA yang mencurigakan ke dalam kolom input.
3. Klik tombol **"🔍 Analisis Berita"**.
4. Tunggu beberapa detik — Gemini akan mengembalikan:
   - **Probabilitas Hoax** (0–100%)
   - **Analisis Bahasa** (pola clickbait, emosi, dll.)
   - **Saran Cek Fakta** (langkah verifikasi)
   - **Kesimpulan** (Aman / Mencurigakan / Hoax)

---

## 📚 Untuk Mahasiswa AI Semester 1

Proyek ini cocok untuk belajar:

- ✅ **Prompt Engineering** — bagaimana menyusun *system prompt* yang mengarahkan AI.
- ✅ **Structured Outputs** — cara memaksa LLM mengembalikan JSON sesuai schema.
- ✅ **Pydantic** — validasi tipe data Pythonic.
- ✅ **Environment Variables** — praktik aman menyimpan API Key.
- ✅ **Streamlit Basics** — `set_page_config`, `text_area`, `button`, `spinner`, `progress`, `metric`, `info`, `success`, `warning`, `error`.

Selamat belajar! 🚀

---

## 📝 Lisensi

MIT License — bebas digunakan untuk portfolio, tugas kuliah, atau project pribadi.

---

## ☁️ Deploy ke Streamlit Community Cloud (GRATIS, Selamanya)

### Kenapa Streamlit Cloud?

- ✅ **100% gratis** untuk public repo
- ✅ **HTTPS otomatis**
- ✅ **Auto-deploy** setiap push ke GitHub
- ✅ **Sleep after inactivity** (tidak membebani Anda)
- ✅ **Secret management** untuk API key (aman)

### Step 1: Buat Akun GitHub

Jika belum punya: [github.com/signup](https://github.com/signup) (gratis).

### Step 2: Buat Repository Baru di GitHub

1. Login ke GitHub
2. Klik **+** (kanan atas) → **New repository**
3. Isi form:
   - **Repository name**: `HoaxRaja` (atau nama lain)
   - **Description**: `🕵️ Detektif Hoax AI - Analisa hoax Indonesia dengan Gemini`
   - **Visibility**: ✅ **Public** (wajib untuk tier gratis Streamlit Cloud)
   - ❌ **JANGAN** centang "Add README" / ".gitignore" (kita sudah punya)
4. Klik **Create repository**
5. **CATAT URL** repo (misal `https://github.com/rajaadedia/HoaxRaja`)

### Step 3: Push Kode dari Local ke GitHub

Buka PowerShell di folder project, lalu jalankan:

```powershell
cd "c:\Users\rajaa\Music\HoaxRaja"

# Inisialisasi Git (hanya sekali)
git init
git branch -M main

# Add semua file (SELAIN .env karena ada di .gitignore)
git add .

# Commit pertama
git commit -m "Initial commit: HoaxRaja v1.0"

# Connect ke GitHub (GANTI URL dengan URL repo Anda!)
git remote add origin https://github.com/rajaadedia/HoaxRaja.git

# Push ke GitHub
git push -u origin main
```

Jika diminta login: pakai **Personal Access Token** (bukan password). Buat di: [github.com/settings/tokens](https://github.com/settings/tokens) → Generate new token (classic) → Scope: `repo`.

### Step 4: Deploy ke Streamlit Cloud

1. Buka **[share.streamlit.io](https://share.streamlit.io)**
2. Login pakai akun GitHub Anda
3. Klik **"New app"**
4. Isi form:
   - **Repository**: pilih `rajaadedia/HoaxRaja`
   - **Branch**: `main`
   - **Main file path**: `app.py`
   - **App URL**: pilih subdomain (misal `hoaxraja.streamlit.app`)
5. Klik **"Advanced settings"** (⚠️ PENTING!)
6. Di bagian **"Secrets"**, paste ini (GANTI dengan API key asli Anda):
   ```toml
   GEMINI_API_KEY = "AIzaSy...your_key_here"
   ```
7. Klik **"Deploy!"**
8. Tunggu 3-5 menit. App Anda akan live di `https://hoaxraja.streamlit.app` 🎉

### Step 5: Update Deployment Setelahnya

Setiap kali Anda edit kode lokal:

```powershell
cd "c:\Users\rajaa\Music\HoaxRaja"
git add .
git commit -m "Deskripsi perubahan"
git push
```

Streamlit Cloud akan **otomatis rebuild & redeploy** dalam 1-2 menit. ✅

### 🔐 Keamanan API Key

- ✅ API key disimpan di **Streamlit Secrets** (encrypted, tidak terlihat di GitHub)
- ✅ File `.env` lokal Anda TIDAK ter-upload (sudah di-`.gitignore`)
- ✅ User lain yang deploy fork harus pakai API key sendiri

### ⚙️ Update Secrets Setelah Deploy

1. Buka [share.streamlit.io](https://share.streamlit.io) → pilih app Anda
2. Klik **⋮** (menu) → **Settings** → **Secrets**
3. Edit value, klik **Save**
4. App akan otomatis restart

### 💡 Tips Tambahan

- **Custom domain**: Streamlit Cloud tidak support custom domain di tier gratis. Pakai `*.streamlit.app` saja.
- **Private repo**: Untuk private repo, perlu upgrade ke Streamlit for Teams (berbayar).
- **Cold start**: App sleep setelah tidak ada访问 selama ~7 hari. Klik "Wake up" untuk restart.

---

## 🐛 Troubleshooting

**Q: Build gagal di Streamlit Cloud?**
A: Cek log di dashboard. Biasanya karena `requirements.txt` ada package yang conflict. Test lokal dulu: `pip install -r requirements.txt`.

**Q: API key error "PERMISSION_DENIED"?**
A: Pastikan API key benar di Streamlit Secrets. Generate ulang di [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey).

**Q: Scraping gagal (404/timeout)?**
A: Beberapa media kadang block scraper. Sudah ada retry+backoff handling. Cek log di dashboard.

**Q: Mau pakai database (bukan file JSON)?**
A: Tier gratis Streamlit Cloud tidak punya persistent storage. History akan reset setiap restart. Untuk upgrade, pakai Streamlit for Teams atau deploy ke Railway/Render (juga punya free tier).