@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
title PowerQuery TW - 公開離線服務
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto :failure

echo ============================================================
echo   PowerQuery TW 公開離線服務
echo ============================================================
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\public_offline_service.ps1" -Mode start
if errorlevel 1 goto :failure

echo.
echo 已完成。上方 HTTPS 網址可提供給其他人使用。
popd
pause
exit /b 0

:failure
echo.
echo 啟動失敗，請查看 logs\public-offline-launcher.log。
popd
pause
exit /b 1
