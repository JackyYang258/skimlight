#!/usr/bin/env bash
# 在 WSL 中运行：把扩展复制到 Windows 目录，生成本地程序启动器，并在注册表中为 Chrome 注册 Native Messaging。
# 可重复运行（修改扩展代码后重新运行一次，再到 chrome://extensions 点「重新加载」）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="com.skimlight.host"
EXT_ID="$(cat "$ROOT/extension_id.txt")"
WIN_USER="$(cmd.exe /c 'echo %USERNAME%' 2>/dev/null | tr -d '\r')"
WIN_BASE="C:\\Users\\$WIN_USER\\AppData\\Local\\Skimlight"
LIN_BASE="/mnt/c/Users/$WIN_USER/AppData/Local/Skimlight"
DISTRO="${WSL_DISTRO_NAME:?需要在 WSL 中运行}"

mkdir -p "$LIN_BASE"
rm -rf "$LIN_BASE/extension"
cp -r "$ROOT/extension" "$LIN_BASE/extension"

# 启动器：Chrome 通过 cmd 运行 .bat，.bat 再调用 WSL 中的 Python 本地程序（标准输入输出直接透传）
printf '@echo off\r\nwsl.exe -d %s -e %s\r\n' "$DISTRO" "$ROOT/host/skimlight_host.sh" > "$LIN_BASE/skimlight_host.bat"

cat > "$LIN_BASE/$NAME.json" <<JSON
{
  "name": "$NAME",
  "description": "Skimlight local host",
  "path": "${WIN_BASE//\\/\\\\}\\\\skimlight_host.bat",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
JSON

reg.exe add "HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\$NAME" /ve /t REG_SZ /d "$WIN_BASE\\$NAME.json" /f > /dev/null

echo "已安装："
echo "  扩展目录     $WIN_BASE\\extension"
echo "  本地程序描述 $WIN_BASE\\$NAME.json"
echo "  扩展 ID      $EXT_ID"
echo
echo "下一步（只需一次）：Chrome 打开 chrome://extensions → 打开右上角「开发者模式」"
echo "→「加载已解压的扩展程序」→ 选择 $WIN_BASE\\extension"
