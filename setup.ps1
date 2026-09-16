[CmdletBinding()]
param(
    [switch]$CodeIntelligence,
    [switch]$DevelopmentVenv
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$requirements = Join-Path $repositoryRoot "requirements.txt"
$installationStateDirectory = Join-Path $env:APPDATA "AgenteGlobal"
$installationStateFile = Join-Path $installationStateDirectory "install-path.txt"
$bootstrapSource = Join-Path $repositoryRoot "AgenteGlobal\bin\launch.ps1"
$bootstrapDestination = Join-Path $installationStateDirectory "launch.ps1"
$nativeWorkspace = Join-Path $repositoryRoot "AgenteGlobal\WorkSpaceNativo"
$portableLauncher = Join-Path $repositoryRoot "Ativador de Agente.cmd"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher 'py' não foi encontrado. Instale Python 3.11+ x64 antes de executar este script."
}

& py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "O AgenteGlobal exige Python 3.11 ou superior."
}

& py -3 -m pip install --disable-pip-version-check --user --requirement $requirements
if ($LASTEXITCODE -ne 0) {
    throw "A instalação das dependências do AgenteGlobal falhou."
}
& py -3 -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "O Python do usuário possui dependências incompatíveis."
}
& py -3 -c "import agents, mcp, openai, pydantic, prompt_toolkit, rich, tree_sitter, tree_sitter_language_pack; print('Dependências do AgenteGlobal: OK')"
if ($LASTEXITCODE -ne 0) {
    throw "As dependências foram instaladas, mas não puderam ser importadas."
}
& py -3 -m pyright --version
if ($LASTEXITCODE -ne 0) {
    throw "O Pyright não pôde ser iniciado pelo Python do usuário."
}

if ($DevelopmentVenv) {
    $venvRoot = Join-Path $repositoryRoot ".venv"
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        & py -3 -m venv $venvRoot
    }
    & $venvPython -m pip install --disable-pip-version-check --requirement $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "A instalação no ambiente opcional de desenvolvimento falhou."
    }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "O ambiente opcional de desenvolvimento possui dependências incompatíveis."
    }
    Write-Output "Ambiente opcional de desenvolvimento pronto: $venvPython"
}

if ($CodeIntelligence) {
    Write-Warning "-CodeIntelligence não é mais necessário: o setup padrão já instala esse suporte."
}

New-Item -ItemType Directory -Path $nativeWorkspace -Force | Out-Null
New-Item -ItemType Directory -Path $installationStateDirectory -Force | Out-Null
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($installationStateFile, $repositoryRoot, $utf8WithoutBom)
Copy-Item -LiteralPath $bootstrapSource -Destination $bootstrapDestination -Force
& reg.exe add "HKCU\Software\AgenteGlobal" /v InstallPath /t REG_SZ /d $repositoryRoot /f 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Não foi possível registrar o caminho no HKCU; o ativador usará o estado UTF-8 em APPDATA."
}

$desktopDirectory = [Environment]::GetFolderPath("Desktop")
if ($desktopDirectory -and (Test-Path -LiteralPath $portableLauncher -PathType Leaf)) {
    Copy-Item -LiteralPath $portableLauncher -Destination (Join-Path $desktopDirectory "Ativador de Agente.cmd") -Force
    Write-Output "Ativador criado na Area de Trabalho."
}

Write-Output "AgenteGlobal instalado para o usuário atual. Não é necessário ativar venv."
