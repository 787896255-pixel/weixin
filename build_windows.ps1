$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $IsWindows -and $env:OS -ne 'Windows_NT') { throw '请在 Windows 上执行，PyInstaller 不支持从 macOS 交叉构建 Windows 程序。' }
py -3 -m venv .build-venv
if ($LASTEXITCODE) { throw '无法创建 Python 构建环境' }
$python = Join-Path $PSScriptRoot '.build-venv\Scripts\python.exe'
& $python -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE) { throw '安装构建依赖失败' }
& $python -m PyInstaller --noconfirm --clean --windowed --onedir `
  --name JingkeExporter --add-data 'web;web' --collect-all webview `
  --collect-all Crypto --collect-all zstandard --collect-all lz4 `
  --hidden-import psutil --hidden-import tkinter app.py
if ($LASTEXITCODE) { throw '打包失败' }
Compress-Archive -Path 'dist\JingkeExporter\*' -DestinationPath 'dist\JingkeExporter-Windows.zip' -Force
Write-Host '已生成 dist\JingkeExporter\JingkeExporter.exe 和 dist\JingkeExporter-Windows.zip'
