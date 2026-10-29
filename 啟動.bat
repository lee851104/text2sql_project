@echo off
setlocal EnableExtensions DisableDelayedExpansion
title PowerQuery TW

rem Always work from this launcher's directory. pushd also supports UNC paths.
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto :project_directory_error

echo ============================================================
echo   PowerQuery TW Web Launcher
echo ============================================================
echo.

set "POWERQUERY_URL=http://127.0.0.1:8765/"

rem Reuse an already healthy service before touching uv, the venv, or the database.
if defined POWERQUERY_SKIP_EXISTING_CHECK goto :prepare_project
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\launcher_health.ps1" check >nul 2>&1
if not errorlevel 1 goto :already_running

:prepare_project
where uv >nul 2>&1
if errorlevel 1 goto :uv_missing

set "NEED_SYNC=0"

rem Re-sync only when the virtual environment is absent or dependency files changed.
if not exist ".venv\Scripts\python.exe" set "NEED_SYNC=1"
if not exist ".venv\Scripts\powerquery.exe" set "NEED_SYNC=1"
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\launcher_dependency_state.ps1" check >nul 2>&1
if errorlevel 1 set "NEED_SYNC=1"

if "%NEED_SYNC%"=="1" goto :sync_dependencies
echo [1/4] Python environment is current; dependency sync skipped.
goto :initialize_directories

:sync_dependencies
echo [1/4] Synchronizing Python dependencies. The first run may take a while...
call uv sync --extra dev --extra online
if errorlevel 1 goto :sync_error
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\launcher_dependency_state.ps1" write >nul 2>&1
if errorlevel 1 echo [WARNING] Dependency stamp could not be saved; the next launch will sync again.

:initialize_directories
echo [2/4] Checking project directories...
call uv run --no-sync python -m project_tasks init-dirs >nul
if errorlevel 1 goto :directory_error

echo [3/4] Checking the query database...
if exist "data\processed\power.db" goto :database_ready
echo       power.db is missing; building it now...
call uv run --no-sync python -m ingest.build_db
if errorlevel 1 goto :database_error

:database_ready
echo [4/4] Starting http://127.0.0.1:8765/
echo       The browser will open after the health check succeeds.
echo       Press Ctrl+C in this window to stop the service.
echo.

rem Poll the real health endpoint in a hidden helper before opening the browser.
if defined POWERQUERY_SKIP_BROWSER goto :launch_server
start "" /b powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\launcher_health.ps1" wait-and-open >nul 2>&1

:launch_server
call uv run --no-sync powerquery --serve --host 127.0.0.1 --port 8765
if errorlevel 1 goto :server_error
goto :server_stopped

:already_running
echo [4/4] PowerQuery TW is already running. Opening the browser...
if defined POWERQUERY_SKIP_BROWSER goto :success
start "" "%POWERQUERY_URL%"
goto :success

:uv_missing
echo [ERROR] uv was not found.
echo         Install uv, make sure uv.exe is on PATH, and launch this file again.
goto :failure

:project_directory_error
echo [ERROR] Could not enter the project directory containing this launcher.
goto :failure_without_popd

:sync_error
echo.
echo [ERROR] Dependency synchronization failed.
echo         Check the network, available disk space, and pyproject.toml above.
goto :failure

:directory_error
echo.
echo [ERROR] Project directory initialization failed.
goto :failure

:database_error
echo.
echo [ERROR] power.db could not be built.
echo         Check the messages above and the Taipower source files.
goto :failure

:server_error
echo.
echo [ERROR] The web service stopped unexpectedly or could not start.
echo         If port 8765 is in use, close the program using that port and retry.
goto :failure

:server_stopped
echo.
echo PowerQuery TW has stopped.
goto :success

:failure
popd

:failure_without_popd
echo.
if defined POWERQUERY_NO_PAUSE exit /b 1
pause
exit /b 1

:success
popd
echo.
if defined POWERQUERY_NO_PAUSE exit /b 0
pause
exit /b 0
