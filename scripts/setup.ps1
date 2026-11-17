# ASEKE BTC Exec - provision the APPROVED project-private runtime (simulation only).
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1            # provision / verify
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Check     # verify only, no changes
#
# What it does (pinned by config\runtime-lock.json and requirements\dev-cp315-win_amd64.lock):
#   1. obtains the pinned python.org CPython full-layout zip into .exec-runtime\downloads\...,
#      verifies its SHA-256 against the PINNED digest (and, when online, against python.org's
#      install-manager index), then verifies the Authenticode signature (Python Software
#      Foundation) of every executable/DLL/extension before anything is executed;
#   2. extracts into a staging directory, probes the staged interpreter (Python version, LOADED
#      SQLite version and source id), and only then renames it to .exec-runtime\cpython-...;
#   3. creates the side-by-side virtual environment .venv-exec at its FINAL path, installs the
#      hash-locked dev dependencies (wheels only) and the project (editable, no build isolation);
#   4. verifies the venv with `python -I -m aseke_exec runtime-info` (runtime_policy.operational)
#      and writes .venv-exec\aseke-venv.json LAST. The wrapper refuses a venv without it.
#
# It never touches the old .venv, var\, runtime roots, global/shared Python, PATH, the registry,
# the py launcher or persistent environment variables. Environment variables it sets are
# process-scoped and restored on exit. A second run with everything in place changes nothing.
[CmdletBinding()]
param(
    [switch]$Check,
    [string]$ArchivePath,          # use an already downloaded copy of the pinned zip (offline)
    [string]$WheelDir,             # use an existing wheelhouse (offline); hashes still enforced
    [ValidateSet("", "after-extract", "after-venv")]
    [string]$TestFailAfter = ""    # test hook: simulate an interrupted setup (only ever stops early)
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0

# Windows PowerShell 5.1 launched from PowerShell 7 inherits pwsh's PSModulePath, so autoloading
# Get-FileHash / Expand-Archive picks the incompatible 7.x modules and fails ("not recognized").
# Load the inbox modules by explicit path from $PSHOME instead; PSModulePath is left untouched.
if ($PSVersionTable.PSEdition -eq "Desktop") {
    foreach ($m in "Microsoft.PowerShell.Utility", "Microsoft.PowerShell.Security", "Microsoft.PowerShell.Archive") {
        Import-Module -Name (Join-Path $PSHOME "Modules\$m") -ErrorAction Stop
    }
}

$root = Split-Path -Parent $PSScriptRoot
$lockFile = Join-Path $root "config\runtime-lock.json"
$lock = Get-Content -Raw -LiteralPath $lockFile | ConvertFrom-Json
$lockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $lockFile).Hash.ToLowerInvariant()
function P([string]$rel) { return (Join-Path $root ($rel -replace '/', '\')) }
$interp = P $lock.paths.interpreter_dir
$venv = P $lock.paths.venv_dir
$downloads = P $lock.paths.downloads_dir
$wheels = if ($WheelDir) { $WheelDir } else { P $lock.paths.wheel_dir }
$pipCache = P $lock.paths.pip_cache_dir
$reqLock = P $lock.dependencies_lock
$venvPy = Join-Path $venv "Scripts\python.exe"
$marker = Join-Path $venv "aseke-venv.json"
$interpMarker = Join-Path $interp "aseke-provenance.json"

function Say([string]$m) { Write-Host "[setup] $m" }
function Fail([string]$m) { throw "[setup] FAILED: $m" }

function Test-Signatures([string]$dir) {
    $allowed = $lock.artifact.unsigned_allowed_prefix
    $subject = $lock.artifact.authenticode_signer_subject
    $bad = @()
    $n = 0
    # explicit extension filter: Windows PowerShell 5.1 ignores -Include together with -LiteralPath
    $pe = Get-ChildItem -LiteralPath $dir -Recurse -File | Where-Object { @(".exe", ".dll", ".pyd") -contains $_.Extension.ToLowerInvariant() }
    foreach ($f in $pe) {
        $rel = $f.FullName.Substring($dir.Length).TrimStart('\')
        $sig = Get-AuthenticodeSignature -LiteralPath $f.FullName
        $n++
        if ($sig.Status -eq "Valid") {
            $s = $sig.SignerCertificate.Subject
            if ($s -ne $subject -and $s -notlike "CN=Microsoft*") { $bad += "$rel (unexpected signer $s)" }
        } elseif (-not $rel.StartsWith($allowed)) {
            $bad += "$rel ($($sig.Status))"
        }
    }
    foreach ($must in @("python.exe", "DLLs\sqlite3.dll", "DLLs\_sqlite3.pyd")) {
        $sig = Get-AuthenticodeSignature -LiteralPath (Join-Path $dir $must)
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -ne $subject) { $bad += "$must not PSF-signed" }
    }
    if ($bad.Count) { Fail ("Authenticode verification failed: " + ($bad -join "; ")) }
    Say "Authenticode: $n PE files checked; python.exe, sqlite3.dll, _sqlite3.pyd signed by the PSF"
}

function Invoke-Probe([string]$py) {
    $code = "import json,sqlite3,struct,sys,sysconfig;c=sqlite3.connect(':memory:');v,sid=c.execute('select sqlite_version(),sqlite_source_id()').fetchone();print(json.dumps({'python':'%d.%d.%d'%sys.version_info[:3],'impl':sys.implementation.name,'bits':struct.calcsize('P')*8,'ft':bool(sysconfig.get_config_var('Py_GIL_DISABLED')),'sqlite':v,'source_id':sid,'base_prefix':sys.base_prefix}))"
    $out = & $py -I -c $code
    if ($LASTEXITCODE -ne 0) { Fail "version probe failed for $py" }
    return ($out | ConvertFrom-Json)
}

function Assert-Probe($pr, [string]$what) {
    $problems = @()
    if ($pr.python -ne $lock.python.version) { $problems += "python $($pr.python)" }
    if ($pr.impl -ne $lock.python.implementation) { $problems += "implementation $($pr.impl)" }
    if ($pr.bits -ne 64) { $problems += "$($pr.bits)-bit" }
    if ([bool]$pr.ft -ne [bool]$lock.python.free_threaded) { $problems += "free-threaded=$($pr.ft)" }
    if ($pr.sqlite -ne $lock.sqlite.version) { $problems += "sqlite $($pr.sqlite)" }
    if ($pr.source_id -ne $lock.sqlite.source_id) { $problems += "sqlite source id $($pr.source_id)" }
    if ($problems.Count) { Fail "$what does not match the pinned build: $($problems -join ', ')" }
    Say "$what OK: Python $($pr.python) / loaded SQLite $($pr.sqlite) ($($pr.source_id))"
}

function Test-VenvReady {
    if (-not (Test-Path -LiteralPath $venvPy) -or -not (Test-Path -LiteralPath $marker)) { return $false }
    try { $m = Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json } catch { return $false }
    if ($m.approved_build_id -ne $lock.approved_build_id -or $m.runtime_lock_sha256 -ne $lockHash) { return $false }
    $req = (Get-FileHash -Algorithm SHA256 -LiteralPath $reqLock).Hash.ToLowerInvariant()
    if ($m.dependencies_lock_sha256 -ne $req) { return $false }
    $ErrorActionPreference = "Continue"   # native stderr must not terminate (Windows PowerShell 5.1)
    $info = & $venvPy -I -m aseke_exec runtime-info 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    try { $j = ($info -join "`n") | ConvertFrom-Json } catch { return $false }
    return [bool]$j.runtime_policy.operational
}

# Process-scoped settings only; restored in finally.
$saved = @{}
foreach ($k in "PIP_CACHE_DIR", "PIP_DISABLE_PIP_VERSION_CHECK", "PIP_NO_INPUT", "PIP_CONFIG_FILE",
               "PYTHONNOUSERSITE", "PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
               "PIP_FIND_LINKS", "PIP_REQUIRE_VIRTUALENV") {
    $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
}
function Invoke-Setup {
    $env:PIP_CACHE_DIR = $pipCache
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
    $env:PIP_NO_INPUT = "1"
    $env:PIP_CONFIG_FILE = "NUL"        # ignore user/global pip configuration
    $env:PYTHONNOUSERSITE = "1"
    foreach ($k in "PYTHONPATH", "PYTHONHOME", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS") {
        Remove-Item "Env:\$k" -ErrorAction SilentlyContinue
    }

    if (Test-VenvReady) {
        Say "already provisioned and verified: $venvPy ($($lock.approved_build_id)); nothing changed"
        return
    }
    if ($Check) { Fail "runtime not provisioned/verified (run setup without -Check)" }

    # ---- 1+2. interpreter
    if (-not (Test-Path -LiteralPath $interpMarker)) {
        if (Test-Path -LiteralPath $interp) {
            Fail "$interp exists without aseke-provenance.json (unexpected content); inspect it manually - setup never deletes it"
        }
        New-Item -ItemType Directory -Force -Path $downloads | Out-Null
        $zip = if ($ArchivePath) { $ArchivePath } else { Join-Path $downloads $lock.artifact.name }
        if (-not (Test-Path -LiteralPath $zip)) {
            Say "downloading $($lock.artifact.url)"
            $part = "$zip.part"
            Invoke-WebRequest -UseBasicParsing -Uri $lock.artifact.url -OutFile $part
            Move-Item -LiteralPath $part -Destination $zip -Force
        }
        $h = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        if ($h -ne $lock.artifact.sha256) { Fail "SHA-256 mismatch for $zip ($h); the file is left in place for inspection" }
        Say "SHA-256 matches the pinned digest $h"
        if (-not $ArchivePath) {
            try {
                $idx = Invoke-RestMethod -UseBasicParsing -Uri $lock.artifact.index_url
                $e = $idx.versions | Where-Object { $_.id -eq $lock.artifact.index_id -and $_.'sort-version' -eq $lock.python.version } | Select-Object -First 1
                if ($null -eq $e) { Say "WARNING: python.org index has no $($lock.artifact.index_id) $($lock.python.version) entry (pinned digest + Authenticode still enforced)" }
                elseif ($e.hash.sha256 -ne $h) { Fail "python.org index digest $($e.hash.sha256) != archive $h" }
                else { Say "python.org install-manager index digest matches" }
            } catch {
                if ($_.Exception.Message -like "*FAILED*") { throw }
                Say "WARNING: python.org index not reachable; relying on the pinned digest + Authenticode"
            }
        }
        $stage = Join-Path (Split-Path -Parent $interp) ("staging-" + [guid]::NewGuid().ToString("N").Substring(0, 12))
        # Windows PowerShell 5.1 / .NET Framework without long-path support: directories must stay
        # under 248 chars and files under 260, or Expand-Archive fails midway with a misleading
        # cleanup error. Check up front against both the staging and the final interpreter path.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $za = [System.IO.Compression.ZipFile]::OpenRead($zip)
        try { $longest = ($za.Entries | ForEach-Object { $_.FullName.Length } | Measure-Object -Maximum).Maximum }
        finally { $za.Dispose() }
        $base = [Math]::Max($stage.Length, $interp.Length) + 1
        if ($base + $longest -ge 248) {
            Fail ("install path too long for Windows path limits: '$stage' + longest archive entry ($longest chars) " +
                  "reaches $($base + $longest) chars (limit ~247 for directories). Use a shorter checkout path.")
        }
        Say "extracting to $stage"
        $renamed = $false
        try {
            Expand-Archive -LiteralPath $zip -DestinationPath $stage
            Test-Signatures $stage
            Assert-Probe (Invoke-Probe (Join-Path $stage "python.exe")) "staged interpreter"
            $prov = [ordered]@{
                approved_build_id = $lock.approved_build_id; artifact = $lock.artifact.name
                artifact_sha256 = $h; verified = "pinned sha256 + python.org index (when online) + Authenticode (PSF)"
                installed_at = (Get-Date).ToUniversalTime().ToString("o")
            }
            ($prov | ConvertTo-Json) | Set-Content -LiteralPath (Join-Path $stage "aseke-provenance.json") -Encoding UTF8
            Rename-Item -LiteralPath $stage -NewName (Split-Path -Leaf $interp)   # atomic on one volume
            $renamed = $true
        } finally {
            # only THIS run's own staging directory is ever removed (never an installed runtime)
            if (-not $renamed -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
        }
        Say "interpreter installed at $interp"
    } else {
        Say "interpreter present: $interp"
        Test-Signatures $interp
    }
    $basePy = Join-Path $interp "python.exe"
    Assert-Probe (Invoke-Probe $basePy) "project interpreter"
    if ($TestFailAfter -eq "after-extract") { Fail "simulated interruption after extract (test hook)" }

    # ---- 3. side-by-side venv at its FINAL path
    if (Test-Path -LiteralPath $venv) {
        $cfgFile = Join-Path $venv "pyvenv.cfg"
        $home_ = ""
        if (Test-Path -LiteralPath $cfgFile) {
            $mm = Select-String -LiteralPath $cfgFile -Pattern '^home\s*=\s*(.+)$' | Select-Object -First 1
            if ($mm) { $home_ = $mm.Matches[0].Groups[1].Value.Trim() }
        }
        if ($home_ -and ((Resolve-Path -LiteralPath $home_).Path -eq (Resolve-Path -LiteralPath $interp).Path)) {
            Say "removing an incomplete/unverified $venv built from the project interpreter (no data inside)"
            Remove-Item -LiteralPath $venv -Recurse -Force
        } else {
            Fail "$venv exists but was not created from $interp (home=$home_); inspect it manually - setup never deletes it"
        }
    }
    & $basePy -I -m venv $venv
    if ($LASTEXITCODE -ne 0) { Fail "venv creation failed" }
    Assert-Probe (Invoke-Probe $venvPy) "venv interpreter"
    if ($TestFailAfter -eq "after-venv") { Fail "simulated interruption after venv creation (test hook)" }

    if (-not $WheelDir) {
        New-Item -ItemType Directory -Force -Path $wheels | Out-Null
        & $venvPy -I -m pip download --only-binary=:all: --require-hashes --no-deps -r $reqLock -d $wheels
        if ($LASTEXITCODE -ne 0) { Fail "dependency download failed" }
    }
    & $venvPy -I -m pip install --no-index --find-links $wheels --only-binary=:all: --require-hashes --no-deps -r $reqLock
    if ($LASTEXITCODE -ne 0) { Fail "hash-locked dependency install failed" }
    & $venvPy -I -m pip install --no-index --no-deps --no-build-isolation -e $root
    if ($LASTEXITCODE -ne 0) { Fail "project install failed" }

    # ---- 4. verify the effective runtime, then write the marker LAST
    $info = & $venvPy -I -m aseke_exec runtime-info
    if ($LASTEXITCODE -ne 0) { Fail "runtime-info failed in the new venv" }
    $j = ($info -join "`n") | ConvertFrom-Json
    if (-not $j.runtime_policy.operational) { Fail ("runtime policy refuses the new venv: " + ($j.runtime_policy.reasons -join "; ")) }
    $m = [ordered]@{
        approved_build_id = $lock.approved_build_id; runtime_lock_sha256 = $lockHash
        dependencies_lock_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $reqLock).Hash.ToLowerInvariant()
        python_executable = $j.python_executable; sqlite_version = $j.sqlite_version
        sqlite_source_id = $j.sqlite_source_id; created_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    ($m | ConvertTo-Json) | Set-Content -LiteralPath $marker -Encoding UTF8
    Say "verified: $($j.python_executable) Python $($lock.python.version), SQLite $($j.sqlite_version) ($($j.wal_reset_bug))"
    Say "use: .\scripts\aseke-exec.ps1 <command>   (the old .venv was not modified)"
}

try {
    Invoke-Setup
} finally {
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process") }
}
