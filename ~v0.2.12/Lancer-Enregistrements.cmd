@echo off
setlocal
cd /d "%~dp0"
if not defined CAMERA_PYTHON set "CAMERA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if not exist "%CAMERA_PYTHON%" (
  echo Python not found: "%CAMERA_PYTHON%"
  echo Set CAMERA_PYTHON to your interpreter path.
  pause
  exit /b 1
)
"%CAMERA_PYTHON%" -u camera_playback.py %*
if errorlevel 1 (
  echo.
  echo Launch failed. Keep this message for diagnostics.
  pause
)
endlocal
