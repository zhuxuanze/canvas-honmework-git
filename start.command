#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "请先安装 Python 3.11 或更新版本：https://www.python.org/downloads/"
  read -r -p "按回车关闭"
  exit 1
fi
if [ ! -d .venv ]; then python3 -m venv .venv; fi
if ! .venv/bin/python -c 'import streamlit, httpx, bs4, keyring, playwright, tzdata' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.showEmailPrompt false --browser.gatherUsageStats false
