# App Store Watchlist

以 Python 3.12 標準函式庫監視 App Store 本體、IAP 與訂閱價格，透過 Discord webhook 通知，GitHub Actions 定期執行。不需要安裝 Python 套件。

## 設定 App

`apps.json` 初始為空清單，請參考 `apps.example.json` 填入自己的 App。範例 App 不會自動加入監視。

```json
{
  "apps": [
    {
      "id": "6448311069",
      "regions": ["tw", "us", "jp"],
      "language": "en-US",
      "track_app": true,
      "track_iap": true,
      "iap_ids": []
    }
  ]
}
```

- `id`：商店網址 `id` 後面的數字。
- `regions`：兩碼商店地區，不是語言；各地價格以當地幣別獨立比較。
- `language`：IAP 名稱顯示語言，預設 `en-US`。
- `track_app` / `track_iap`：是否監視本體／IAP，預設皆為 `true`。
- `iap_ids`：空陣列監視來源列出的全部 IAP；填入數字 ID 僅監視指定商品。可先 dry-run 查看 `DEBUG` 日誌取得 ID。

支援多個 App、多地區；同一 App／地區只能設定一次。修改清單不會將移除的監視項目當作商品下架。

## 執行與部署

本機先驗證價格，無須 Discord Secret：

```powershell
python watch.py --dry-run --force
```

正式執行，將 Discord webhook URL 設為環境變數 `DISCORD_WEBHOOK_URL` 後執行：

```powershell
python watch.py --force
```

`--dry-run` 查詢及比較價格，但不儲存狀態、不發通知；`--force` 忽略執行間隔。可使用 `--apps`、`--config`、`--state` 指定其他檔案。

GitHub 部署步驟：

1. 建立 GitHub 儲存庫並推送本專案至預設分支。
2. 在 Settings → Secrets and variables → Actions 新增 `DISCORD_WEBHOOK_URL` repository secret。
3. 確保 repository／organization 允許 workflow 的 `contents: write` 權限，且分支規則允許 Actions bot 提交狀態。
4. 在 Actions → Watch App Store prices → Run workflow 手動執行一次，建立基準。

Workflow 每小時 UTC 第 17 分鐘喚醒，程式依 `config.json` 決定是否查詢。預設約每三小時一次；手動執行強制查詢。僅查詢時會更新狀態；尚未到期仍會重試待發通知。GitHub 排程可能延遲，非精準計時；一小時以下的間隔需同步調整 workflow cron。[GitHub 排程文件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)

狀態保存在 `data/state.json`，Workflow 即使查詢或通知失敗，也會嘗試提交已完成的狀態。同時執行會排隊。公開儲存庫無活動 60 天可能停用排程；價格不變時仍會記錄成功觀測時間，但排程是否停用仍應以 GitHub 狀態為準。

## 通知規則與狀態

- 首次加入 App／地區只建立基準，預設不通知。
- 既有商品降價、漲價、轉免費或貨幣改變都通知；相同現價不重複通知。
- 建立基準後新增 IAP 通知；商品從來源消失需連續三次成功查詢確認，重新出現也通知。
- 查詢失敗保留舊值，不增加消失次數。404／Lookup 明確空結果則視為本次未列出。
- 「商品不再列出」表示此來源未提供該商品，不能證明 App 或 IAP 已從商店永久下架。
- 先保存價格與待發事件，再發送通知。每筆成功送出後儲存狀態，失敗事件留待下次重試。
- 通知為至少一次投遞：Discord 已收到但回應遺失、程序中斷或 Git 提交失敗時可能重複。訊息附事件 ID 方便辨識。
- 取消監視的歷史快照保留在狀態檔，再次監視會與保留價格比較。取消前已產生的待發事件仍會投遞。
- 訂閱方案按 IAP ID、offer type 與週期區分；只比較公開列出的價格，不判斷個別帳號是否具備優惠資格，也不換算匯率。

## config.json

