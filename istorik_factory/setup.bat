@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === ISTORIK VIDEO FACTORY: установка ===
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo Установите Python 3.10+ с python.org и запустите снова & pause & exit /b 1)
if not exist .venv (%PY% -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || (echo Ошибка установки зависимостей & pause & exit /b 1)
python -m playwright install chromium
where node >nul 2>nul || (echo Устанавливаю Node.js LTS - нужен для загрузки файлов в ChatCut & winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements)
where ffprobe >nul 2>nul || (echo Устанавливаю FFmpeg & winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements)
echo.
echo Готово. Запускайте ISTORIK_VIDEO_FACTORY.bat
pause
