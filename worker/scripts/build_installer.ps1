[CmdletBinding()]
param(
    [string]$Version,
    [string]$SignToolName = $env:FEICHUAN_INNO_SIGNTOOL_NAME,
    [switch]$AllowDevelopmentBuild,
    [switch]$AllowUnsignedStable,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Dist = Join-Path $Root "dist"
$Release = Join-Path $Root "release"
$InstallerScript = Join-Path $Root "installer\feichuan.iss"
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

function Find-InnoCompiler {
    if ($env:INNO_SETUP_COMPILER) {
        $configured = [Environment]::ExpandEnvironmentVariables($env:INNO_SETUP_COMPILER)
        if (Test-Path -LiteralPath $configured -PathType Leaf) {
            return (Resolve-Path -LiteralPath $configured).Path
        }
        throw "INNO_SETUP_COMPILER does not point to an existing ISCC.exe."
    }

    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    throw "Inno Setup 6 was not found. Set INNO_SETUP_COMPILER to ISCC.exe."
}

function Assert-ValidSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Formal release payload is not Authenticode-signed: $Path (status: $($signature.Status))."
    }
}

function Assert-ManifestHash(
    [string]$ManifestText,
    [string]$Path,
    [string]$RelativePath
) {
    $fullWidthColon = [string][char]0xFF1A
    $pattern = (
        '(?mi)^' +
        [regex]::Escape($RelativePath) +
        '\s+SHA-256' +
        [regex]::Escape($fullWidthColon) +
        '([0-9A-F]{64})\s*$'
    )
    $match = [regex]::Match($ManifestText, $pattern)
    if (-not $match.Success) {
        throw "The dist hash manifest has no valid SHA-256 entry for $RelativePath."
    }
    $expectedHash = $match.Groups[1].Value
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actualHash -ne $expectedHash) {
        throw "The dist hash manifest does not match $RelativePath. Rebuild the application first."
    }
}

if ($AllowDevelopmentBuild -and $AllowUnsignedStable) {
    throw "AllowDevelopmentBuild and AllowUnsignedStable are mutually exclusive."
}
$strictFormalRelease = -not $AllowDevelopmentBuild -and -not $AllowUnsignedStable

if (-not $Version) {
    $Version = Get-ConfiguredString "VERSION"
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Stable installer version must have the form X.Y.Z; got '$Version'."
}

$configuredVersion = Get-ConfiguredString "VERSION"
if ($Version -ne $configuredVersion) {
    throw "Installer version '$Version' does not match application VERSION '$configuredVersion'."
}

$AppName = Get-ConfiguredString "APP_NAME"
$appExePath = Join-Path $Dist "$AppName.exe"
$manifestCandidates = @()
foreach ($textFile in Get-ChildItem -LiteralPath $Dist -Filter "*.txt" -File) {
    if ($textFile.Name -eq "THIRD_PARTY_NOTICES.txt") {
        continue
    }
    $candidateContent = Get-Content -LiteralPath $textFile.FullName -Raw -Encoding UTF8
    if ($candidateContent.Contains($AppName) -and $candidateContent.Contains("SHA-256")) {
        $manifestCandidates += $textFile
    }
}
if ($manifestCandidates.Count -ne 1) {
    throw "Expected exactly one application hash manifest in dist; found $($manifestCandidates.Count)."
}
$manifestPath = $manifestCandidates[0].FullName

$requiredFiles = @(
    $appExePath,
    (Join-Path $Dist "THIRD_PARTY_NOTICES.txt"),
    $manifestPath,
    (Join-Path $Dist "licenses\FFmpeg-LGPL-3.0.txt"),
    (Join-Path $Dist "licenses\GNU-GPL-3.0.txt"),
    (Join-Path $Dist "assets\donation_qr.jpg"),
    (Join-Path $Dist "tools\yt-dlp.exe"),
    (Join-Path $Dist "tools\ffmpeg.exe"),
    (Join-Path $Dist "tools\ffprobe.exe"),
    (Join-Path $Dist "tools\SHA2-256SUMS")
)
foreach ($requiredFile in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Missing installer payload: $requiredFile"
    }
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8
$fullWidthColon = [string][char]0xFF1A
$versionLinePattern = '(?m)^[^\r\n]+' + [regex]::Escape($fullWidthColon + $Version) + '\s*$'
if ($manifest -notmatch $versionLinePattern) {
    throw "The dist hash manifest does not match version $Version. Rebuild the application first."
}

