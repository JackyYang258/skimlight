#!/usr/bin/env bash
# 在 Linux / WSL 上构建 Windows 安装包（不需要 Windows 上的 Python，也不依赖 WSL 运行）：
#   dist/Skimlight-<版本>-windows-x64.zip
# 包内：python\（Python 嵌入式版 + jieba、httpx）、app\（本地程序）、extension\、install.cmd、uninstall.cmd。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
PYVER="3.12.10"          # 嵌入式 Python 版本；依赖按 cp312 / win_amd64 下载
VERSION="$("$PY" -c "import json;print(json.load(open('$ROOT/extension/manifest.json'))['version'])")"
EXT_ID="$(cat "$ROOT/extension_id.txt")"
DIST="$ROOT/dist"
CACHE="$DIST/cache"
OUT="$DIST/Skimlight"
ZIP="$DIST/Skimlight-$VERSION-windows-x64.zip"

mkdir -p "$CACHE/wheels"
rm -rf "$OUT" "$ZIP"
mkdir -p "$OUT"

echo "==> Python $PYVER 嵌入式版"
EMBED="$CACHE/python-$PYVER-embed-amd64.zip"
[ -f "$EMBED" ] || curl -fsSL -o "$EMBED" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-embed-amd64.zip"
mkdir -p "$OUT/python"
( cd "$OUT/python" && "$PY" -c "import zipfile,sys;zipfile.ZipFile(sys.argv[1]).extractall('.')" "$EMBED" )
PTH="$(ls "$OUT"/python/python3*._pth)"
# 嵌入式版默认只搜索自带的标准库；加入依赖目录与应用目录，并启用 site
printf 'python312.zip\r\n.\r\nLib\\site-packages\r\n..\\app\r\nimport site\r\n' > "$PTH"

echo "==> 依赖（win_amd64 / cp312）"
[ -n "$(ls "$CACHE"/wheels/jieba-*.whl 2>/dev/null)" ] || \
  "$PY" -m pip wheel -q --no-deps -w "$CACHE/wheels" "$(grep -E '^jieba==' "$ROOT/requirements.txt")"
REQS="$CACHE/requirements-win.txt"
grep -v -E '^\s*(#|$)|^jieba==' "$ROOT/requirements.txt" > "$REQS"
ls "$CACHE"/wheels/jieba-*.whl >> "$REQS"
"$PY" -m pip install -q --no-compile --disable-pip-version-check \
  --target "$OUT/python/Lib/site-packages" \
  --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 --only-binary=:all: \
  -r "$REQS"
rm -rf "$OUT/python/Lib/site-packages/bin"
# 精简：运行时不需要的编译源文件、类型存根、测试，以及 jieba 的 paddle 模式模型（约减少 100 MB）
SP="$OUT/python/Lib/site-packages"
find "$SP" -type f \( -name '*.cpp' -o -name '*.c' -o -name '*.h' -o -name '*.pyx' -o -name '*.pxd' \
  -o -name '*.pyi' -o -name '*.f' -o -name '*.f90' \) -delete
find "$SP" -type d \( -name tests -o -name testing \) -prune -exec rm -rf {} +
rm -rf "$SP/jieba/lac_small" "$SP/jieba/analyse"   # paddle 模式模型、关键词提取（本地程序不用）

