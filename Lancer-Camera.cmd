@echo off
setlocal
title Camera - v0.2.11-dev
cd /d "%~dp0"
if not defined CAMERA_PYTHON set "CAMERA_PYTHON=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if not exist "%CAMERA_PYTHON%" (
  echo Python introuvable : "%CAMERA_PYTHON%"
  echo Definissez CAMERA_PYTHON avec le chemin de votre interpreteur.
  pause
  exit /b 1
)
echo Camera - v0.2.11-dev
echo Dossier : %CD%
echo.
"%CAMERA_PYTHON%" -u camera_viewer.py
if errorlevel 1 (
  echo.
  echo Le programme a rencontre une erreur. Conservez ce message pour le diagnostic.
  pause
)
endlocal
