[CmdletBinding()]
param(
    [string]$Version
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConfigPath = Join-Path $Root "src\feichuan_downloader\config.py"

function Get-ConfiguredString([string]$Name) {
    $text = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
    $pattern = '(?m)^' + [regex]::Escape($Name) + '\s*=\s*"([^"]+)"\s*$'
    $match = [regex]::Match($text, $pattern)
    if (-not $match.Success) {
        throw "Could not read $Name from src\feichuan_downloader\config.py."
    }
    return $match.Groups[1].Value
}

if (-not $Version) {
    $Version = Get-ConfiguredString "VERSION"
}

$AppName = Get-ConfiguredString "APP_NAME"
$Source = Join-Path $Root "release\$AppName-Setup-$Version.exe"
$Destination = "D:\$AppName-Setup-$Version.exe"
$RootHashSidecar = "$Destination.sha256"

if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
    throw "Installer does not exist: $Source"
}

Copy-Item -LiteralPath $Source -Destination $Destination -Force
Remove-Item -LiteralPath $RootHashSidecar -Force -ErrorAction SilentlyContinue

Write-Host "Copied installer to $Destination"
Write-Host "D:\ root .sha256 sidecar is intentionally not generated."
