@echo off
REM Stage 1: the minimal version (move + two-finger hold + pinch click). Q or Esc quits.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" minimal.py
if errorlevel 1 pause
