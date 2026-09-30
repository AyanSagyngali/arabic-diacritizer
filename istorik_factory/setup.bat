@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ISTORIK VIDEO FACTORY: installation...
where py >nul 2>nul && (py -3 install.py) || (python install.py)
