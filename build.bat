@echo off
setlocal
cd /d "%~dp0"
uv add -r requirements.txt
pyinstaller --noconfirm --clean --onefile --windowed --uac-admin --name BootMark --icon=logo.ico --add-data "logo.ico;." bootmark.py
