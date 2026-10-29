@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title PowerQuery TW - 停止即時收集
pushd "%~dp0" >nul 2>&1
call uv run --no-sync python -m ingest.realtime stop
set "RESULT=%ERRORLEVEL%"
popd
if defined POWERQUERY_NO_PAUSE exit /b %RESULT%
pause
exit /b %RESULT%
