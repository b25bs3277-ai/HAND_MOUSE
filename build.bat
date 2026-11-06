@echo off
REM ==========================================================
REM  Builds dist\HandMouse.exe : one file, no console window,
REM  works with NO internet (all models are packed inside).
REM  Run setup.bat once before this.
REM ==========================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"

python -m pip install "pyinstaller>=6.6"
if errorlevel 1 goto :fail

REM If you downloaded models\hand_landmarker.task (new mediapipe), pack it too.
set EXTRA=
if exist "models\hand_landmarker.task" set EXTRA=--add-data "models\hand_landmarker.task;models"

REM --collect-data mediapipe     : the built-in hand model files (.tflite, .binarypb)
REM --collect-binaries mediapipe : MediaPipe's DLLs (needed by new mediapipe versions)
REM --hidden-import pynput...    : pynput picks its Windows code at run time; tell PyInstaller about it
pyinstaller --noconfirm --clean --onefile --noconsole --name HandMouse ^
    --collect-data mediapipe ^
    --collect-binaries mediapipe ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput.mouse._win32 ^
    %EXTRA% ^
    main.py
if errorlevel 1 goto :fail

echo.
echo  Done:  dist\HandMouse.exe
echo  Test it OFFLINE: turn Wi-Fi off, then double-click dist\HandMouse.exe.
echo  settings.json and handmouse.log will appear next to the .exe.
pause
exit /b 0

:fail
echo.
echo  Build FAILED - see README.md section "Building the .exe".
pause
exit /b 1
