#!/usr/bin/env bash
# 删除注册表项与 Windows 端文件（Chrome 中的扩展需在 chrome://extensions 手动移除）。
set -euo pipefail
WIN_USER="$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r')"
reg.exe delete "HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\com.skimlight.host" /f > /dev/null 2>&1 || true
rm -rf "/mnt/c/Users/$WIN_USER/AppData/Local/Skimlight"
echo "已卸载本地程序注册与文件。"
