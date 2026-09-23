@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Install Python 3.11 or newer from python.org, then run this file again.
    pause
    exit /b 1
  )
)
if not exist ".venv\installed.flag" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Check your internet connection.
    pause
    exit /b 1
  )
  type nul > ".venv\installed.flag"
)
".venv\Scripts\python.exe" -m streamlit run app.py
pause
