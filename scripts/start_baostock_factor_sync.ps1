# Start factor_data_wide sync loop (detached from IDE terminal)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Log = Join-Path $Root "data\baostock_factor_sync.log"
New-Item -ItemType Directory -Force -Path (Join-Path $Root "data") | Out-Null
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Error "python not found in PATH"; exit 1 }
Start-Process -FilePath $py -ArgumentList @(
    "-u", "scripts\run_baostock_factor_sync_loop.py",
    "--batch-size", "100",
    "--stocks-per-round", "300",
    "--round-timeout", "1800",
    "--sleep", "10"
) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $Log -RedirectStandardError "${Log}.err"
Write-Host "Started factor sync loop. Log: $Log"
