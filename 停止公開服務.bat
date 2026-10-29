@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title PowerQuery TW - 停止公開服務
pushd "%~dp0" >nul 2>&1
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\public_offline_service.ps1" -Mode stop
set "RESULT=%ERRORLEVEL%"
popd
pause
exit /b %RESULT%
