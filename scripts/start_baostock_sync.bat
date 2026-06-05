@echo off
cd /d "%~dp0.."
:loop
echo %date% %time% [watchdog] start>> data\baostock_sync.log
python -u scripts\run_baostock_sync_loop.py --batch-size 100 --stocks-per-round 300 --round-timeout 900
echo %date% %time% [watchdog] restart>> data\baostock_sync.log
timeout /t 10 /nobreak >nul
goto loop
