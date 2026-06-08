@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause
    exit /b 1
)

echo === baostock factor_data_wide extras single run ===
python -u scripts\sync_baostock_factor_extras.py --batch-size 80
if errorlevel 1 (
    echo [ERROR] sync failed
    pause
    exit /b 1
)
pause
