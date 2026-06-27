@echo off
echo ================================================
echo   ETL File Loader - Setup
echo ================================================
echo.

:: ── 1. Check Python ──────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo.
    echo Please install Python 3.9 or later from:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: Check "Add Python to PATH" during installation.
    echo Then re-run this installer.
    echo.
    pause
    exit /b 1
)
echo [1/4] Python found:
python --version
echo.

:: ── 2. Check ODBC Driver 17 ──────────────────────
reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server" >nul 2>&1
if errorlevel 1 (
    reg query "HKLM\SOFTWARE\WOW6432Node\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server" >nul 2>&1
)
if errorlevel 1 (
    echo [ERROR] ODBC Driver 17 for SQL Server not found.
    echo.
    echo Please download and install it from:
    echo   https://aka.ms/downloadmsodbcsql
    echo.
    echo Then re-run this installer.
    echo.
    pause
    exit /b 1
)
echo [2/4] ODBC Driver 17 for SQL Server found.
echo.

:: ── 3. Create virtual environment ────────────────
echo [3/4] Creating virtual environment...
if exist ".venv" (
    echo   .venv already exists, skipping creation.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)
echo.

:: ── 4. Install dependencies ───────────────────────
echo [4/4] Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo ================================================
echo   Setup complete!
echo   Double-click run.bat to launch the app.
echo ================================================
echo.
pause
