@echo off
setlocal
set "APP_DATA_DIR=%~dp0.local-data"
set "APP_HOST=127.0.0.1"
set "APP_PORT=8012"
call "%~dp0start.cmd" %*
exit /b %errorlevel%
