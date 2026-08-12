@echo off
setlocal

pushd "%~dp0"
set "feedian_python=%~dp0.venv\Scripts\python.exe"
if not exist "%feedian_python%" set "feedian_python=python"

for %%C in (init config status migrate sync reextract enrich-stars render run snapshot restore schedule ingest search) do (
    if /I "%~1"=="%%C" goto modern_command
)
if "%~1"=="-h" goto modern_command
if "%~1"=="--help" goto modern_command

"%feedian_python%" -m feedian --config config.json %*
goto command_finished

:modern_command
"%feedian_python%" -m feedian %*

:command_finished
set "exit_code=%ERRORLEVEL%"
popd

exit /b %exit_code%
