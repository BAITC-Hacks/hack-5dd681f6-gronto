@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Could not create a Python environment. Install a complete Python 3.11-3.13 distribution.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
  echo pip is missing in .venv. Trying to restore it with ensurepip...
  ".venv\Scripts\python.exe" -m ensurepip --upgrade
  if errorlevel 1 goto :pip_error
  ".venv\Scripts\python.exe" -m pip --version >nul 2>&1
  if errorlevel 1 goto :pip_error
)
if not exist ".venv\installed.flag" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Review the pip error above.
    pause
    exit /b 1
  )
  type nul > ".venv\installed.flag"
)
".venv\Scripts\python.exe" -m streamlit run app.py
pause
exit /b %errorlevel%

:pip_error
echo This Python installation cannot restore pip. Install the full Python distribution with pip,
echo then remove the .venv folder and run this file again.
pause
exit /b 1
