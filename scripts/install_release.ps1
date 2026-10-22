[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Wheel,
    [string]$Python = "python",
    [switch]$SkipNode
)

$ErrorActionPreference = "Stop"
$WheelPath = (Resolve-Path -LiteralPath $Wheel).Path
$WheelDir = Split-Path -Parent $WheelPath
# Never let the caller's cwd shadow site-packages with a source checkout. The
# installer may legitimately be launched from the GA repository root.
Set-Location $WheelDir

function Resolve-PythonCommand {
    param([string]$Name)
    $resolved = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $resolved) {
        throw "Missing $Name. Install Python 3.10-3.13 and add it to PATH."
    }
    return $resolved.Source
}

function Require-Command {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing $Name. $Hint"
    }
}

Write-Host "[1/6] Checking user-prerequisite runtimes..."
$PythonExe = Resolve-PythonCommand $Python
if (-not $SkipNode) {
    Require-Command "node" "Install Node.js 22.x and add it to PATH."
    Require-Command "npm" "npm is included with Node.js; verify PATH."
}

$PythonVersion = & $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) { throw "Failed to query Python version." }
Write-Host "Python: $PythonVersion"
if (-not $SkipNode) {
    Write-Host "Node:   $(& node --version)"
    Write-Host "npm:    $(& npm --version)"
}

Write-Host "[2/6] Installing GenericAgent wheel..."
& $PythonExe -m pip install --upgrade --force-reinstall $WheelPath
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

Write-Host "[3/6] Installing Ink UI npm dependencies..."
# Resolve from the installed ga_cli package, not a bare `import frontends`:
# when run from the GA source checkout, that import picks the repository copy.
$SitePackages = & $PythonExe -c "import pathlib, ga_cli; print(pathlib.Path(ga_cli.__file__).resolve().parent.parent)"
if ($LASTEXITCODE -ne 0) { throw "Cannot locate installed ga_cli package." }
$UiDir = Join-Path $SitePackages "frontends\ink\ui"
if (-not (Test-Path (Join-Path $UiDir "package-lock.json"))) {
    throw "Installed Ink UI metadata missing at $UiDir"
}
if (-not $SkipNode) {
    & npm ci --prefix $UiDir
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
} else {
    Write-Host "npm install skipped by request."
}

Write-Host "[4/6] Checking console-script PATH..."
$ScriptsDir = & $PythonExe -c "import sysconfig; print(sysconfig.get_path('scripts'))"
$PathParts = ($env:PATH -split [System.IO.Path]::PathSeparator) | ForEach-Object { $_.TrimEnd('\') }
if ($PathParts -notcontains $ScriptsDir.TrimEnd('\')) {
    Write-Warning "Python Scripts is not on PATH: $ScriptsDir"
    Write-Host "Add this directory to PATH to run 'ga' directly."
}

Write-Host "[5/6] Creating user llm.yaml if needed..."
$GaExe = Join-Path $ScriptsDir "ga.exe"
if (Test-Path $GaExe) {
    & $GaExe --version
} else {
    & $PythonExe -m ga_cli --version
}
if ($LASTEXITCODE -ne 0) { throw "GA version smoke test failed." }
$ConfigPath = & $PythonExe -c "from genericagent import paths; print(paths.seed_llm_config() or (paths.user_config_dir() / 'llm.yaml'))"
if ($LASTEXITCODE -ne 0) { throw "GA post-install config smoke test failed." }
& $PythonExe -c "from genericagent import paths; paths.ensure_runtime_dirs(); print('User config:', paths.user_config_dir()); print('User data:  ', paths.user_data_dir())"
if ($LASTEXITCODE -ne 0) { throw "GA post-install runtime-directory smoke test failed." }

Write-Host "[6/6] Installed."
Write-Host "LLM config: $ConfigPath"
Write-Host "Launch:     ga"
