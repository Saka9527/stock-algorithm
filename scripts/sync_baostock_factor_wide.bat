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

echo === baostock factor_data_wide sync ===
python -u scripts\sync_baostock_factor_wide.py %*
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" echo [ERROR] exit code %ERR%
pause
exit /b %ERR%
