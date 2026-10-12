#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
echo "登录浏览器安装完成。可以启动同步器。"
