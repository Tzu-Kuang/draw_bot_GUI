@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Kuji GUI - Start
if not exist "kuji_gui.py" (
    echo [ERROR] Program file not found. Unzip ALL files first.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Dependencies are not installed yet.
    echo Run install.bat in this same folder first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "kuji_gui.py"
if errorlevel 1 (
    echo.
    echo [ERROR] GUI stopped with an error. Take a screenshot of this window.
    pause
    exit /b 1
)
exit /b 0
