[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DigestArguments
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$scriptPath = Join-Path $PSScriptRoot 'mycelium.py'
$launchers = @(
    @{ Command = 'python'; Prefix = @() },
    @{ Command = 'python3'; Prefix = @() },
    @{ Command = 'py'; Prefix = @('-3') }
)

$selected = $null
foreach ($launcher in $launchers) {
    $command = Get-Command $launcher.Command -ErrorAction SilentlyContinue
    if ($null -eq $command) { continue }
    try {
        & $command.Source @($launcher.Prefix) -B -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        $selected = @{ Path = $command.Source; Prefix = $launcher.Prefix }
        break
    }
    catch {
        continue
    }
}

if ($null -eq $selected) {
    Write-Error 'Mycelium reflections digest requires Python 3.9 or newer. Tried python, python3, and py -3.'
    exit 1
}

& $selected.Path @($selected.Prefix) -B $scriptPath digest @DigestArguments
exit $LASTEXITCODE
