@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -m venv .venv
    if errorlevel 1 goto failed
)

".venv\Scripts\python.exe" -c "import streamlit, altair, PIL" >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)

".venv\Scripts\python.exe" -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501
if errorlevel 1 goto failed
exit /b 0

:failed
echo Gagal menjalankan aplikasi. Pastikan Python tersedia dan koneksi internet aktif saat instalasi pertama.
pause
exit /b 1
