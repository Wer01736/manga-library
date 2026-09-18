@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
if not exist "%PYTHON_EXE%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] 找不到 Python，漫畫網頁版無法啟動。
    pause
    exit /b 1
  )
  set "PYTHON_EXE=python"
)

echo [%date% %time%] Starting with %PYTHON_EXE%>>startup.log
"%PYTHON_EXE%" main.py 1>>server.log 2>>server-error.log
if errorlevel 1 (
  echo.
  echo [ERROR] 漫畫網頁版啟動失敗，請檢查 server-error.log。
  pause
)
