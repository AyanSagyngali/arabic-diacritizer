#!/usr/bin/env bash
# ISTORIK VIDEO FACTORY — запуск (macOS / Linux)
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./setup.sh
. .venv/bin/activate
exec python run.py "$@"
