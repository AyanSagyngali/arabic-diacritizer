@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ISTORIK VIDEO FACTORY
if not exist .venv\.install_ok (
  where py >nul 2>nul && (py -3 install.py) || (python install.py)
)
if not exist .venv\.install_ok exit /b 1
start "" .venv\Scripts\pythonw.exe run.py %*
