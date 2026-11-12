SISTEM ADMINISTRASI INHARA CAMP
Prototype Python + Streamlit + SQLite

CARA MENJALANKAN DI VISUAL STUDIO CODE
1. Ekstrak folder inh ara_camp.zip ke komputer.
2. Buka folder inh ara_camp melalui VS Code.
3. Pastikan Python sudah terinstal.
4. Buka Terminal di VS Code.
5. (Opsional, disarankan) buat virtual environment:
   Windows:
     py -m venv .venv
     .venv\Scripts\activate
   Mac/Linux:
     python3 -m venv .venv
     source .venv/bin/activate
6. Instal library:
     pip install -r requirements.txt
7. Jalankan aplikasi:
     streamlit run app.py
8. Browser akan terbuka otomatis dengan aplikasi INHARA Camp.

Database inh ara.db akan dibuat otomatis ketika aplikasi pertama kali dijalankan.
Data kategori dan alat contoh juga diisi otomatis.

CATATAN
- Stok dan tarif sewa contoh adalah data simulasi, bukan data faktual perusahaan.
- Fitur yang tersedia: Beranda, Kategori (tambah/ubah/hapus), Data Alat Camping
  (tambah/ubah/hapus, cari, dan filter kategori).
- Fitur transaksi penyewaan, data penyewa, pengembalian, login, dan laporan bulanan
  belum termasuk dalam prototype dasar ini.
- Untuk mengulang data awal, tutup aplikasi lalu hapus file inh ara.db dan jalankan
  kembali streamlit run app.py.
