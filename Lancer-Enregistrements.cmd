@echo off
setlocal
cd /d "%~dp0"
if not defined CAMERA_PYTHON set "CAMERA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if not exist "%CAMERA_PYTHON%" (
  echo Python introuvable : "%CAMERA_PYTHON%"
  echo Definissez CAMERA_PYTHON avec le chemin de votre interpreteur.
  pause
  exit /b 1
)
"%CAMERA_PYTHON%" -u camera_playback.py %*
if errorlevel 1 (
  echo.
  echo Echec du lancement. Conservez ce message pour le diagnostic.
  pause
)
endlocal
