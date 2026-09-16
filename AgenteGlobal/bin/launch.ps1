$ErrorActionPreference = "Stop"

$stateDirectory = Join-Path $env:APPDATA "AgenteGlobal"
$stateFile = Join-Path $stateDirectory "install-path.txt"
$candidates = [System.Collections.Generic.List[string]]::new()

if ($env:AGENTEGLOBAL_HOME) {
    $candidates.Add($env:AGENTEGLOBAL_HOME)
}
if (Test-Path -LiteralPath $stateFile -PathType Leaf) {
    $saved = [System.IO.File]::ReadAllText($stateFile, [System.Text.Encoding]::UTF8).Trim()
    if ($saved) {
        $candidates.Add($saved)
    }
}
try {
    $registered = (Get-ItemProperty -LiteralPath "HKCU:\Software\AgenteGlobal" -Name InstallPath -ErrorAction Stop).InstallPath
    if ($registered) {
        $candidates.Add([string]$registered)
    }
} catch {
    # Registry fallback is optional; the UTF-8 state file is canonical.
}

$repositoryRoot = $null
foreach ($candidate in $candidates) {
    try {
        $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
    } catch {
        continue
    }
    $candidateLauncher = Join-Path $resolved "AgenteGlobal\bin\agenteglobal.cmd"
    if (Test-Path -LiteralPath $candidateLauncher -PathType Leaf) {
        $repositoryRoot = $resolved
        break
    }
}

if (-not $repositoryRoot) {
    [Console]::Error.WriteLine("AgenteGlobal não foi localizado. Execute setup.ps1 na raiz atual do repositório.")
    exit 2
}

$launcher = Join-Path $repositoryRoot "AgenteGlobal\bin\agenteglobal.cmd"
# O Core resolve o workspace padrao a partir da propria distribuicao. Nao
# converta o CWD (inclusive a Area de Trabalho em um duplo clique) em escopo.
# @args preserva --workspace explicito e os demais argumentos do operador.
& $launcher @args
exit $LASTEXITCODE
