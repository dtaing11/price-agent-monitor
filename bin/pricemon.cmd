@echo off
rem Run the agent from anywhere on Windows:
rem   add this bin\ directory to your PATH, then use `pricemon ...`
setlocal
set "PROJECT=%~dp0.."
cd /d "%PROJECT%"
if "%PRICEMON_PYTHON%"=="" set "PRICEMON_PYTHON=python"
"%PRICEMON_PYTHON%" -m pricemon %*
endlocal
