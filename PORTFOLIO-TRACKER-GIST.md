# Portfolio Tracker × Gist 契約

改 **SQLite 本機儀表板**（資料夾 `pionex grid record-v2`）或 **Portfolio Tracker** 之前，先讀這份。兩邊共用同一個私有 Gist，一邊改壞，另一邊網格頁或加密欄就會停。

Excel 那條線（`pionex grid record`、08:00）**不走 Gist**，跟這份無關。

## 誰寫、誰讀

| 角色 | 專案 | 做什麼 |
|---|---|---|
| 唯一寫入方 | SQLite 儀表板 `python -m v2.capture` | ingest 成功後 PATCH Gist **第二檔** |
| 目前倉位寫入 | `python -m v2.capture --live` 或儀表板「更新目前倉位」或 30 分鐘排程 | 成功後 PATCH **第三檔** `pionex-grid-live.json` |
| 只讀方 | Portfolio Tracker（v15.954 起帳本；v15.962 起目前倉位） | 啟動／pull／進網格頁時讀；失敗保留舊 cache，不中斷持倉同步 |

- Gist 第一檔永遠是 PT 的 `portfolio-tracker-holdings.json`。SQLite 這邊 **禁止** 把這個檔名放進 PATCH body。
- 第二檔檔名固定：`pionex-grid-ledger.json`（`LEDGER_FILENAME`）。**不要改檔名**。
- 第三檔檔名固定：`pionex-grid-live.json`（`LIVE_FILENAME`）。PATCH 時 **只帶這一檔**，不要順便重寫第二檔。
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
- `GridProfit` 直接來自派網 API 的 `gridProfit`，已包含已提領的配對利潤，不是剩餘可提領額。`ProfitWithdrawn` 用於標記提領事件與對帳，不可再加進每日或歷史配對收益。例如前日 `gridProfit=1500`，次日 `gridProfit=1505`、`profitWithdrawn=1000`，每日收益是 5，不是 1005。即時近 24 小時數值另讀 `gridProfit24h`。
- `wallet.twd` = 臺銀美金現金買入口徑，跟 SQLite 儀表板同一套

可加、不可拿掉：`true_profit_usdt`、`daily_profit_coin`、`fx_gap_usdt` 等新欄位 PT 現在不驗證，**加**沒問題；**刪上面列出的舊欄位**或改名字會壞。

## v15.968 關倉結算

- `days[].true_profit_usdt`／`true_profit_twd`：截至該快照的歷史配對收益，含基準帶入及記錄期間已關倉、提領、複投。記錄前已提領的收益可能未涵蓋。
- `days[].unresolved_count`：該區間配對資料待補數；數值合計是已知部分，PT 月平均暫不計入待補日，補齊後重算。
- 頂層新增 `settlements[]`，`schema` 仍為 `1`。每筆以訂單 `id` 唯一識別，含 `symbol`、`currency`、`closed_at`（可空）、`observed_at`、`capture_date`、`status`（pending/partial/settled）、`source`、`grid_profit`、`net_profit`、`reported_total_profit`、`funding_fee`、`fee`、`lifetime_grid_profit`、`non_grid_profit`、`daily_reconciled`。
- 結算金額預設是 `currency` 原幣；`*_usdt` 只有 U 本位或已有結算換算價（關倉價或人工）時提供。API `TotalProfit` 僅為回報值，不假定已含所有費用；`net_profit` 改用下方錢包實拿口徑自動計算。
- `grid_profit` 是派網最後顯示值；提領／複投倉須確認完整 `lifetime_grid_profit` 才能計算 `non_grid_profit = net_profit - lifetime_grid_profit`。資金費、手續費只供對帳，不從淨損益再次扣除。
- 缺值用 `null`，不可改成 0；延遲取得結算時回補首次關倉的快照區間。手動補登紀錄存 SQLite，後續 API 不覆蓋人工值。
- PT 只讀關倉紀錄，不建立 dirty/pending、不修改 holdings 或錢包。缺少 `settlements` 的舊帳本仍可顯示。

### 關倉淨損益自動確認（錢包實拿口徑）

- `net_profit` 預設自動計算：`實際回到錢包（unlockQuoteAmount）＋期間已提領（profitWithdrawn）− 自己投入`。自己投入＝`quoteInvestment + extraMargin − profitReinvest`（複投的錢已在拿回金額內，不重複計）；體驗金倉（`investmentFrom` 含 BONUS）自己投入為 0，只計解鎖金額。
- 算得出就 `status: settled`、`source: api`；人工覆蓋後 `source: manual+api`。`status` 仍只有 pending／partial／settled 三種值。
- 只增不刪的新欄位：`net_method`（wallet／manual）、`received`、`own_cash_in`、`own_cash_in_source`、`investment`、`withdrawn_total`、`reinvest_total`、`bonus`、`net_check`（已實現損益＋手續費＋資金費−體驗金分潤）、`net_check_diff`、`net_check_ok`、`needs_review`（兩法差距過大且尚未人工確認投入或淨損益）、`roi_pct`、`close_reason`、`hold_days`、`settlement_rate_source`、`received_usdt`。
- 提領／複投倉沒有人工值時，`lifetime_grid_profit` 自動取 `gridProfit + profitReinvest + profitReduce`。
- 幣本位關倉的換算價自動取 API `closedPrice`（關倉後固定不變；反向合約 `USDT.PERP/幣` 取倒數）。缺少時仍需人工換算價，不用浮動即時價。取得換算價後，關倉區間的 `days[]` 會從待補變成有數字，這是唯一會改動既有 `days[]` 數值的情況，並記入 `corrections[]`。

