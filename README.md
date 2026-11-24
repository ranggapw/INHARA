# INHARA CAMP

Aplikasi administrasi penyewaan perlengkapan camping berbasis Streamlit dan SQLite.
Fitur yang tersedia: ringkasan, data alat dan unit, kategori, identitas penyewa,
penyewaan, transaksi, pengembalian, login multi-peran dengan riwayat aktivitas, serta
manajemen pengguna untuk owner.

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
  tab **Daftar** otomatis menjadi **owner**.
- Pendaftaran berikutnya membuat akun **kasir**; owner dapat mengubah perannya
  menjadi **admin** melalui halaman **Manajemen pengguna**.
- Password disimpan sebagai hash **PBKDF2-HMAC-SHA256** (600.000 iterasi, salt
  acak per akun). Password asli tidak pernah disimpan.
- Setelah 5 kali gagal login untuk username yang sama dalam 15 menit, percobaan
  berikutnya ditolak sementara.
- Setiap aktivitas masuk, keluar, dan login gagal dicatat di tabel
  `login_history` (username, waktu, alasan, alamat IP, dan perangkat).

### Peran dan akses menu

| Menu                  | Owner | Admin | Kasir |
| --------------------- | :---: | :---: | :---: |
| Ringkasan             |  Ya   |  Ya   |  Ya   |
| Data alat             |  Ya   |  Ya   |  -    |
| Kategori alat         |  Ya   |  Ya   |  -    |
| Identitas penyewa     |  Ya   |  Ya   |  Ya   |
| Penyewaan baru        |  Ya   |  Ya   |  Ya   |
| Transaksi             |  Ya   |  Ya   |  Ya   |
| Pengembalian          |  Ya   |  Ya   |  Ya   |
| Riwayat login         |  Ya   |  Ya   |  Ya   |
| Manajemen pengguna    |  Ya   |  -    |  -    |

- **Owner** mengelola seluruh operasional dan akun pengguna. Halaman
  **Manajemen pengguna** memungkinkan owner mengubah peran, menonaktifkan akun,
  dan mereset password. Sistem menolak perubahan yang menghilangkan owner aktif
  terakhir.
- **Admin** mengelola inventaris (data alat dan kategori) serta seluruh transaksi,
  tanpa akses ke manajemen pengguna.
- **Kasir** fokus pada pelayanan penyewaan dan pengembalian tanpa akses ke
  inventaris dan manajemen pengguna.
- Owner melihat riwayat login seluruh akun beserta filter username; admin dan
  kasir hanya melihat riwayat aktivitas akunnya sendiri.

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
- Database lama dengan peran `admin`/`staf` dimigrasikan otomatis saat aplikasi
  dijalankan: `staf` menjadi `kasir`, dan akun pertama dipromosikan menjadi
  `owner` bila belum ada owner.
- Untuk mengulang data awal, tutup aplikasi lalu hapus file `inhara_camp.db` dan
  jalankan kembali aplikasi.
