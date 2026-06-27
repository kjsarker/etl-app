@echo off
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] App is not installed yet.
    echo Please double-click install.bat first.
    echo.
    pause
    exit /b 1
)
echo Starting ETL File Loader...
.venv\Scripts\python.exe -m streamlit run app.py
