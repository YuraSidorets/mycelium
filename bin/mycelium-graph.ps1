#requires -Version 7.5
$ErrorActionPreference = "Stop"
$forwardedArguments = @($args)
$python = $null
$pythonPrefix = @()
foreach ($candidate in @(
    @{ Name = "python"; Prefix = @() },
    @{ Name = "python3"; Prefix = @() },
    @{ Name = "py"; Prefix = @("-3") }
)) {
    $command = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $command) { continue }
    $prefix = @($candidate.Prefix)
    & $command.Source @prefix -B -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
        $python = $command
        $pythonPrefix = $prefix
        break
    }
}
if (-not $python) {
    throw "Mycelium Atlas requires Python 3.9 or newer. Tried python, python3, and py -3."
}

& $python.Source @pythonPrefix -B (Join-Path $PSScriptRoot "mycelium_graph.py") @forwardedArguments
exit $LASTEXITCODE
