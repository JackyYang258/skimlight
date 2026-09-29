#!/bin/sh
# Linux / WSL 下的本地程序启动脚本（测试用 Chromium、或 Windows 通过 wsl.exe 调用）
DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec "$DIR/.venv/bin/python" -u "$DIR/host/skimlight_host.py"
