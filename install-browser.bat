@echo off
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 goto fail
echo Chromium installed.
goto end
:fail
echo Installation failed. Check Python version and network connection.
pause
:end
