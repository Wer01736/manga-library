@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0重新啟動漫畫網頁版.ps1"
if errorlevel 1 (
  echo.
  echo 啟動失敗。請查看上方訊息與 launcher-server-error.log。
  pause
)
