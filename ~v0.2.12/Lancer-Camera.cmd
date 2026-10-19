@echo off
setlocal
title Camera - v0.2.12
cd /d "%~dp0"
if not defined CAMERA_PYTHON set "CAMERA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if not exist "%CAMERA_PYTHON%" (
  echo Python not found: "%CAMERA_PYTHON%"
  echo Set CAMERA_PYTHON to your interpreter path.
  pause
  exit /b 1
)
echo Camera - v0.2.12
echo Folder: %CD%
echo.
"%CAMERA_PYTHON%" -u camera_viewer.py
if errorlevel 1 (
  echo.
  echo The application encountered an error. Keep this message for diagnostics.
  pause
)
endlocal
