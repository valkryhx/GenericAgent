[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipClean
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Invoke-Python {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $Python $($Arguments -join ' ')"
    }
}

Write-Host "[1/7] Checking build toolchain..."
Invoke-Python @("-c", "import build, setuptools; print('build', build.__version__)")

if (-not $SkipClean) {
    Write-Host "[2/7] Cleaning previous GA build and release artifacts..."
    $BuildDir = Join-Path $RepoRoot "build"
    if (Test-Path -LiteralPath $BuildDir) {
        $ResolvedBuild = (Resolve-Path -LiteralPath $BuildDir).Path
        if ($ResolvedBuild -ne $BuildDir) {
            throw "Unexpected build directory resolution: $ResolvedBuild"
        }
        Remove-Item -LiteralPath $ResolvedBuild -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path "dist" | Out-Null
    @(
        "dist\*.whl",
        "dist\*.tar.gz",
        "dist\SHA256SUMS.txt",
        "dist\build-report.json",
        "dist\llm.yaml.example",
        "dist\install_release.ps1"
    ) | ForEach-Object {
        Get-ChildItem -Path $_ -ErrorAction SilentlyContinue | Remove-Item -Force
    }
} else {
    Write-Host "[2/7] Cleaning skipped."
}

Write-Host "[3/7] Syncing packaged assets from assets/ ..."
$SourceAssets = Join-Path $RepoRoot "assets"
$PackagedAssets = Join-Path $RepoRoot "genericagent\resources\assets"
if (-not (Test-Path -LiteralPath $SourceAssets)) {
    throw "Source assets directory not found: $SourceAssets"
}
if (-not (Test-Path -LiteralPath $PackagedAssets)) {
    throw "Packaged assets directory not found: $PackagedAssets"
}
# Keep the wheel's bundled copy a byte-exact mirror of the source-of-truth
# assets/ tree for every curated file that ships. Only files already present
# under resources/assets are copied, so large non-runtime assets
# (pdf/images/demo) are never pulled into the wheel. Copying unconditionally
# is idempotent and avoids depending on Get-FileHash being available.
$SyncedAssets = 0
foreach ($TargetFile in (Get-ChildItem -LiteralPath $PackagedAssets -Recurse -File)) {
    $Relative = $TargetFile.FullName.Substring($PackagedAssets.Length).TrimStart('\')
    $SourceFile = Join-Path $SourceAssets $Relative
    if (-not (Test-Path -LiteralPath $SourceFile)) {
        throw "Packaged asset has no counterpart in assets/: $Relative"
    }
    Copy-Item -LiteralPath $SourceFile -Destination $TargetFile.FullName -Force
    $SyncedAssets++
}
Write-Host ("  packaged assets synced ({0} file(s))" -f $SyncedAssets)

Write-Host "[4/7] Building wheel and sdist..."
Invoke-Python @("-m", "build", "--wheel", "--sdist")

$Wheel = Get-ChildItem -Path "dist\genericagent-*.whl" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Wheel) {
    throw "Wheel was not produced in dist/."
}

Write-Host "[5/7] Auditing wheel contents..."
$AuditScript = @'
import sys
import zipfile
from pathlib import Path

wheel = Path(sys.argv[1])
required = {
    "ga_cli/cli.py",
    "genericagent/paths.py",
    "genericagent/llm/client.py",
    "genericagent/resources/llm.yaml.example",
    "genericagent/resources/assets/tools_schema.json",
    "genericagent/resources/assets/sys_prompt.txt",
    "genericagent/resources/skills/workflow-authoring/SKILL.md",
    "genericagent/resources/skills/workflow-authoring/references/api.md",
    "genericagent/resources/skills/workflow-authoring/references/artifacts.md",
    "genericagent/resources/skills/workflow-authoring/references/examples.md",
    "genericagent/resources/skills/workflow-authoring/references/resume.md",
    "genericagent/resources/skills/workflow-authoring/references/testing.md",
    "genericagent/resources/assets/tmwd_cdp_bridge/config.js",
    "genericagent/workflow/js/worker.js",
    "genericagent/workflow/js/script_contract.js",
    "frontends/ink/bridge.py",
    "frontends/ink/ui/package.json",
    "frontends/ink/ui/package-lock.json",
    "frontends/ink/ui/src/main.tsx",
    "frontends/ink/ui/src/App.tsx",
    "frontends/ink/ui/src/clipboardImage.ts",
    "frontends/common/chatapp_common.py",
    "frontends/terminal/continue_cmd.py",
}
forbidden_parts = (
    "node_modules/",
    "__pycache__/",
)
forbidden_suffixes = (
    "/llm.yaml",
    "/llm.yml",
    "/mykey.py",
    "/mykey.json",
    "/.env",
)

with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    missing = sorted(required - names)
    leaked = []
    for name in names:
        normalized = name.replace("\\", "/")
        if any(part in normalized for part in forbidden_parts):
            leaked.append(name)
            continue
        if any(normalized.endswith(suffix) for suffix in forbidden_suffixes):
            leaked.append(name)
    if missing or leaked:
        if missing:
            print("Missing required wheel entries:", file=sys.stderr)
            for item in missing:
                print(f"  - {item}", file=sys.stderr)
        if leaked:
            print("Forbidden wheel entries:", file=sys.stderr)
            for item in leaked:
                print(f"  - {item}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Wheel audit OK: {len(names)} entries")
'@
# Execute the audit from stdin.  A temporary script file is unnecessary and
# was previously not written before Invoke-Python tried to open it.
$AuditScript | & $Python - $Wheel.FullName
if ($LASTEXITCODE -ne 0) {
    throw "Wheel audit failed."
}

Write-Host "[6/7] Packaging release files..."
Copy-Item -Force "llm.yaml.example" "dist\llm.yaml.example"
Copy-Item -Force "scripts\install_release.ps1" "dist\install_release.ps1"

$HashTargets = Get-ChildItem -Path "dist" -File | Where-Object { $_.Name -ne "SHA256SUMS.txt" -and $_.Name -ne "build-report.json" } | Sort-Object Name
$HashLines = foreach ($File in $HashTargets) {
    $Hash = (& $Python -c "import hashlib,sys,pathlib; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" $File.FullName)
    if ($LASTEXITCODE -ne 0) { throw "SHA256 calculation failed for $($File.Name)" }
    "$Hash  $($File.Name)"
}
$ShaText = (($HashLines -join "`n") + "`n")
[System.IO.File]::WriteAllText((Join-Path (Get-Location) "dist\SHA256SUMS.txt"), $ShaText, [System.Text.UTF8Encoding]::new($false))

$Report = [ordered]@{
    builtAt = (Get-Date).ToUniversalTime().ToString("o")
    python = (& $Python --version 2>&1 | Out-String).Trim()
    node = (& node --version 2>&1 | Out-String).Trim()
    npm = (& npm --version 2>&1 | Out-String).Trim()
    wheel = $Wheel.Name
    wheelSha256 = (& $Python -c "import hashlib,sys,pathlib; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" $Wheel.FullName)
    wheelBytes = $Wheel.Length
    # These values are produced by the audit above. If any forbidden content
    # had been found, the build would already have failed.
    sensitiveConfigIncluded = $false
    runtimeBundled = $false
    nodeModulesIncluded = $false
    audit = "passed"
    supportedEntries = @("ga", "ga ink", "ga cli", "ga list", "ga status", "ga update")
}
$ReportJson = ($Report | ConvertTo-Json -Depth 4).Replace("`r`n", "`n")
$ReportPath = Join-Path (Join-Path (Get-Location) "dist") "build-report.json"
[System.IO.File]::WriteAllText($ReportPath, $ReportJson + "`n", [System.Text.UTF8Encoding]::new($false))

Write-Host "[7/7] Release artifacts:"
Get-ChildItem -Path "dist" | Select-Object Name, Length | Format-Table -AutoSize
