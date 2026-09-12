@echo off
REM Autoriser l'exécution des scripts PowerShell pour la session courante
powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-ExecutionPolicy Bypass -Scope Process -Force"

REM Exécuter le script PowerShell dans le même dossier que ce .bat
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0System_preparation.ps1"

pause
