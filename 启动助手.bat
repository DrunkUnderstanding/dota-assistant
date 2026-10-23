@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Dota 选人助手

where python >nul 2>nul
if errorlevel 1 (
  echo 没找到 Python。到 https://www.python.org/downloads/ 安装 3.11 或更高版本，安装时勾选 Add to PATH。
  pause
  exit /b 1
)

python -c "import requests, numpy, cv2, mss, anthropic" >nul 2>nul
if errorlevel 1 (
  echo 第一次使用，安装 Python 依赖…
  python -m pip install -r requirements.txt
)

python -X utf8 -m dota_assistant start
if errorlevel 1 pause
