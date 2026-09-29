@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ISTORIK VIDEO FACTORY
if not exist .venv\Scripts\python.exe call setup.bat
call .venv\Scripts\activate.bat
python run.py %*
pause
