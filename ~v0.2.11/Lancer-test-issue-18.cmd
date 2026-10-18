@echo off
setlocal
cd /d "%~dp0"
echo Camera v0.2.11 - starting the main application.
call "%~dp0Lancer-Camera.cmd"
exit /b %errorlevel%
