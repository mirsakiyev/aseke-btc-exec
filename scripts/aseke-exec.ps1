# Canonical launcher: runs the CLI with the APPROVED project runtime (.venv-exec), never another
# Python. Example: .\scripts\aseke-exec.ps1 status
# Works from any current directory (relative --config/--data-dir paths resolve against the
# repository root) and with paths containing spaces; the CLI's exit code is propagated.
# The Python side enforces the runtime policy again (runtime_policy.py), so a direct
# `python -m aseke_exec` on another interpreter cannot bypass it.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$lock = Get-Content -Raw -LiteralPath (Join-Path $root "config\runtime-lock.json") | ConvertFrom-Json
$venv = Join-Path $root ($lock.paths.venv_dir -replace '/', '\')
$python = Join-Path $venv "Scripts\python.exe"
$marker = Join-Path $venv "aseke-venv.json"
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $marker)) {
    [Console]::Error.WriteLine("refused: the approved runtime $venv is missing or unverified. " +
        "Run: powershell -ExecutionPolicy Bypass -File `"$root\scripts\setup.ps1`" (no fallback to another Python).")
    exit 4
}
Push-Location -LiteralPath $root
try {
    & $python -I -m aseke_exec @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
