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

echo === baostock -^> factor_data_wide extras sync loop ===
echo fields: float_cap, total_cap, is_st, is_suspended, turnover_20d
echo log: data\baostock_factor_extras_sync.log
python -u scripts\run_baostock_factor_extras_loop.py --batch-size 80 --stocks-per-round 200 --round-timeout 2400 --sleep 15
if errorlevel 1 (
    echo [ERROR] loop exited, see data\baostock_factor_extras_sync.log
    pause
    exit /b 1
)
pause
