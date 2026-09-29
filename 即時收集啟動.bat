@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title PowerQuery TW - 即時收集

rem 從批次檔所在的專案目錄執行；pushd 也支援 UNC 路徑。
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto :project_directory_error

where uv >nul 2>&1
if errorlevel 1 goto :uv_missing

if not defined POWERQUERY_RESTART_DELAY set "POWERQUERY_RESTART_DELAY=60"

:collect
echo [realtime] Starting the collector. Press Ctrl+C in this window to stop it.
call uv run --no-sync python -m ingest.realtime run
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" goto :stopped
if "%RESULT%"=="3" goto :already_running
echo [realtime] The collector exited with code %RESULT%. Restarting in %POWERQUERY_RESTART_DELAY% seconds...
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "Start-Sleep -Seconds %POWERQUERY_RESTART_DELAY%"
goto :collect

:already_running
echo [realtime] Another collector is already running; this window will not start a second one.
goto :success

:stopped
echo [realtime] The collector has stopped.
goto :success

:uv_missing
echo [ERROR] uv was not found. Install uv, make sure uv.exe is on PATH, and try again.
goto :failure

:project_directory_error
echo [ERROR] Could not enter the project directory containing this launcher.
goto :failure_without_popd

:failure
popd

:failure_without_popd
if defined POWERQUERY_NO_PAUSE exit /b 1
pause
exit /b 1

:success
popd
if defined POWERQUERY_NO_PAUSE exit /b 0
pause
exit /b 0
