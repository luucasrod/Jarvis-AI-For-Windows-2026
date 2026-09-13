@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Jarvis.ps1" -Action status
exit /b %errorlevel%
