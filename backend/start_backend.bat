@echo off
rem EMANAGER Dialer backend: double-click to start.
rem First run creates .venv, installs everything (CPU only) and opens .env for the keys.
setlocal
cd /d "%~dp0"
title EMANAGER Dialer - serwer

if not exist ".venv\Scripts\python.exe" (
  echo Tworzenie srodowiska Python w backend\.venv ...
  py -3.12 -m venv .venv 2>/dev/null || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Nie znaleziono Pythona 3.11+. Zainstaluj go z python.org i zaznacz "Add python.exe to PATH".
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Instalacja nie powiodla sie, szczegoly powyzej.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Uzupelnij GEMINI_API_KEY, AUTH_TOKEN i DATABASE_URL w oknie Notatnika, zapisz i zamknij go.
  notepad ".env"
)

echo Serwer startuje. Klient laczy sie z adresem localhost (albo IP tego komputera w sieci).
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
