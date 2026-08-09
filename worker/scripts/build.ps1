$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
python (Join-Path $PSScriptRoot "build.py")
