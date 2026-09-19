@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.release-work\test-deps" set "PYTHONPATH=%~dp0.release-work\test-deps;%PYTHONPATH%"
if defined CAMERA_PYTHON (
  "%CAMERA_PYTHON%" camera_playback.py %*
) else (
  python camera_playback.py %*
)
if errorlevel 1 (
  echo.
  echo Echec du lancement. Conservez ce message pour le diagnostic.
  pause
)
endlocal
