@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Jarvis.ps1" -Action logs
exit /b %errorlevel%
