# Validates the mod and runs its tests: what a release, and every change, must pass.
#   powershell -ExecutionPolicy Bypass -File tools\check.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
& claude plugin validate $Root
if ($LASTEXITCODE -ne 0) { throw "claude plugin validate failed" }
& claude plugin test $Root
if ($LASTEXITCODE -ne 0) { throw "claude plugin test failed" }
& python -m py_compile (Get-ChildItem (Join-Path $Root "collector\*.py")).FullName (Join-Path $Root "tools\clean-archive.py") (Get-ChildItem (Join-Path $Root "skills\*\*.py")).FullName
if ($LASTEXITCODE -ne 0) { throw "Python scripts do not compile" }
& python -m unittest discover -q -s (Join-Path $Root "tests") -p "test_*.py"
if ($LASTEXITCODE -ne 0) { throw "collector tests failed" }
Write-Host "All checks passed." -ForegroundColor Green
