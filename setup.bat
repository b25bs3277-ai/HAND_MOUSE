@echo off
REM ==========================================================
REM  HandMouse - one-time setup (needs internet ONCE)
REM  Creates a private Python environment in .venv and installs
REM  everything from requirements.txt.
REM ==========================================================
cd /d "%~dp0"

set "PY="
py -3.11 -c "import sys" >nul 2>nul && set "PY=py -3.11"
if not defined PY (
    py -3.10 -c "import sys" >nul 2>nul && set "PY=py -3.10"
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,10),(3,11)) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo  Python 3.11 or 3.10 was not found.
    echo  Install Python 3.11 64-bit from https://www.python.org/downloads/windows/
    echo  and tick "Add python.exe to PATH" during install. Then run setup.bat again.
    pause
    exit /b 1
)

echo Using: %PY%
%PY% -c "import struct, sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)"
if errorlevel 1 (
    echo  This Python is 32-bit. MediaPipe needs 64-bit Python. Install the 64-bit version.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

python -c "import mediapipe, cv2, pynput; print('OK  mediapipe', mediapipe.__version__, '  opencv', cv2.__version__)"
if errorlevel 1 goto :fail

echo.
echo  Setup finished.
echo    Stage 1 test : double-click stage1.bat
echo    Full app     : double-click run.bat
echo  After this, no internet is needed.
pause
exit /b 0

:fail
echo.
echo  Setup FAILED. Read the error above, then see README.md section "Common errors".
pause
exit /b 1
