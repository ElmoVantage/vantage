$dst = "$env:LOCALAPPDATA\Vantage"
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-Item "$PSScriptRoot\dist\Vantage" $dst -Recurse
Copy-Item "$PSScriptRoot\.env"       "$dst\.env"       -Force
Copy-Item "$PSScriptRoot\tracker.db" "$dst\tracker.db" -Force
if (Test-Path "$PSScriptRoot\sync_state.json") {
    Copy-Item "$PSScriptRoot\sync_state.json" "$dst\sync_state.json" -Force
}

$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$env:USERPROFILE\Desktop\Vantage.lnk")
$shortcut.TargetPath       = "$dst\Vantage.exe"
$shortcut.WorkingDirectory = $dst
$shortcut.IconLocation     = "$dst\Vantage.exe"
$shortcut.Description      = "Vantage"
$shortcut.Save()

Write-Host "Installed to $dst"
Write-Host "Shortcut created on Desktop - right-click it to pin to taskbar"
