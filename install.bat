@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Kuji GUI - Dependency Installer

set "LOG=%~dp0install.log"
>"%LOG%" echo === Kuji GUI installer ===
>>"%LOG%" echo Directory: %CD%

echo =============================================
echo Kuji GUI - Setup for Windows

echo =============================================
echo.

if not exist "requirements.txt" (
    echo [ERROR] Missing requirements.txt
    echo Unzip ALL files into a normal folder first.
    goto :failed
)
if not exist "kuji_gui.py" (
    echo [ERROR] Missing kuji_gui.py
    echo Unzip ALL files into a normal folder first.
    goto :failed
)

echo [1/4] Finding Python 3.10+ ...
call :find_python
if errorlevel 1 (
    echo [ERROR] Python 3.10+ not detected.
    echo Get Python from https://www.python.org/downloads/windows/
    echo If installed, close this window and rerun after enabling Python in PATH.
    >>"%LOG%" echo Python 3.10+ not found in py, python, python3.
    goto :failed
)

echo Using: %PYTHON_CMD%
%PYTHON_CMD% --version
%PYTHON_CMD% --version >>"%LOG%" 2>&1

echo.
echo [2/4] Creating a private Python environment ...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv ".venv" >>"%LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Could not create .venv
        echo Try extracting this folder under your Desktop or Documents.
        goto :failed
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Missing .venv Python after setup.
    goto :failed
)

echo.
echo [3/4] Checking pip ...
".venv\Scripts\python.exe" -m pip --version >>"%LOG%" 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m ensurepip --upgrade >>"%LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Cannot enable pip in .venv
        goto :failed
    )
)

echo.
echo [4/4] Installing Selenium. Internet is required ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --prefer-binary -r "requirements.txt" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    echo Check your internet, corporate proxy, or antivirus rules.
    goto :failed
)

".venv\Scripts\python.exe" -c "import selenium, tkinter; print('Selenium', selenium.__version__, '- Tkinter OK')" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Validation failed. See install.log.
    goto :failed
)

echo.
echo [SUCCESS] All dependencies installed.
echo Next: Double click start_gui.bat
>>"%LOG%" echo Installation completed successfully.
echo.
pause
exit /b 0

:find_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    exit /b 0
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    exit /b 0
)
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python3"
    exit /b 0
)
exit /b 1

:failed
echo.
echo SETUP FAILED. Please send a screenshot and install.log.
echo Log: "%LOG%"
echo.
pause
exit /b 1