| 設定 | 預設 | 說明 |
| --- | --- | --- |
| `interval_hours` | 3 | 兩次查詢至少相隔的小時數 |
| `request_interval_seconds` | 3.2 | Apple 請求間隔，共用節流 |
| `request_timeout_seconds` | 25 | 單次請求逾時秒數 |
| `max_retries` | 3 | 首次失敗後的最大重試次數 |
| `retry_base_seconds` | 5 | 指數退避起始秒數；支援 Retry-After 秒數／日期及 Discord retry_after，超過 120 秒則留待下次執行 |
| `missing_confirmation_checks` | 3 | 消失通知所需連續成功查詢次數 |
| `notify_initial` | false | 首次建立基準是否逐商品通知 |
| `notify_new_products` | true | 新商品是否通知 |
| `log_level` | INFO | DEBUG / INFO / WARNING / ERROR |
| `log_file` | null | 可填 `logs/watch.log`，啟用輪替檔案日誌 |

50 個 App × 3 個地區 × 2 種來源，約 300 個請求，正常節流約 16 分鐘，限流重試會延長。Workflow 有 50 分鐘上限；地區較多時可降低監視範圍或調整上限。

## Logging 與驗證

Console 記錄時間、成功查詢數、商品數、變動事件、重試、失敗與 Discord 投遞結果；不列印 webhook、Authorization、環境變數或完整 HTTP 回應。GitHub Actions 提供步驟日誌與執行摘要。`DEBUG` 另列每筆商品 ID／價格，便於挑選指定 IAP。

本機檔案日誌最多 2 MB／檔，保留三份備份；`logs/` 不提交 Git。GitHub 自行管理 Actions 日誌保存期限。持久價格狀態與待發通知保存在 Git，可追溯變動；不是每次都提交完整回應或日誌檔。

```powershell
python -m unittest discover -s tests -v
```

測試覆蓋首次基準、價格漲跌／貨幣改變、不重複通知、新增／消失／恢復、通知部分失敗保留事件、IAP 方案識別及格式異常。整合情境以模擬 HTTP 回應驗證兩個 App × 三個地區（TWD、USD、JPY）的本體／一般 IAP／訂閱，18 筆價格變動經由真實解析、比較、狀態保存及通知組裝流程送至模擬 Discord；下一輪相同價格不重複通知。另驗證僅本體或僅 IAP 的設定，以及 HTTP 地區／語言／標頭、gzip、精確金額、共享節流、Retry-After、Discord 限流與重導限制。測試不存取網路、不發真實 Discord 訊息。

2026-09-30 使用實際 fetcher 唯讀查詢：Procreate 本體在台灣、美國、日本皆成功取得價格，幣別分別為 TWD、USD、JPY；ChatGPT IAP 在同三個地區分別取得 6、9、6 個商品方案，幣別亦正確。此驗證沒有修改監視清單或發送 Discord 訊息。

## 資料來源與限制

Python 直接以 HTTPS 存取來源，瀏覽器 CORS／same-origin 限制不適用，因此不需要 localhost 轉發或 port 8765。請求不使用 API token 或 cookie；地區由 URL 的 country／catalog 地區指定。Apple GET 重導限相同主機與商店，Discord POST 不允許重導。詳細技術記錄見 [AGENTS.md](AGENTS.md)。

App 本體使用 [Apple Lookup API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/index.html)。IAP 使用 Apple 網頁內部 catalog endpoint 的 `top-in-app-purchasables` view，參考 [AppPriceTracker-iOS 原始碼](https://github.com/paradossio/AppPriceTracker-iOS/blob/main/AppPriceTracker.py)。排程及 Git 保存方式參考 [appstore-discounts](https://github.com/appstore-discounts/appstore-discounts)。本專案獨立實作，未複製參考專案程式碼。

IAP endpoint 沒有官方公開契約，可能限流、改版，且商品清單可能不完整。偵測到格式異常或分頁時停止該來源更新，避免誤判商品消失。未列出的商品、個人優惠或全部訂閱方案完整性均無法保證。即時試查曾收到 HTTP 429，後續同環境查詢成功；部署後需使用實際監視清單核對價格。其他 App 的查詢失敗不影響已成功 App 的處理。
