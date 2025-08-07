@echo off
setlocal

REM === Configuration ===
set VENV_PATH=env
set ENTRY_COMMAND=python -m bCNC

echo ============================
echo 🔄 Activating virtual environment...
echo ============================
call "%VENV_PATH%\Scripts\activate.bat"

if errorlevel 1 (
    echo ❌ Failed to activate virtual environment.
    pause
    exit /b 1
)

echo ============================
echo 🚀 Running bCNC...
echo ============================
%ENTRY_COMMAND%

pause
