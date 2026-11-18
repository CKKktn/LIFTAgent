@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\bootstrap.py %*
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python tools\bootstrap.py %*
  ) else (
    py tools\bootstrap.py %*
  )
)
set "app_result=%errorlevel%"
if not "%app_result%"=="0" pause
exit /b %app_result%