### 等效倉位（只新增欄位）

- 調高槓桿但把保證金留在原倉時，派網的「投入」（`quoteInvestment`）會變小、`extraMargin` 變大，每格數量和強平價不變。
- 帳本 `days[]` 新增 `equivalent_usdt`（投入＋追加保證金，當天運行中倉位加總）與 `pct_equivalent`（每日利潤 ÷ 等效倉位）；`latest.equivalent_usdt` 同義。舊的 `investment_usdt`、`pct` 不變（仍是派網投入口徑）。
- 第三檔 `pionex-grid-live.json` 新增 `total_equivalent_usdt`、`leverage_nominal`（投入加權名目槓桿）、`leverage_equivalent`（名義倉位 ÷ 等效倉位）。`profit_24h_pct` 的分母改為等效倉位，調槓桿不再讓它跳高。

### v15.974 資金費與等效倉位（只新增欄位）

PT v15.974 起網格頁會讀這些欄位；缺少時（舊帳本）退回原畫面，不報錯。`schema` 仍為 `1`，舊欄位與意義不變：`daily_profit_usdt` 仍只有配對，資金費**不**加進每日利潤。

- `days[]`：
  - `funding_usdt`：當日資金費淨額（派網 `totalFundingFee` 和上一次每日快照相減，付出為負）。沒有可比的前一筆時為 `null`，不補 0。
  - `funding_span_days`：資金費涵蓋的天數；中間缺一天資料時為 2。
  - `net_usdt`：`daily_profit_usdt + funding_usdt`；資金費為 `null` 時也是 `null`。
  - `funding_total_usdt`：所有出現過的倉（含已關倉）的累計資金費合計；沒有資料的日子沿用前一天。
  - `leverage_equivalent`：當天運行中倉的 投入×派網槓桿 ÷ 等效倉位。
- `days[].rows[]`：`equivalent_usdt`（投入＋追加保證金，關倉列為 `null`）、`funding_usdt`、`funding_total_usdt`。
- `latest`：`funding_total_usdt`、`leverage_equivalent`。
- 第三檔 `rows[]`：`equivalent_usdt`、`extra_margin_usdt`、`equivalent_leverage`、`funding_total_usdt`（該倉累計淨額）；頂層 `total_funding_usdt`（運行中各倉合計）。
- 新欄位四捨五入（金額 2 位、資金費 4 位）以控制 Gist 大小；舊欄位精度不變。幣本位資金費是當天新增的幣 × 抓取價，不含幣價重估。

### 歷史修正回報 `corrections[]`

- 頂層新增 `corrections[]`（最近 30 筆，PT 目前不讀）：`capture_date`、`field`（daily_profit_i／cumulative_i／true_profit_i／unresolved_count）、`old_value`、`new_value`、`reason`、`corrected_at`。已記錄的日子因關倉補抓、人工補登、加倉／複投判定而重算時寫入；當日擷取本身不算修正。
- PT 每次拉取都整份取代帳本，所以修正後的數字與月平均會自動更新，不需要 PT 端另外處理。
- 發布結果存在本機 `v2-data/publish-state.json`（內容雜湊不含 TWD／FX 欄位）。帳本與上次成功發布不同時，每 30 分鐘的 `capture --live` 排程會另外送一次「只含帳本檔」的 PATCH 補發；儀表板「執行狀態」也可手動重新發布。

幣本位（ETH）的 `rows[].daily_profit_usdt` 是「當天新增的幣 × 抓取現貨價」，不是畫面 USDT 庫存相減。PT 不必改 HTML，pull 後單日數字會自己變。

## 第三檔目前倉位（`pionex-grid-live.json`）

PT 驗：`schema === 1` 且 `rows` 是陣列。不是帳本、沒有 `days`。失敗只讓「目前倉位」區空白，月曆仍用第二檔。

頂層：`schema`、`source`（`"pionex-grid-v2-live"`）、`captured_at`、`position_count`、`wallet.usdt`／`wallet.twd`、`total_profit_24h_usdt`、`total_investment_usdt`、`total_grid_profit_usdt`、`profit_24h_pct`、`rows[]`（`symbol`、`leverage`、`trend`、`investment_usdt`、`grid_profit_usdt`、`profit_24h_usdt`、`mark_price`、`liq_price`）。

不要放 raw API、金鑰、sqlite。live 發布失敗不刪本機 `live-snapshot.json`。

方向欄位採畫面交易對口徑：API `USDT.PERP/ETH` 等反向合約的 `short` 顯示為 ETH/USDT 的 `long`，`Leverage` 文字同步轉換。只在確認反向交易對時轉換；SQLite 與原始快照仍保留 API 方向，供反向價格、強平及盈虧計算使用，避免二次反轉。本機即時、每日及歷史倉位摘要與 Gist live 共用此顯示轉換。

## 改程式時的檢查清單

動到下面任何一項，先問「PT 網格頁 / 填入加密欄還活著嗎」：

1. `ledger_publish_payload()` 的 key、巢狀結構、`schema` 數字
2. `LEDGER_FILENAME`／`LIVE_FILENAME`、Gist PATCH 的 `files` 物件（每次只動一檔；永遠不要帶 holdings）
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
