@echo off
setlocal

set PYTHON_VERSION=3.11
set VENV_NAME=env
set REQUIREMENTS_PATH=bCNC\requirements.txt
set GIT_REPO_URL=https://github.com/shefeequebk/bCNC

echo ===============================
echo Checking for Git and updating code...
echo ===============================

REM Check if git is available
git --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Git is not installed or not in PATH.
    echo Please install Git from: https://git-scm.com/
    pause
    exit /b 1
)

REM Check if this is a git repository
git status >nul 2>&1
if errorlevel 1 (
    echo ❌ This directory is not a git repository.
    echo.
    echo Cloning repository from %GIT_REPO_URL%...
    git clone %GIT_REPO_URL% .
    if errorlevel 1 (
        echo ❌ Failed to clone repository.
        pause
        exit /b 1
    )
    echo ✅ Repository cloned successfully.
) else (
    echo ✅ Git repository found.
    
    REM Check if remote is correct
    git remote get-url origin >nul 2>&1
    if errorlevel 1 (
        echo Setting up remote origin...
        git remote add origin %GIT_REPO_URL%
    ) else (
        for /f "delims=" %%i in ('git remote get-url origin') do set "CURRENT_REMOTE=%%i"
        if not "%CURRENT_REMOTE%"=="%GIT_REPO_URL%" (
            echo Updating remote origin to correct URL...
            git remote set-url origin %GIT_REPO_URL%
        )
    )
    
    REM Fetch latest changes
    echo Fetching latest changes from remote...
    git fetch origin
    if errorlevel 1 (
        echo ⚠️ Warning: Failed to fetch from remote. Continuing with local code...
    ) else (
        echo ✅ Successfully fetched latest changes.
    )
    
    REM Check if there are local changes and discard them
    git diff --quiet
    if errorlevel 1 (
        echo ⚠️ Local changes detected. Discarding all local changes...
        git reset --hard HEAD
        git clean -fd
        echo ✅ Local changes discarded.
    )
    
    REM Pull latest changes
echo Pulling latest changes...
git pull origin master
if errorlevel 1 (
    echo ⚠️ Warning: Failed to pull latest changes. Using current local code.
) else (
    echo ✅ Successfully updated to latest code.
)
)

echo.
echo ===============================
echo Checking for Python %PYTHON_VERSION%
echo ===============================

REM First try the Python Launcher
for /f "delims=" %%i in ('py -%PYTHON_VERSION% -c "import sys; print(sys.executable)" 2^>nul') do (
    set "PYTHON_EXEC=%%i"
    goto :check_version
)

REM Fallback: try to find python3.11 explicitly
for /f "delims=" %%i in ('where python3.%PYTHON_VERSION:~2% 2^>nul') do (
    set "PYTHON_EXEC=%%i"
    goto :check_version
)

REM Final fallback: check 'python'
for /f "delims=" %%i in ('where python 2^>nul') do (
    set "PYTHON_EXEC=%%i"
    goto :check_version
)

echo ❌ Python %PYTHON_VERSION% is not installed.
echo.
echo Attempting to install Python %PYTHON_VERSION%...
echo.

REM Try to install Python using winget (Windows Package Manager)
winget --version >nul 2>&1
if not errorlevel 1 (
    echo Installing Python %PYTHON_VERSION% using winget...
    winget install Python.Python.%PYTHON_VERSION%
    if errorlevel 1 (
        echo ❌ Failed to install Python using winget.
        echo.
        echo Please install Python %PYTHON_VERSION% manually from:
        echo https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo ✅ Python %PYTHON_VERSION% installed successfully.
    echo.
    echo Refreshing PATH and checking installation...
    echo.
    
    REM Refresh environment variables
    echo Refreshing environment variables...
    
    REM Try refreshenv (Chocolatey)
    call refreshenv >nul 2>&1
    if errorlevel 1 (
        REM Try PowerShell method
        powershell -Command "& { $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User') }" >nul 2>&1
        if errorlevel 1 (
            echo ⚠️ Warning: Could not refresh environment automatically.
            echo Please restart your command prompt and run this script again.
            pause
            exit /b 1
        )
    )
    
    REM Try to find Python again
    for /f "delims=" %%i in ('py -%PYTHON_VERSION% -c "import sys; print(sys.executable)" 2^>nul') do (
        set "PYTHON_EXEC=%%i"
        goto :check_version
    )
    
    for /f "delims=" %%i in ('where python3.%PYTHON_VERSION:~2% 2^>nul') do (
        set "PYTHON_EXEC=%%i"
        goto :check_version
    )
    
    for /f "delims=" %%i in ('where python 2^>nul') do (
        set "PYTHON_EXEC=%%i"
        goto :check_version
    )
    
    echo ❌ Python installation completed but not found in PATH.
    echo Please restart your command prompt and run this script again.
    pause
    exit /b 1
) else (
    echo ❌ winget is not available. Please install Python %PYTHON_VERSION% manually.
    echo Download from: https://www.python.org/downloads/
    echo.
    echo After installation, restart this script.
    pause
    exit /b 1
)

:check_version
REM Remove quotes if any
set "PYTHON_EXEC=%PYTHON_EXEC:"=%"

REM Get version of the selected Python
for /f "tokens=2 delims= " %%v in ('"%PYTHON_EXEC%" --version 2^>nul') do (
    set "PY_VER=%%v"
)

echo Found Python version: %PY_VER%

echo %PY_VER% | findstr "^%PYTHON_VERSION%" >nul
if errorlevel 1 (
    echo ❌ Python %PYTHON_VERSION% is required. Found %PY_VER%.
    exit /b 1
)

echo ===============================
echo Creating virtual environment...
echo ===============================
"%PYTHON_EXEC%" -m venv %VENV_NAME%
if errorlevel 1 (
    echo ❌ Failed to create virtual environment.
    exit /b 1
)

echo ===============================
echo Activating virtual environment...
echo ===============================
call %VENV_NAME%\Scripts\activate.bat
if errorlevel 1 (
    echo ❌ Failed to activate virtual environment.
    exit /b 1
)

echo ===============================
echo Installing requirements...
echo ===============================
pip install --upgrade pip
pip install -r %REQUIREMENTS_PATH%
if errorlevel 1 (
    echo ❌ Failed to install requirements.
    exit /b 1
)

echo ✅ Environment setup complete!

echo.
echo ===============================
echo Git Status Summary
echo ===============================
echo Current branch: 
git branch --show-current
echo.
echo Latest commit:
git log --oneline -1
echo.
echo Status:
git status --porcelain
if errorlevel 1 (
    echo ✅ Working directory is clean.
) else (
    echo ⚠️ You have uncommitted changes.
)

echo.
echo ===============================
echo Setup Complete!
echo ===============================
echo To activate the environment, run: %VENV_NAME%\Scripts\activate.bat
echo To run bCNC, use: python bCNC\bCNC.py
echo.
pause
