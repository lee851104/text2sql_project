@echo off
rem 把這個檔案的捷徑放進「啟動」資料夾（Win+R 輸入 shell:startup），登入後就會開始收集。
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
start "PowerQuery TW - 即時收集" /min "%~dp0即時收集啟動.bat"
exit /b 0
