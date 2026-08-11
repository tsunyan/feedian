@echo off
setlocal

pushd "%~dp0"
python -m feedian --config config.json %*
set "exit_code=%ERRORLEVEL%"
popd

exit /b %exit_code%
