@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Kuji GUI - Local Demo Site
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run install.bat first.
    pause
    exit /b 1
)
echo Demo site: http://127.0.0.1:8765
".venv\Scripts\python.exe" "demo_site.py"
if errorlevel 1 pause
