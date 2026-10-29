@echo off
REM Launch The Last Caretaker save editor GUI.
setlocal
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "D:\Python\python.exe" set "PY=D:\Python\python.exe"
if not defined PY if exist "D:\Python312\python.exe" set "PY=D:\Python312\python.exe"
if not defined PY if exist "D:\Python310\python.exe" set "PY=D:\Python310\python.exe"
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
if not defined PY where py >nul 2>nul && set "PY=py"

if not defined PY (
    echo Could not find a Python interpreter.
    pause
    exit /b 1
)

"%PY%" last_caretaker_save_gui.py %*
if errorlevel 1 pause
endlocal