echo "==> 应用与扩展"
mkdir -p "$OUT/app/host" "$OUT/app/skimlight"
cp "$ROOT/host/skimlight_host.py" "$OUT/app/host/"
cp "$ROOT"/skimlight/*.py "$OUT/app/skimlight/"
cp -r "$ROOT/extension" "$OUT/extension"
cp "$ROOT/LICENSE" "$OUT/"

# 启动器：Chrome 通过 cmd 运行 .bat；-u 使标准输出不缓冲（Native Messaging 需要）
printf '@echo off\r\n"%%~dp0python\\python.exe" -X utf8 -W ignore::SyntaxWarning -u "%%~dp0app\\host\\skimlight_host.py"\r\n' > "$OUT/skimlight_host.bat"
printf '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%%~dp0install.ps1"\r\npause\r\n' > "$OUT/install.cmd"
# 卸载：PowerShell 删除注册表项并检查 Chrome 是否仍在使用本地程序（失败时退出码非 0），
# 程序目录由 cmd 最后删除；(goto) 先结束批处理上下文，cmd 不再回读已被删除的脚本文件
printf '@echo off\r\ncd /d "%%TEMP%%"\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%%~dp0uninstall.ps1"\r\nif errorlevel 1 (pause & exit /b 1)\r\npause\r\n(goto) 2>nul & rmdir /s /q "%%LOCALAPPDATA%%\\Programs\\Skimlight"\r\n' > "$OUT/uninstall.cmd"

# PowerShell 5 按系统代码页读取无 BOM 的脚本，中文会乱码，因此写入带 BOM 的 UTF-8、CRLF 换行
write_ps1() { printf '\xef\xbb\xbf' > "$1"; sed "s/__EXT_ID__/$EXT_ID/g; s/\$/\r/" >> "$1"; }

write_ps1 "$OUT/install.ps1" <<'PS1'
# Skimlight 安装：复制到 %LOCALAPPDATA%\Programs\Skimlight，并为 Chrome 注册本地程序（仅当前用户，无需管理员权限）
$ErrorActionPreference = 'Stop'
$Name = 'com.skimlight.host'
$ExtId = '__EXT_ID__'
$Src = $PSScriptRoot
$Dest = Join-Path $env:LOCALAPPDATA 'Programs\Skimlight'

Write-Host ''
Write-Host 'Skimlight 安装' -ForegroundColor Cyan
if ($Src.TrimEnd('\') -ne $Dest.TrimEnd('\')) {
    # Chrome 运行时会占用已安装的 python.exe，覆盖安装前先确认本地程序没有在运行
    $Running = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Dest\*" }
    if ($Running) {
        Write-Host 'Chrome 正在使用 Skimlight 本地程序，无法覆盖安装。' -ForegroundColor Red
        Write-Host '请完全退出 Chrome（包括系统托盘中的后台进程）后重新运行 install.cmd。'
        exit 1
    }
    Write-Host "复制文件到 $Dest ..."
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    # /MIR 会删除目标中多出的文件：排除安装时生成的描述文件与 Python 缓存；/R /W 避免遇到被占用文件时长时间重试
    robocopy $Src $Dest /MIR /XF "$Name.json" /XD __pycache__ /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw '复制失败：部分文件被占用。请完全退出 Chrome 后重新运行 install.cmd。' }
}

$Bat = Join-Path $Dest 'skimlight_host.bat'
$ManifestPath = Join-Path $Dest "$Name.json"
$Manifest = [ordered]@{
    name = $Name; description = 'Skimlight local host'; path = $Bat; type = 'stdio'
    allowed_origins = @("chrome-extension://$ExtId/")
} | ConvertTo-Json
[IO.File]::WriteAllText($ManifestPath, $Manifest, (New-Object Text.UTF8Encoding $false))
New-Item -Force -Path "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$Name" -Value $ManifestPath | Out-Null

Write-Host '检查运行环境（首次运行需要十几秒）...'
# PowerShell 5 在 Stop 模式下会把外部程序写到 stderr 的任何内容（如 jieba 的语法警告）当作错误，检查期间改为 Continue
$ErrorActionPreference = 'Continue'
$Check = & (Join-Path $Dest 'python\python.exe') -X utf8 -W ignore -c "import jieba, jieba.posseg, httpx; print('ok')" 2>&1 | Out-String
$Code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($Code -ne 0 -or $Check -notmatch 'ok') { throw "运行环境检查失败：$Check" }

$ExtDir = Join-Path $Dest 'extension'
try { Set-Clipboard -Value $ExtDir } catch {}
Write-Host ''
Write-Host '本地程序已安装。' -ForegroundColor Green
Write-Host '最后一步（只需一次）：'
Write-Host '  1. 在 Chrome 地址栏打开 chrome://extensions ，打开右上角「开发者模式」'
Write-Host '  2. 点「加载已解压的扩展程序」，选择下面的文件夹（路径已复制到剪贴板）：'
Write-Host "     $ExtDir" -ForegroundColor Yellow
Write-Host '  3. 扩展会自动打开设置页，填写 API key 后即可使用；在网页上按 Alt+J 开关'
Write-Host ''
Write-Host '如果之前加载过旧版 Skimlight，请先在 chrome://extensions 中移除旧版。'
PS1

write_ps1 "$OUT/uninstall.ps1" <<'PS1'
# Skimlight 卸载：删除注册表项与程序文件；配置与缓存（含 API key）保留在 %LOCALAPPDATA%\Skimlight\data
$ErrorActionPreference = 'Stop'
$Name = 'com.skimlight.host'
$Dest = Join-Path $env:LOCALAPPDATA 'Programs\Skimlight'
$Data = Join-Path $env:LOCALAPPDATA 'Skimlight\data'

# 程序目录由 uninstall.cmd 在最后删除；这里先确认本地程序没有在运行（Chrome 打开时会占用 python.exe）
$Running = Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Dest\*" }
if ($Running) {
    Write-Host 'Chrome 正在使用 Skimlight 本地程序。请完全关闭 Chrome 后重新运行 uninstall.cmd。' -ForegroundColor Red
    exit 1
}
Remove-Item -Force -ErrorAction SilentlyContinue -Path "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$Name"
Write-Host '已卸载 Skimlight 本地程序。' -ForegroundColor Green
Write-Host '请在 chrome://extensions 中移除 Skimlight 扩展。'
Write-Host "配置与缓存保留在 $Data ；如需一并删除（包括 API key），删除该文件夹即可。"
PS1

cat > "$OUT/README.txt" <<'TXT'
Skimlight for Windows
=====================

Install / 安装
  1. Unzip this folder anywhere and double-click install.cmd.
     解压后双击 install.cmd（文件会复制到 %LOCALAPPDATA%\Programs\Skimlight）。
  2. Open chrome://extensions, turn on Developer mode, click "Load unpacked" and choose
     %LOCALAPPDATA%\Programs\Skimlight\extension (the installer copies this path to the clipboard).
     打开 chrome://extensions，打开「开发者模式」，点「加载已解压的扩展程序」，选择上面的文件夹。
  3. Enter your OpenRouter or TypeSafe API key on the settings page that opens.
     在自动打开的设置页中填写 API key。按 Alt+J 开关当前网页。

Uninstall / 卸载
  Double-click uninstall.cmd, then remove the extension in chrome://extensions.
  Settings and cache stay in %LOCALAPPDATA%\Skimlight\data.

https://github.com/JackyYang258/skimlight
TXT
sed -i 's/$/\r/' "$OUT/README.txt"

echo "==> 打包"
( cd "$DIST" && "$PY" - "$ZIP" <<'PYZIP'
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for root, _, files in os.walk("Skimlight"):
        for f in files:
            z.write(os.path.join(root, f))
PYZIP
)
echo "完成：$ZIP"
du -sh "$OUT" "$ZIP"
