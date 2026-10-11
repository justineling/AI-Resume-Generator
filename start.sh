#!/usr/bin/env bash
# AI Resume Generator —— 一键启动（macOS / Linux）
set -euo pipefail

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo
    echo "  [错误] 未找到 Python。"
    echo "  请先安装 Python 3.11 或更高版本：https://www.python.org/downloads/"
    echo
    exit 1
fi

exec "$PY" ./start.py "$@"
