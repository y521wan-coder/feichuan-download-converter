[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ArtifactsRoot = Join-Path $ProjectRoot "artifacts"
$CandidateRoot = Join-Path $ArtifactsRoot "candidate"
$Stage = Join-Path $CandidateRoot "app"
$Release = Join-Path $ProjectRoot "release"
$WorkerRoot = Join-Path $ProjectRoot "worker"
$Dotnet = Join-Path $ProjectRoot "tools\dotnet\dotnet.exe"
$AppProject = Join-Path $ProjectRoot "src\AccessibleVideoToText.App\AccessibleVideoToText.App.csproj"
$WorkerExe = Join-Path $WorkerRoot "dist\feichuan-worker.exe"
$InstallerScript = Join-Path $ProjectRoot "installer\FeichuanDownloadConverter.iss"
$Installer = Join-Path $Release "飞船下载转换工具-Setup-1.0.exe"

function Assert-ChildPath([string]$Path, [string]$Parent) {
    $parentFull = [IO.Path]::GetFullPath($Parent).TrimEnd('\') + '\'
    $pathFull = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    if (-not $pathFull.StartsWith($parentFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify path outside project artifacts: $Path"
    }
}

function Find-InnoCompiler {
    $candidates = @(
        $env:INNO_SETUP_COMPILER,
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    foreach ($candidate in $candidates) {
        $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
        if (Test-Path -LiteralPath $expanded -PathType Leaf) {
            return (Resolve-Path -LiteralPath $expanded).Path
        }
    }
    throw "Inno Setup 6 was not found."
}

Assert-ChildPath $CandidateRoot $ArtifactsRoot
if (Test-Path -LiteralPath $CandidateRoot) {
    Remove-Item -LiteralPath $CandidateRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
New-Item -ItemType Directory -Path $Release -Force | Out-Null

& python (Join-Path $WorkerRoot "scripts\build_worker.py")
if ($LASTEXITCODE -ne 0) {
    throw "Headless worker build failed with exit code $LASTEXITCODE."
}

& $Dotnet publish $AppProject -c Release -r win-x64 --self-contained true --no-restore -o $Stage
if ($LASTEXITCODE -ne 0) {
    throw ".NET publish failed with exit code $LASTEXITCODE."
}

Copy-Item -LiteralPath $WorkerExe -Destination (Join-Path $Stage "feichuan-worker.exe")
Copy-Item -LiteralPath (Join-Path $WorkerRoot "tools") -Destination (Join-Path $Stage "tools") -Recurse
Copy-Item -LiteralPath (Join-Path $WorkerRoot "assets") -Destination (Join-Path $Stage "assets") -Recurse
New-Item -ItemType Directory -Path (Join-Path $Stage "licenses") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $WorkerRoot "licenses\FFmpeg-LGPL-3.0.txt") -Destination (Join-Path $Stage "licenses")
Copy-Item -LiteralPath (Join-Path $WorkerRoot "licenses\GNU-GPL-3.0.txt") -Destination (Join-Path $Stage "licenses")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "third-party\licenses\FFMpegCore-MIT.txt") -Destination (Join-Path $Stage "licenses")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "third-party\licenses\TencentCloudSDK-Apache-2.0.txt") -Destination (Join-Path $Stage "licenses")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "third-party\licenses\Tencent-QCloud-COS-MIT.txt") -Destination (Join-Path $Stage "licenses")
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.txt") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $ProjectRoot "docs\release\隐私说明.txt") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $ProjectRoot "docs\release\快捷键.txt") -Destination $Stage
Copy-Item -LiteralPath (Join-Path $ProjectRoot "docs\release\测试候选版说明.txt") -Destination $Stage

$required = @(
    "飞船下载转换工具.exe",
    "feichuan-worker.exe",
    "使用说明.txt",
    "tools\ffmpeg.exe",
    "tools\ffprobe.exe",
    "tools\yt-dlp.exe",
    "tools\SHA2-256SUMS",
    "assets\donation_qr.jpg",
    "THIRD_PARTY_NOTICES.txt"
)
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $Stage $relative) -PathType Leaf)) {
        throw "Missing candidate payload: $relative"
    }
}

$ffmpegInfo = (& (Join-Path $Stage "tools\ffmpeg.exe") -hide_banner -version 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "FFmpeg version audit failed."
}
if ($ffmpegInfo -match '(?i)--enable-(?:gpl|nonfree)(?:\s|$)') {
    throw "Candidate contains a GPL-enabled or nonfree FFmpeg build."
}

$manifestLines = [Collections.Generic.List[string]]::new()
$manifestLines.Add("飞船下载转换工具")
$manifestLines.Add("显示版本：1.0")
$manifestLines.Add("程序集版本：1.0.0.0")
$manifestLines.Add("构建类型：本地测试候选，未发布到服务器，未做 Authenticode 签名")
$manifestLines.Add("")
foreach ($file in Get-ChildItem -LiteralPath $Stage -Recurse -File | Sort-Object FullName) {
    if ($file.Name -eq "版本与校验.txt") {
        continue
    }
    $relative = $file.FullName.Substring($Stage.Length).TrimStart('\')
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
    $manifestLines.Add("$relative SHA-256：$hash")
}
[IO.File]::WriteAllLines(
    (Join-Path $Stage "版本与校验.txt"),
    $manifestLines,
    [Text.UTF8Encoding]::new($true)
)

$compiler = Find-InnoCompiler
& $compiler "/Qp" "/DSourceStage=$Stage" "/DInstallerOutputDir=$Release" $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "Installer was not produced: $Installer"
}
$installerHash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToUpperInvariant()
[IO.File]::WriteAllText(
    "$Installer.sha256",
    "$installerHash  $([IO.Path]::GetFileName($Installer))`r`n",
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Built local candidate: $Installer"
Write-Host "SHA-256: $installerHash"
