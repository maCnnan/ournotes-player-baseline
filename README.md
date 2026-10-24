# Our Notes 自動打歌 — 從零到 baseline

這份是 **lead 60 + fallback lead 25 + 長鍵後黃鍵修復** 的基準檔。
分數參考：Ave Mujica HARD 約 133831（視環境會有波動）。

## 0. 你需要先有

- Windows
- [MuMu Player](https://www.mumuplayer.com/)，遊戲已能手動進打歌畫面
- Python 3.10+（安裝時勾選 Add python.exe to PATH）
- Android platform-tools（`adb.exe`），預設路徑 `C:\platform-tools\adb.exe`
- 專案資料夾 `D:\ournotes_bot`（裡面已有 `maatouch`、相依套件）

MuMu 的 ADB 埠一般是 `127.0.0.1:16384`。腳本會自己 `adb connect`。

## 1. 一鍵覆蓋（不要手動貼程式碼）

1. 下載 [overwrite.bat](https://github.com/maCnnan/ournotes-player-baseline/raw/refs/heads/main/overwrite.bat)
2. 雙擊執行
3. 看到「已覆蓋」和檔案大小約 90 KB 即成功
4. 舊檔會存成 `D:\ournotes_bot\player_live.py.bak`

或瀏覽器直接存這支 Python：

https://raw.githubusercontent.com/maCnnan/ournotes-player-baseline/main/player_live.py

## 2. 安裝 Python 套件（只做一次）

在 `D:\ournotes_bot`：

```powershell
cd D:\ournotes_bot
python -m pip install numpy opencv-python mss dxcam
```

## 3. 開一局

1. MuMu 開著，遊戲停在 **即將開始打歌** 的畫面（視窗不要最小化、不要中途縮放）
2. PowerShell：

```powershell
cd D:\ournotes_bot
python -u player_live.py --input maatouch --adb C:\platform-tools\adb.exe
```

3. 終端機應出現：

```text
taps use --lead-ms 60, fallback --fallback-lead-ms 25
```

4. 進歌。按 `F8` 或 `Ctrl+C` 停止。
5. 結束後看 `scheduled_touches_live.csv` 與遊戲結算。

## 4. 這版固定什麼、不要亂改什麼

| 項目 | 值 | 備註 |
|------|-----|------|
| `--lead-ms` | 60 | 實測最佳。75 會退步 |
| `--fallback-lead-ms` | 25 | 只影響 fallback |
| `FRAC_ON` | 0.30 | 降到 0.25 會讓 predicted 崩掉 |
| probe-skip | 關閉 | 開了會漏掉長鍵後的新音符 |
| 黃鍵 | 長鍵後先抬起再滑 | 已修 |

一次只改一個參數。改壞就雙擊 `overwrite.bat` 回到這版。

## 5. 每局要記的數字

- 分數、MISS、PERFECT 的 FAST / SLOW
- 終端機：`predicted`、`fallback`、`holds`、`flick actions`

predicted 明顯掉到 800 以下，多半是開局太晚或視窗被縮放，不是程式邏輯壞了。請從頭進歌再打一次。
