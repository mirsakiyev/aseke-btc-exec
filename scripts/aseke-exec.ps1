# Thin wrapper: runs the CLI with the repository virtual environment.
# Example: .\scripts\aseke-exec.ps1 status
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "missing .venv - run .\scripts\setup.ps1 first" }
Push-Location $root
try {
    & $python -m aseke_exec @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
