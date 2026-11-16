# ASEKE BTC Exec - create the repository virtual environment and install (simulation only).
# Usage (from the repository root):  powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { & py -3.12 -m venv .venv } else { & python -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw "failed to create .venv (Python 3.12+ required)" }

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

& .\.venv\Scripts\python.exe -m aseke_exec validate-config
Write-Host "Setup complete. Run commands with: .\scripts\aseke-exec.ps1 <command>"
