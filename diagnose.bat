@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Kuji GUI - Diagnose
set "DIAG=%~dp0diagnose.log"
>"%DIAG%" echo === Diagnostics ===
>>"%DIAG%" echo Date: %DATE% %TIME%
>>"%DIAG%" echo Directory: %CD%
>>"%DIAG%" echo.
>>"%DIAG%" echo === py ===
py -3 --version >>"%DIAG%" 2>&1
>>"%DIAG%" echo === python ===
python --version >>"%DIAG%" 2>&1
>>"%DIAG%" echo === python3 ===
python3 --version >>"%DIAG%" 2>&1
>>"%DIAG%" echo === where ===
where py >>"%DIAG%" 2>&1
where python >>"%DIAG%" 2>&1
>>"%DIAG%" echo === .venv ===
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" --version >>"%DIAG%" 2>&1
    ".venv\Scripts\python.exe" -m pip show selenium >>"%DIAG%" 2>&1
) else (
    >>"%DIAG%" echo No .venv Python found
)
>>"%DIAG%" echo === files ===
if exist requirements.txt (>>"%DIAG%" echo requirements.txt OK) else (>>"%DIAG%" echo requirements.txt MISSING)
if exist kuji_gui.py (>>"%DIAG%" echo kuji_gui.py OK) else (>>"%DIAG%" echo kuji_gui.py MISSING)
echo Saved diagnostics to: "%DIAG%"
echo Attach diagnose.log and install.log if setup keeps failing.
pause
