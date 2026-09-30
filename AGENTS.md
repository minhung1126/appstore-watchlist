# 專案規格與維護指引

## 已確認需求

- 監視第三方 App 的本體價格、IAP 和訂閱；多個 App、多個商店地區，預計 App 不超過 50 個。
- 同一地區的價格漲跌、免費／付費轉換、幣別改變均發 Discord webhook。
- 首次查詢只建立基準；新增商品通知；連續三次成功查詢未列出商品才通知消失，失敗不計次。
- `apps.json` 管理 App ID、regions、language、track_app、track_iap、iap_ids。
- `config.json` 管理頻率、HTTP 節流與重試、通知與 logging；預設約每三小時查詢。
- 儲存沿用 `data/state.json`，GitHub Actions 提交 Git。通知採持久待發事件，逐筆成功後移除。
- 使用 Python 3.12 標準函式庫，不引入匯率服務。多貨幣以來源的幣別與 Decimal 原值獨立比较。
- 本輪使用者已明確縮小範圍為 HTTP 請求細節。不得自行加入本機 Web UI、HTTP server、localStorage 或變更儲存架構。

## 網路請求細節

實作位於 `watch.py`，參考：

- https://github.com/appstore-discounts/appstore-discounts
- https://github.com/paradossio/AppPriceTracker-iOS/blob/main/AppPriceTracker.py
- https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/Searching.html

### App 本體

GET `https://itunes.apple.com/lookup?id={app_id}&country={region}&entity=software`

- 固定 ID 監視使用 Lookup；不需要搜尋 App 名稱。
- `country` 使用兩碼地區小寫，不從 IP 或顯示語言推斷商店。
- 價格讀 `price`、幣別讀 `currency`，核對 `trackId`。
- 明確 `resultCount=0` 或 HTTP 404 表示本輪未列出；空 body、非 JSON、欄位錯誤屬查詢失敗。

### IAP／訂閱

GET `https://apps.apple.com/api/apps/v1/catalog/{region}/apps/{app_id}?platform=web&views=top-in-app-purchasables&l={language}`

- 發送 Safari User-Agent、Accept: application/json、Accept-Encoding: gzip。
- Referer: `https://apps.apple.com/{region}/app/id{app_id}`。
- Accept-Language 使用設定語言，`l` 為其小寫形式。語言只控制文字，region 才控制商店。
- 依參考實作保留 `Authorization: Bearer` 空 bearer 標頭；它不包含憑證，不需要 API token、登入、cookie 或 Apple 開發者帳號。不得把 Discord Secret 放入 Apple 請求。
- 讀 `data[0].views.top-in-app-purchasables.data`，核對 App ID；讀每個 IAP ID 的 offers、price、currencyCode、isSubscription 與 recurringSubscriptionPeriod。
- 商品識別為 App／region／IAP ID／offer type／週期，避免用翻譯名稱對齊商品。
- 來源是 Apple 網頁內部 API，沒有公開穩定契約。可能限流，且 top view 可能非完整商品清單；不得承諾全部 IAP／個人優惠可取得。
- 格式異常、含分頁 next、缺少價格等情況停止該來源更新，保留舊狀態。

### Python 執行方式

Python／GitHub Actions 從伺服器端直接呼叫 Apple API，瀏覽器 same-origin／CORS 限制不適用此執行方式。參考專案需要 localhost 轉發是其瀏覽器 UI 架構的需求；此專案不需啟動 port 8765，因此沒有該端口單一實例限制。Workflow 用 concurrency 排隊以保護 Git 狀態。

### 節流、錯誤與重導

- 共用 Client 依 monotonic 時間節流，預設序列化請求間隔 3.2 秒，約 18.75 次／分鐘。Apple 文件寫約 20 次／分鐘且可能改變；不要以五路併發繞過限制，也不要承諾 30 國十秒完成。
- 每次嘗試都套用節流與 25 秒 timeout；預設首次加三次重試。
- 網路／HTTP 傳輸失敗、HTTP 408／429／5xx 指數退避，起始五秒，基本退避上限 60 秒。
- 支援 Retry-After 秒數與 HTTP 日期，以及 Discord 429 JSON 的 retry_after。等待超過 120 秒則結束該請求交由後續執行重試，不提前重試。
- gzip 解壓後 UTF-8 JSON 解析；金額以 Decimal 處理，狀態保存數字字串，避免浮點誤差。
- 不使用 CookieJar。Apple 僅允許 HTTPS、相同主機及商店的 GET 重導；Lookup 重導需保留 country 和 id。拒絕 webhook POST 重導，避免憑證或資料轉送。
- Apple 404 計為未列出；Discord 404 是通知失敗，事件保留。其他永久 HTTP 錯誤不反覆重試。
- Logging 不輸出請求完整 URL、webhook、Authorization 或完整回應；以來源、HTTP code、重試與事件 ID 記錄。

## 通知與保存

Discord POST JSON 使用 Content-Type: application/json，保留其他 query 參數並設定 wait=true。訊息顯示商品類型、本地幣別舊價／新價、地區、IAP ID、訂閱週期和 App 商店連結。allowed_mentions 禁用自動提及。

先保存價格與待發事件，再投遞。每筆成功後存檔；未知結果或中斷可能重複通知，採至少一次投遞，事件 ID 可協助辨識。不得宣稱 exactly-once。

## 驗證

執行 `python -m unittest discover -s tests -v`，測試不可發真實 Discord 訊息。

2026-09-30 唯讀實查：Procreate 本體於 tw/us/jp 成功取得 TWD/USD/JPY；ChatGPT IAP 於相同地區取得 6/9/6 個商品方案。此驗證不保證長期可用或清單完整；先前曾收到 429，之後成功。實際 Discord 投遞尚需使用者設定 Secret。

維護 HTTP 時應驗證 URL／地區／標頭、無 cookie、gzip／金額精度、節流、退避、重導限制與秘密遮蔽；修改追蹤時應驗證本體、IAP、訂閱、多地區和幣別的通知與持久狀態。
