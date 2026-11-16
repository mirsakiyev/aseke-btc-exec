# Reproducible Phase 1 demonstration (no network, no API keys, simulation only).
# Resets ONLY this repository's simulated exchange; the executor journal keeps its history.
$ErrorActionPreference = "Stop"
$exec = Join-Path $PSScriptRoot "aseke-exec.ps1"

& $exec validate-config
& $exec sim init --reset
& $exec publish-example long --trade-id ("demo-long-" + (Get-Date -Format "yyyyMMddHHmmss"))
& $exec cycle
& $exec status
& $exec sim set-market --price 66100     # take-profit triggers on the simulated venues
& $exec cycle
& $exec status
& $exec scenario run all
