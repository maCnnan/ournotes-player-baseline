@echo off
setlocal
set "DEST=D:\ournotes_bot"
set "URL=https://raw.githubusercontent.com/maCnnan/ournotes-player-baseline/main/player_live.py"

if not exist "%DEST%" (
  echo [錯誤] 找不到資料夾 %DEST%
  echo 請先建立該資料夾，或用記事本把 DEST 改成你的專案路徑。
  pause
  exit /b 1
)

cd /d "%DEST%"
if exist player_live.py copy /Y player_live.py player_live.py.bak >nul

echo 正在下載 baseline player_live.py ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%DEST%\player_live.py'"
if errorlevel 1 (
  echo [錯誤] 下載失敗。檢查網路，或先用瀏覽器打開：
  echo %URL%
  pause
  exit /b 1
)

echo.
echo 已覆蓋 %DEST%\player_live.py
echo 舊檔備份為 player_live.py.bak
dir player_live.py
echo.
echo 下一步：
echo   python -u player_live.py --input maatouch --adb C:\platform-tools\adb.exe
echo.
pause
