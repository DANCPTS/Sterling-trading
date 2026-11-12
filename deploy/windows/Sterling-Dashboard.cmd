@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sterling-dashboard.ps1" %*
exit /b %ERRORLEVEL%
