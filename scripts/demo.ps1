# Reproducible demonstration in a FRESH isolated runtime root (no network, no API keys,
# simulation only). Never resets or reuses var\ or an existing runtime root.
$ErrorActionPreference = "Stop"
$exec = Join-Path $PSScriptRoot "aseke-exec.ps1"
$rt = "runtime\demo-" + (Get-Date -Format "yyyyMMddHHmmss")

function Run { & $exec @args; if ($LASTEXITCODE -ne 0) { throw "aseke-exec $args failed ($LASTEXITCODE)" } }
Run runtime-info
Run --data-dir $rt init-runtime
Run --data-dir $rt publish-example long --trade-id ("demo-long-" + (Get-Date -Format "yyyyMMddHHmmss"))
Run --data-dir $rt cycle
Run --data-dir $rt status
Run --data-dir $rt sim set-market --price 66100     # take-profit triggers on the simulated venues
Run --data-dir $rt cycle
Run --data-dir $rt status
Run scenario run all
Write-Host "demo runtime root: $rt"