$manifestHashTargets = @(
    @{ Path = $appExePath; RelativePath = "$AppName.exe" },
    @{ Path = (Join-Path $Dist "THIRD_PARTY_NOTICES.txt"); RelativePath = "THIRD_PARTY_NOTICES.txt" },
    @{ Path = (Join-Path $Dist "licenses\FFmpeg-LGPL-3.0.txt"); RelativePath = "licenses\FFmpeg-LGPL-3.0.txt" },
    @{ Path = (Join-Path $Dist "licenses\GNU-GPL-3.0.txt"); RelativePath = "licenses\GNU-GPL-3.0.txt" },
    @{ Path = (Join-Path $Dist "assets\donation_qr.jpg"); RelativePath = "assets\donation_qr.jpg" },
    @{ Path = (Join-Path $Dist "tools\yt-dlp.exe"); RelativePath = "tools\yt-dlp.exe" },
    @{ Path = (Join-Path $Dist "tools\ffmpeg.exe"); RelativePath = "tools\ffmpeg.exe" },
    @{ Path = (Join-Path $Dist "tools\ffprobe.exe"); RelativePath = "tools\ffprobe.exe" },
    @{ Path = (Join-Path $Dist "tools\SHA2-256SUMS"); RelativePath = "tools\SHA2-256SUMS" }
)
foreach ($target in $manifestHashTargets) {
    Assert-ManifestHash $manifest $target.Path $target.RelativePath
}

if ($strictFormalRelease) {
    Assert-ValidSignature $appExePath
    if (-not $SignToolName) {
        throw "Set FEICHUAN_INNO_SIGNTOOL_NAME to a preconfigured Inno Setup sign-tool name for a formal release."
    }
}

if (-not $AllowDevelopmentBuild) {
    $ffmpegOutput = (& (Join-Path $Dist "tools\ffmpeg.exe") -hide_banner -version 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg did not return auditable version information."
    }
    if ($ffmpegOutput -match '(?i)--enable-(?:gpl|nonfree)(?:\s|$)') {
        throw "Stable release cannot use a GPL-enabled or nonfree FFmpeg payload. Replace and audit it first."
    }
}

if ($PreflightOnly) {
    Write-Host "Installer preflight passed for $AppName $Version."
    return
}

$compiler = Find-InnoCompiler
New-Item -ItemType Directory -Path $Release -Force | Out-Null

$versionParts = $Version.Split('.')
$versionInfoVersion = "$($versionParts[0]).$($versionParts[1]).$($versionParts[2]).0"
$compilerArguments = @(
    "/Qp",
    "/DMyAppName=$AppName",
    "/DMyAppVersion=$Version",
    "/DMyVersionInfoVersion=$versionInfoVersion",
    "/DSourceDist=$Dist",
    "/DInstallerOutputDir=$Release",
    "/DMyManifestPath=$manifestPath"
)
$limitationsPath = $null
if ($AllowUnsignedStable) {
    $limitationsPath = Join-Path $Release "release-limitations-$Version.txt"
    $encodedLimitations = @(
        "6aOe6Ii55LiL6L295bel5YW3IHswfSDlj5HluIPpmZDliLY=",
        "",
        "5q2k5a6J6KOF5YyF5ZKM5Li756iL5bqP5b2T5YmN5pyq5L2/55SoIEF1dGhlbnRpY29kZSDku6PnoIHnrb7lkI3vvIxXaW5kb3dzIOWPr+iDveaYvuekuuacquefpeWPkeW4g+iAheOAgg==",
        "5a6J6KOF5YyF5LuN5bey6YCa6L+H6YCQ5paH5Lu2IFNIQS0yNTYg5riF5Y2V5qCh6aqM77yM5bm25L2/55So57uP6L+H5a6h6K6h55qE6Z2eIEdQTCBGRm1wZWcg5p6E5bu644CC"
    )
    $limitations = @(
        $encodedLimitations |
            ForEach-Object {
                if ($_ -eq "") {
                    ""
                }
                else {
                    [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_))
                }
            }
    )
    $limitations[0] = [string]::Format($limitations[0], $Version)
    $limitations | Set-Content -LiteralPath $limitationsPath -Encoding UTF8
    $compilerArguments += "/DMyLimitationsPath=$limitationsPath"
    Write-Warning "Building an explicitly allowed unsigned stable installer."
}
if ($SignToolName) {
    $compilerArguments += "/DMySignToolName=$SignToolName"
}
$compilerArguments += $InstallerScript

Write-Host "Building installer for $Version with $compiler"
& $compiler @compilerArguments
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}

$installer = Join-Path $Release "$AppName-Setup-$Version.exe"
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
    throw "Inno Setup did not produce the expected installer: $installer"
}
if ($strictFormalRelease) {
    Assert-ValidSignature $installer
}

$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToUpperInvariant()
$hashFile = "$installer.sha256"
$hashLine = "$hash  $([IO.Path]::GetFileName($installer))"
[IO.File]::WriteAllText(
    $hashFile,
    $hashLine + [Environment]::NewLine,
    [Text.UTF8Encoding]::new($false)
)

Write-Host "Built: $installer"
Write-Host "SHA-256: $hash"
