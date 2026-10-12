@echo off
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv)
if errorlevel 1 goto fail
.venv\Scripts\python.exe -c "import streamlit, httpx, bs4, keyring, playwright, tzdata" >nul 2>&1
if errorlevel 1 .venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.showEmailPrompt false --browser.gatherUsageStats false
goto end
:fail
echo Please install Python 3.11+ or check the network connection.
pause
:end
