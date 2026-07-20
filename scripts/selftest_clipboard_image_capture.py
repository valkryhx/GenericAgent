# Set clipboard to a PNG then capture with GA clipboardImage helper.
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMG = REPO / "截图" / "图2.png"
assert IMG.is_file(), IMG

# Avoid $env expansion issues: pass path as arg
ps = r"""
param([string]$Path)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($Path)
[System.Windows.Forms.Clipboard]::SetImage($img)
$img.Dispose()
if ([System.Windows.Forms.Clipboard]::ContainsImage()) { Write-Output 'HAS_IMAGE'; exit 0 }
Write-Output 'NO_IMAGE'
exit 2
"""
r = subprocess.run(
    ["powershell", "-STA", "-NoProfile", "-NonInteractive", "-Command", ps, "-Path", str(IMG)],
    capture_output=True,
    text=True,
)
print("set_clipboard:", r.returncode, r.stdout.strip(), (r.stderr or "")[:400])

# capture via node
node = subprocess.run(
    ["npx", "tsx", "-e", "import { captureClipboardImage } from './src/clipboardImage.ts'; console.log(JSON.stringify(captureClipboardImage()))"],
    cwd=str(REPO / "frontends" / "ink-ui"),
    capture_output=True,
    text=True,
)
print("capture stdout:", node.stdout.strip())
print("capture stderr:", (node.stderr or "")[-500:])
print("capture code:", node.returncode)
