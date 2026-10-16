$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$IsAuto = ($args -contains '--auto') -or ($env:ALFRED_AUTO -match '^(1|true|yes|on)$')

if (($args -contains '--help') -or ($args -contains '-h')) {
    @'
Usage: .\install.ps1 [--auto] [--plain] [--customize] [--service] [--repair]
                     [--backend ollama|lmstudio|llamacpp|skip]
                     [--wheelhouse PATH] [--skip-models]
'@
    exit 0
}

function Find-AlfredPython {
    foreach ($Name in @('python', 'py')) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            $VersionArgs = if ($Name -eq 'py') { @('-3', '-c', 'import sys; print(sys.version_info >= (3, 10))') }
                           else { @('-c', 'import sys; print(sys.version_info >= (3, 10))') }
            $Probe = & $Command.Source @VersionArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $Probe -eq 'True') {
                $Prefix = if ($Name -eq 'py') { @('-3') } else { @() }
                return [pscustomobject]@{ Exe = $Command.Source; Prefix = $Prefix }
            }
        }
    }
    return $null
}

$Python = Find-AlfredPython
if ($Python) {
    $Prefix = $Python.Prefix
    & $Python.Exe @Prefix -m scripts.installer.core @args
    exit $LASTEXITCODE
}

$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    if ($IsAuto) {
        Write-Error 'Alfred needs Python 3.10+ or uv. Install uv, then rerun .\install.ps1 --auto.'
        exit 2
    }
    $Answer = Read-Host 'Python 3.10+ is missing. Download uv and Python 3.12 for your user? [Y/n]'
    if ($Answer -match '^(n|no)$') {
        Write-Error 'Install Python 3.10+ and rerun .\install.ps1.'
        exit 2
    }
    $Installer = Join-Path $env:TEMP 'alfred-install-uv.ps1'
    Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $Installer
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Installer
    Remove-Item $Installer -Force
    $UvPath = Join-Path $HOME '.local\bin\uv.exe'
    if (-not (Test-Path $UvPath)) { $UvPath = Join-Path $HOME '.cargo\bin\uv.exe' }
    if (-not (Test-Path $UvPath)) { throw 'uv installation failed. Open a new PowerShell window and retry.' }
    $Uv = [pscustomobject]@{ Source = $UvPath }
}

& $Uv.Source python install 3.12
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Uv.Source run --no-project --python 3.12 -- python -m scripts.installer.core @args
exit $LASTEXITCODE
