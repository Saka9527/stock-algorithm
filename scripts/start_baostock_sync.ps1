# BaoStock 同步外层守护：子进程退出后自动重启（独立于 Cursor 终端）
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Log = Join-Path $Root "data\baostock_sync.log"
$watchdogPid = $PID
$watchdogPid | Out-File -FilePath (Join-Path $Root "data\baostock_sync.pid") -Encoding ascii

while ($true) {
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    Add-Content -Path $Log -Value "$ts [watchdog] 启动 run_baostock_sync_loop.py" -Encoding UTF8
    python -u scripts/run_baostock_sync_loop.py --batch-size 100 --stocks-per-round 300 --round-timeout 900
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    Add-Content -Path $Log -Value "$ts [watchdog] 循环退出，10s 后重启" -Encoding UTF8
    Start-Sleep -Seconds 10
}
