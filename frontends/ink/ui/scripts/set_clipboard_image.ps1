param([string]$Path)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($Path)
[System.Windows.Forms.Clipboard]::SetImage($img)
$img.Dispose()
if ([System.Windows.Forms.Clipboard]::ContainsImage()) {
  Write-Output 'HAS'
  exit 0
}
Write-Output 'NO'
exit 2
