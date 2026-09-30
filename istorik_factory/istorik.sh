#!/usr/bin/env bash
# ISTORIK VIDEO FACTORY — запуск (macOS / Linux)
cd "$(dirname "$0")"
[ -f .venv/.install_ok ] || python3 install.py || exit 1
exec .venv/bin/python run.py "$@"
