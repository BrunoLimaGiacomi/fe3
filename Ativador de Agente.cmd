@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "BOOTSTRAP=%APPDATA%\AgenteGlobal\launch.ps1"
set "AGENT_ROOT="

rem Dentro do repositorio, use primeiro a copia ao lado deste ativador.
rem Overrides invalidos sao ignorados e nao bloqueiam os fallbacks locais.
if exist "%~dp0AgenteGlobal\bin\agenteglobal.cmd" for %%I in ("%~dp0.") do set "AGENT_ROOT=%%~fI"
if not defined AGENT_ROOT if defined AGENTEGLOBAL_HOME if exist "%AGENTEGLOBAL_HOME%\AgenteGlobal\bin\agenteglobal.cmd" for %%I in ("%AGENTEGLOBAL_HOME%") do set "AGENT_ROOT=%%~fI"

for %%I in ("%CD%" "%CD%\.." "%CD%\..\.." "%CD%\..\..\.." "%CD%\..\..\..\.." "%CD%\..\..\..\..\.." "%CD%\..\..\..\..\..\..") do if not defined AGENT_ROOT if exist "%%~fI\AgenteGlobal\bin\agenteglobal.cmd" set "AGENT_ROOT=%%~fI"

if defined AGENT_ROOT goto launch_local

rem Fora do repositorio, o bootstrap le o caminho UTF-8 sem passar o valor pelo
rem parser/code page do CMD. setup.ps1 instala e atualiza esse arquivo.
if exist "%BOOTSTRAP%" goto launch_bootstrap

echo AgenteGlobal nao foi localizado automaticamente. 1>&2
echo Execute setup.ps1 uma vez na raiz atual do repositorio. 1>&2
pause
exit /b 2

:launch_local
call "%AGENT_ROOT%\AgenteGlobal\bin\agenteglobal.cmd" --workspace "%CD%" %*
set "AGENT_EXIT=%ERRORLEVEL%"
if not "%AGENT_EXIT%"=="0" pause
exit /b %AGENT_EXIT%

:launch_bootstrap
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%BOOTSTRAP%" %*
set "AGENT_EXIT=%ERRORLEVEL%"
if not "%AGENT_EXIT%"=="0" pause
exit /b %AGENT_EXIT%
