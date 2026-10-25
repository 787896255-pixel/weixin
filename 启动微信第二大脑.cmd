@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
where py >nul 2>nul
if errorlevel 1 (
  echo 请先安装 64 位 Python 3.10 或更高版本，安装时勾选 Add Python to PATH。
  echo Python 官方下载：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
if not exist ".venv\.dependencies-ready" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
  echo ready>".venv\.dependencies-ready"
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo 启动失败，请保留上方错误信息。
pause
exit /b 1
