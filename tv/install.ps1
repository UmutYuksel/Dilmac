# Installs the APK on the Google TV over the network, grants the overlay permission and opens the app.
# Usage:  .\install.ps1 -TvIp 192.168.1.50
# If Google TV "Wireless debugging" asks for a pairing code, first:  adb pair <ip>:<pairing port>
param(
    [Parameter(Mandatory = $true)][string]$TvIp,
    [int]$Port = 5555,
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk"
)
$ErrorActionPreference = "Stop"
$adb = "$Sdk\platform-tools\adb.exe"
$apk = Join-Path (Split-Path $PSScriptRoot) "dilmac-tv.apk"
$pkg = "com.dilmac.tv"
if (-not (Test-Path $apk)) { throw "Build the APK with build.ps1 first." }

$target = if ($TvIp -match ":\d+$") { $TvIp } else { "${TvIp}:$Port" }
Write-Host "Connecting to the TV: $target  (accept the 'allow debugging' prompt on the TV if it appears)"
$ErrorActionPreference = "Continue"
& $adb connect $target | Out-Host
$state = ""
for ($i = 0; $i -lt 30; $i++) {
    $state = (& $adb -s $target get-state 2>$null | Out-String).Trim()
    if ($state -eq "device") { break }
    if ($i -eq 0) { Write-Host "Waiting for the prompt on the TV (60 s)..." }
    Start-Sleep -Seconds 2
    if ($i % 5 -eq 4) { & $adb connect $target | Out-Null }
}
$ErrorActionPreference = "Stop"
if ($state -ne "device") { throw "The TV did not connect. Check the prompt on the TV and run again." }

# The app was called "PS5 Altyazi" (com.ps5altyazi.tv) before; it also listens on 8787, so remove it.
$old = "com.ps5altyazi.tv"
if ((& $adb -s $target shell pm list packages $old | Out-String) -match "package:$([regex]::Escape($old))\s") {
    Write-Host "Removing the old app ($old)."
    & $adb -s $target uninstall $old | Out-Host
}
& $adb -s $target install -r $apk
if ($LASTEXITCODE -ne 0) { throw "Install failed." }
& $adb -s $target shell appops set $pkg SYSTEM_ALERT_WINDOW allow
& $adb -s $target shell am start -n "$pkg/.MainActivity" --ez test true
Write-Host "Installed. The 'Dilmaç' screen should open on the TV with a test subtitle."
