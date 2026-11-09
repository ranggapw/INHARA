# INHARA CAMP

Aplikasi administrasi penyewaan perlengkapan camping berbasis Streamlit dan SQLite.
Fitur yang sudah ada di aplikasi tetap tersedia: ringkasan, data alat, kategori,
identitas penyewa, penyewaan, transaksi, dan pengembalian.
Logo INHARA ditampilkan di header dan panel samping aplikasi.

## Jalankan di Windows

1. Pasang Python 3.10 atau lebih baru.
2. Jalankan `run.bat`. Saat pertama kali dijalankan, skrip membuat virtual
   environment dan memasang dependensi.
3. Buka alamat yang ditampilkan di terminal (biasanya `http://localhost:8501`).

Database `inhara_camp.db` dibuat otomatis di folder proyek. Di halaman aplikasi,
gunakan daftar **Pilih halaman** untuk membuka formulir dan mengelola data.
Aplikasi hanya dibuka dari komputer yang menjalankannya agar data administrasi
tidak terbuka ke jaringan tanpa perlindungan.

## Jalankan dengan Docker

Pasang Docker Desktop, lalu dari folder proyek jalankan:

```powershell
docker compose up --build
```

Buka `http://localhost:8501`. Database disimpan dalam volume Docker `inhara_data`
agar tetap ada setelah container dimulai ulang. Hentikan dengan `Ctrl+C`; gunakan
`docker compose down` untuk menurunkan container tanpa menghapus datanya.

## Publikasikan ke internet

Aplikasi dapat dipublikasikan sebagai aplikasi Streamlit dengan memilih `app.py`
sebagai entry point dan memasang dependensi dari `requirements.txt`. Untuk
penggunaan operasional, pastikan penyimpanan SQLite persisten dan aksesnya
dibatasi; penyimpanan lokal layanan hosting gratis dapat bersifat sementara.

Proyek ini menyediakan aplikasi web responsif yang dapat dibuka dari browser
Android. APK Android native tidak dibuat oleh Streamlit; APK memerlukan aplikasi
Android pembungkus dan alamat server web yang sudah dipublikasikan.
