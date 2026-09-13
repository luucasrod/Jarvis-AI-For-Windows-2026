@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Jarvis.ps1" -Action restart
exit /b %errorlevel%
