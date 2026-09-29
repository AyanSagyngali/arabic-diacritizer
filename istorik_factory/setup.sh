#!/usr/bin/env bash
# ISTORIK VIDEO FACTORY — установка (macOS / Linux)
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
command -v node >/dev/null || echo "⚠ Установите Node.js LTS (нужен для загрузки файлов в ChatCut): https://nodejs.org"
command -v ffprobe >/dev/null || echo "⚠ Установите FFmpeg (brew install ffmpeg / apt install ffmpeg)"
echo "Готово. Запуск: ./istorik.sh"
