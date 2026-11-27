# Builds the Dilmaç TV app without Gradle (aapt2 + javac + d8 + apksigner).
# Output: ..\dilmac-tv.apk
param(
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$BuildTools = "34.0.0",
    [string]$Platform = "android-34"
)
$ErrorActionPreference = "Stop"

function Invoke-Tool {
    param([string]$Exe, [string[]]$ToolArgs)
    & $Exe @ToolArgs
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($Exe)) failed (exit code $LASTEXITCODE)" }
}

# Java: JAVA_HOME, PATH or the Temurin install folder
if (-not $env:JAVA_HOME -or -not (Test-Path "$env:JAVA_HOME\bin\javac.exe")) {
    $javac = Get-Command javac -ErrorAction SilentlyContinue
    if ($javac) { $env:JAVA_HOME = Split-Path (Split-Path $javac.Source) }
    else {
        $jdk = Get-ChildItem "$env:ProgramFiles\Eclipse Adoptium" -Directory -Filter "jdk-17*" -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $jdk) { throw "JDK 17 not found." }
        $env:JAVA_HOME = $jdk.FullName
    }
}
$env:Path = "$env:JAVA_HOME\bin;$env:Path"

$bt = "$Sdk\build-tools\$BuildTools"
$jar = "$Sdk\platforms\$Platform\android.jar"
foreach ($p in $bt, $jar) { if (-not (Test-Path $p)) { throw "Android SDK component missing: $p" } }

# Android tools can fail on non-ASCII paths (e.g. 'Altyazı'); build in an ASCII folder.
$work = Join-Path $env:LOCALAPPDATA "dilmac-build"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Path $work, "$work\res\drawable", "$work\classes", "$work\gen", "$work\dex" | Out-Null
Copy-Item "$PSScriptRoot\AndroidManifest.xml" $work
Copy-Item "$PSScriptRoot\src" $work -Recurse

# Make the icon and the TV home screen banner
Add-Type -AssemblyName System.Drawing
function New-Png([string]$Path, [int]$W, [int]$H, [string]$Text, [float]$Size) {
    $bmp = New-Object System.Drawing.Bitmap $W, $H
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::FromArgb(255, 0, 70, 160))
    $font = New-Object System.Drawing.Font "Segoe UI", $Size, ([System.Drawing.FontStyle]::Bold)
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = "Center"; $fmt.LineAlignment = "Center"
    $g.DrawString($Text, $font, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF 0, 0, $W, $H), $fmt)
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
}
New-Png "$work\res\drawable\banner.png" 320 180 "Dilmaç" 30
New-Png "$work\res\drawable\icon.png" 192 192 "TR" 64

Write-Host "1/5 resources"
Invoke-Tool "$bt\aapt2.exe" @("compile", "--dir", "$work\res", "-o", "$work\res.zip")
Invoke-Tool "$bt\aapt2.exe" @("link", "-o", "$work\base.apk", "-I", $jar, "--manifest", "$work\AndroidManifest.xml",
    "--java", "$work\gen", "$work\res.zip")

Write-Host "2/5 java"
$sources = @(Get-ChildItem -Recurse "$work\src", "$work\gen" -Filter *.java | ForEach-Object { $_.FullName })
Invoke-Tool "$env:JAVA_HOME\bin\javac.exe" (@("-encoding", "UTF-8", "-source", "8", "-target", "8", "-nowarn",
    "-Xlint:-options", "-bootclasspath", $jar, "-d", "$work\classes") + $sources)

Write-Host "3/5 dex"
$classes = @(Get-ChildItem -Recurse "$work\classes" -Filter *.class | ForEach-Object { $_.FullName })
Invoke-Tool "$bt\d8.bat" (@("--lib", $jar, "--min-api", "26", "--release", "--output", "$work\dex") + $classes)

Write-Host "4/5 package"
Copy-Item "$work\dex\classes.dex" "$work\classes.dex"
Push-Location $work
try { Invoke-Tool "$bt\aapt.exe" @("add", "base.apk", "classes.dex") | Out-Null } finally { Pop-Location }
Invoke-Tool "$bt\zipalign.exe" @("-f", "-p", "4", "$work\base.apk", "$work\aligned.apk")

Write-Host "5/5 sign"
# The same key is kept so later versions can be installed over the old one on the TV.
$ks = Join-Path $env:LOCALAPPDATA "dilmac-debug.keystore"
if (-not (Test-Path $ks)) {
    Invoke-Tool "$env:JAVA_HOME\bin\keytool.exe" @("-genkeypair", "-keystore", $ks, "-storepass", "android",
        "-keypass", "android", "-alias", "debug", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
        "-dname", "CN=Dilmac")
}
Invoke-Tool "$bt\apksigner.bat" @("sign", "--ks", $ks, "--ks-pass", "pass:android", "--key-pass", "pass:android",
    "--out", "$work\dilmac-tv.apk", "$work\aligned.apk")

$out = Join-Path (Split-Path $PSScriptRoot) "dilmac-tv.apk"
Copy-Item "$work\dilmac-tv.apk" $out -Force
Write-Host "Done: $out"
