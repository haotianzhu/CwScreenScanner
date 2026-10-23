$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# Prefer the project-local Python runtime when one was installed for this build.
$localPython = Join-Path $PSScriptRoot 'build_deps\python\python.exe'
if (Test-Path -LiteralPath $localPython) { $env:MARKET_PYTHON = $localPython }

. (Join-Path $root 'resolve_python.ps1')
$python = Get-MarketPython
$templates = Join-Path $root 'tools\market_bot_templates'
$windivert = Join-Path $root 'vendor\windivert\WinDivert-2.2.2-A\x64'
$windivertDll = Join-Path $windivert 'WinDivert.dll'
$windivertDriver = Join-Path $windivert 'WinDivert64.sys'
$output = Join-Path $PSScriptRoot 'dist'
New-Item -ItemType Directory -Path $output -Force | Out-Null

if (-not (Test-Path -LiteralPath $windivertDll) -or -not (Test-Path -LiteralPath $windivertDriver)) {
    & (Join-Path $root 'fetch_windivert.ps1')
}
if (-not (Test-Path -LiteralPath $windivertDll) -or -not (Test-Path -LiteralPath $windivertDriver)) {
    throw 'WinDivert binaries are unavailable after fetch_windivert.ps1.'
}

$arguments = @(
    '--noconfirm', '--clean', '--onefile', '--windowed', '--uac-admin',
    '--name', 'MarketScreenScanner',
    '--distpath', $output,
    '--workpath', (Join-Path $PSScriptRoot 'build'),
    '--specpath', (Join-Path $PSScriptRoot 'build'),
    '--paths', $root,
    '--hidden-import', 'psutil',
    '--collect-all', 'cryptography',
    '--add-data', "$templates;tools\market_bot_templates",
    '--add-data', "$(Join-Path $root 'maple_crypto_constants.cs');.",
    '--add-binary', "$windivertDll;vendor\windivert\WinDivert-2.2.2-A\x64",
    '--add-data', "$windivertDriver;vendor\windivert\WinDivert-2.2.2-A\x64",
    (Join-Path $PSScriptRoot 'scanner_gui.py')
)
$buildLog = Join-Path $PSScriptRoot 'build\pyinstaller.log'
& $python -m PyInstaller @arguments *> $buildLog
$buildExitCode = $LASTEXITCODE
if (Test-Path -LiteralPath $buildLog) { Get-Content -LiteralPath $buildLog }
$exe = Join-Path $output 'MarketScreenScanner.exe'
if ($buildExitCode -and $buildExitCode -ne 0) { throw "MarketScreenScanner build failed (exit code $buildExitCode); see $buildLog" }
if (-not (Test-Path -LiteralPath $exe) -or (Get-Item -LiteralPath $exe).Length -lt 1MB) {
    throw "MarketScreenScanner build did not produce a valid executable; see $buildLog"
}
Write-Host "Built: $exe"
