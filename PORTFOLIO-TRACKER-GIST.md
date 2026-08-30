# Portfolio Tracker × Gist 契約

改 **SQLite 本機儀表板**（資料夾 `pionex grid record-v2`）或 **Portfolio Tracker** 之前，先讀這份。兩邊共用同一個私有 Gist，一邊改壞，另一邊網格頁或加密欄就會停。

Excel 那條線（`pionex grid record`、08:00）**不走 Gist**，跟這份無關。

## 誰寫、誰讀

| 角色 | 專案 | 做什麼 |
|---|---|---|
| 唯一寫入方 | SQLite 儀表板 `python -m v2.capture` | ingest 成功後 PATCH Gist **第二檔** |
| 只讀方 | Portfolio Tracker（v15.954 起） | 啟動／pull 時讀同一檔；失敗保留舊 cache，不中斷持倉同步 |

- Gist 第一檔永遠是 PT 的 `portfolio-tracker-holdings.json`。SQLite 這邊 **禁止** 把這個檔名放進 PATCH body。
- 第二檔檔名固定：`pionex-grid-ledger.json`（`v2/gist_publish.py` 的 `LEDGER_FILENAME`）。**不要改檔名**，PT 用常數對這個字串。
- SQLite 用本機 `GIST PUBLISH.txt`（第 1 行 Gist ID、第 2 行 token）。PT 用自己存的同一組 Gist ID。兩邊必須指到**同一個** gist。
- 缺 `GIST PUBLISH.txt`、或 PATCH 失敗：只略過發布，**sqlite capture 仍算成功**。不要為了 Gist 去回滾資料庫。
- PT **不放** 派網 API 金鑰，也 **不 PATCH** `pionex-grid-ledger.json`。

程式入口：

- 寫：`v2/gist_publish.py` → `ledger_publish_payload()` in `v2/store.py`
- 讀：`portfolio-tracker-v15.html` → `_validatePionexLedger` / `_applyPionexLedger` / `renderPionexGridTab`

## PT 現在會驗的東西（改了就整份帳本被丟掉）

```js
obj.schema === 1
Array.isArray(obj.days)
```

`schema` 只要不是數字 `1`，PT 會當無效，網格分頁空白、錢包 TWD 也填不進去。

**所以：不要把 `LEDGER_SCHEMA` 改成 2，除非同一天改 PT 的 `_validatePionexLedger`。**

## 已在用的欄位（刪或改型別會壞畫面）

頂層：

- `schema`（必須是 `1`）
- `source`（目前 `"pionex-grid-v2"`；PT 不強制，但不要亂改成別種物件）
- `available`
- `days`（陣列；可為空，但不能缺這個 key 或改成非陣列）
- `capture_date`、`captured_at`（月曆預設日、新鮮度）
- `wallet.usdt`、`wallet.twd`（持倉「加密 TWD」填入；`twd` 必須是數字）
- `latest.gap_days`、`latest` 底下的利潤數字（有就顯示）

`days[]` 每一天：

- `date`（`YYYY-MM-DD`）
- `daily_profit_usdt`（字串或數字都可以，PT 會 `Number()`）
- `cumulative_usdt`
- `daily_profit_twd`、`cumulative_twd`、`investment_usdt`（有就顯示）
- `rows[]`：`symbol`、`status`、`investment_usdt`、`daily_profit_usdt`、`cumulative_usdt`

語意（PT 月曆依此畫）：

- 基準日 `daily_profit_*` = 0
- `cumulative_usdt` = 當天還開著的倉，GridProfit 加總（不是從專案第一天加到現在的流水帳）
- `wallet.twd` = 臺銀美金現金買入口徑，跟 SQLite 儀表板同一套

可加、不可拿掉：`true_profit_usdt`、`daily_profit_coin`、`fx_gap_usdt` 等新欄位 PT 現在不驗證，**加**沒問題；**刪上面列出的舊欄位**或改名字會壞。

幣本位（ETH）的 `rows[].daily_profit_usdt` 是「當天新增的幣 × 抓取現貨價」，不是畫面 USDT 庫存相減。PT 不必改 HTML，pull 後單日數字會自己變。

## 改程式時的檢查清單

動到下面任何一項，先問「PT 網格頁 / 填入加密欄還活著嗎」：

1. `ledger_publish_payload()` 的 key、巢狀結構、`schema` 數字
2. `LEDGER_FILENAME`、Gist PATCH 的 `files` 物件（只能有 ledger 那一檔）
3. 基準日規則、當日利潤 0、累計定義
4. `wallet.twd` 的算法或單位（必須是整數台幣，不是萬、不是字串 `"約 xxx"`）
5. capture 成功後是否還呼叫 `publish_ledger`（預設要呼叫；`--skip-publish` 是例外）
6. PT 的 `_validatePionexLedger`、`GIST_LEDGER_FILENAME`、月曆讀的欄位

建議驗證：

- SQLite：`python -m unittest v2.tests.test_gist_publish v2.tests.test_store.StoreTests.test_ledger_publish_payload_compact`
- 確認 PATCH body **沒有** `portfolio-tracker-holdings.json`
- PT：pull 後網格月曆還有日期、單日／累計還有數字；失敗時持倉同步不能跟著死

## 明確禁止

- 用這組 token 去改 holdings JSON
- 把 sqlite、派網金鑰、原始 API blob 推進 Gist
- 為了本機儀表板新功能而改 `schema` 卻不改 PT
- 假設 PT 會打 `127.0.0.1:8787`（它不會；異地只靠 Gist）

## 相關檔案

SQLite 儀表板：

- `v2/store.py` → `ledger_publish_payload`
- `v2/gist_publish.py`
- `v2/capture.py`（ingest 後發布）
- `GIST PUBLISH.txt`（本機，不上 Git）

Portfolio Tracker：

- `portfolio-tracker-v15.html`（約 v15.954／v15.955）
- 常數 `GIST_LEDGER_FILENAME`
- 網格分頁 `renderPionexGridTab`
