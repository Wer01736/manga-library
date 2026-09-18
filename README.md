# Manga Library／本機漫畫書庫

一套在 Windows 本機執行的 ZIP／CBZ 漫畫收藏管理工具。介面由瀏覽器呈現，預設只監聽 `127.0.0.1:8765`；漫畫內容與書庫索引不會因使用本程式而自動上傳到雲端。

本 repository 只包含程式碼，不包含漫畫、封面、收藏資料庫、下載紀錄或使用者的本機路徑。

## 主要功能

- 掃描一個或多個本機資料夾，建立 ZIP／CBZ 索引。
- 搜尋、作者資訊、自訂分組與標籤。
- 在瀏覽器閱讀、顯示縮圖及調整頁面順序。
- 為作品加上閱讀標記，並篩選已標記的作品。
- 記住上次閱讀位置，之後可快速回到原處。
- 在使用者確認後重新命名、移動或重建壓縮檔。
- 使用 SHA-256 與圖片特徵協助辨認重複檔案，但不自動刪除。
- 以暫存檔重建 ZIP，驗證成功後才取代原檔。
- 可從支援的作品目錄頁建立本機下載佇列；使用者必須自行確認內容使用權與來源網站規則。

## 系統需求

- Windows 10 或 Windows 11
- Python 3.10 以上

## 安裝

在 PowerShell 進入專案資料夾後執行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 啟動

```powershell
python main.py
```

然後在瀏覽器開啟：

```text
http://127.0.0.1:8765/
```

也可以在 Windows 直接執行 `啟動漫畫網頁版.bat`。

第一次啟動時，程式會在本機建立 `data/` 與 SQLite 索引。`data/` 已由 `.gitignore` 排除，不應提交到 GitHub。

## 測試

```powershell
python -m unittest discover -s tests -v
```

測試會在 `.test_runtime/` 內自行產生純白測試圖片與暫存 ZIP，不需要也不使用真實漫畫圖片。

## 資料與版權

- 請在操作收藏前自行保留重要檔案的備份。
- 本程式不附帶任何漫畫、封面、字型或模型檔案。
- 請只處理及下載你有權使用的內容，並遵守內容來源網站的使用條款與所在地法律。
- `.gitignore` 是最後一道防線；提交前仍應確認 staged files 不含 `data/`、資料庫、log 或私人路徑。

## 授權

程式碼以 [MIT License](LICENSE) 授權。

