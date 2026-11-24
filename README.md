# INHARA CAMP

Aplikasi administrasi penyewaan perlengkapan camping berbasis Streamlit dan SQLite.
Fitur yang tersedia: ringkasan, data alat dan unit, kategori, identitas penyewa,
penyewaan, transaksi, pengembalian, serta login dengan riwayat aktivitas.

## Menjalankan di Windows

1. Pasang Python 3.10 atau lebih baru.
2. Jalankan `run.bat`. Skrip membuat virtual environment dan memasang dependensi
   saat pertama kali dijalankan.
3. Buka alamat yang ditampilkan di terminal (biasanya `http://localhost:8501`).

Menjalankan manual:

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Database `inhara_camp.db` dibuat otomatis di folder proyek. Lokasi database dapat
diubah dengan variabel lingkungan `INHARA_DB_PATH`.

## Login dan pengguna

- Saat pertama dijalankan belum ada akun. Akun pertama yang didaftarkan melalui
  tab **Daftar** otomatis menjadi **admin**.
- Pendaftaran berikutnya membuat akun **staf**.
- Admin melihat riwayat login seluruh akun; staf hanya melihat riwayatnya sendiri.
- Password disimpan sebagai hash **PBKDF2-HMAC-SHA256** (600.000 iterasi, salt
  acak per akun). Password asli tidak pernah disimpan.
- Setelah 5 kali gagal login untuk username yang sama dalam 15 menit, percobaan
  berikutnya ditolak sementara.
- Setiap aktivitas masuk, keluar, dan login gagal dicatat di tabel
  `login_history` (username, waktu, alasan, alamat IP, dan perangkat).

## Jalankan dengan Docker

Pasang Docker Desktop, lalu dari folder proyek jalankan:

```powershell
docker compose up --build
```

Buka `http://localhost:8501`. Database disimpan dalam volume Docker
`inhara_data` agar tetap ada setelah container dimulai ulang. Hentikan dengan
`Ctrl+C`; gunakan `docker compose down` untuk menurunkan container tanpa
menghapus datanya.

## Publikasikan ke internet

Aplikasi dapat dipublikasikan sebagai aplikasi Streamlit dengan memilih `app.py`
sebagai entry point dan memasang dependensi dari `requirements.txt`. Untuk
penggunaan operasional, pastikan penyimpanan SQLite persisten dan aksesnya
dibatasi. Karena login memakai session per browser, gunakan HTTPS agar password
tidak terkirim dalam teks biasa.

## Catatan

- Stok dan tarif sewa contoh adalah data simulasi, bukan data faktual perusahaan.
- Untuk mengulang data awal, tutup aplikasi lalu hapus file `inhara_camp.db` dan
  jalankan kembali aplikasi.
