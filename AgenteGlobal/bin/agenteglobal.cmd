@echo off
setlocal
set "AGENT_ROOT=%~dp0.."
for %%I in ("%AGENT_ROOT%") do set "AGENT_ROOT=%%~fI"

where py >nul 2>&1
if errorlevel 1 (
    echo Python 3.11 or newer was not found. Install Python and run setup.ps1. 1>&2
    exit /b 1
)

py -3 -c "import agents, mcp, openai, pydantic, prompt_toolkit, rich, tree_sitter, tree_sitter_language_pack" >nul 2>&1
if errorlevel 1 (
    echo AgenteGlobal dependencies are missing. Run setup.ps1 once. 1>&2
    exit /b 2
)

py -3 "%AGENT_ROOT%\AgenteGlobal.py" %*
exit /b %ERRORLEVEL%
