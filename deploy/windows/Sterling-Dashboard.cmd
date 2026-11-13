@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sterling-dashboard.ps1" %*
set CODE=%ERRORLEVEL%
if not "%CODE%"=="0" (
  echo.
  echo The dashboard was not updated. Read the messages above.
  echo A failed fetch is marked OFFLINE / STALE. A failed check keeps the last good page.
  pause
)
exit /b %CODE%
