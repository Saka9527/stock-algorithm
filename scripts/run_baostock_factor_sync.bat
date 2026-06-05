@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause
    exit /b 1
)

if not exist data mkdir data

if not exist data\.factor_wide_schema_v1 (
    echo === migrate factor_data_wide schema ===
    python scripts\migrate_factor_data_wide_schema.py
    if errorlevel 1 (
        echo [ERROR] migrate failed
        pause
        exit /b 1
    )
    echo ok> data\.factor_wide_schema_v1
)

echo === baostock -^> factor_data_wide sync loop ===
echo log: data\baostock_factor_sync.log
python -u scripts\run_baostock_factor_sync_loop.py --batch-size 100 --stocks-per-round 300 --round-timeout 1800 --sleep 10
if errorlevel 1 (
    echo [ERROR] loop exited, see data\baostock_factor_sync.log
    pause
    exit /b 1
)
pause
