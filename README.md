# Pionex Grid

Read-only recorder for Pionex **contract grid** (futures grid) profits.

This public repository contains **program files only**. Live Excel ledgers, SQLite databases, API keys, logs, and exports stay on the local machine.

## Local paths

Two views, one shared SQLite ledger:

- **Excel 每日紀錄** (08:12 production): `D:\My-project\pionex grid record`
- **SQLite 本機儀表板** (HTML at 127.0.0.1:8787): `D:\My-project\pionex grid record-v2`

The folder name still has `-v2` because it is a git worktree. On screen it is the SQLite dashboard, not “v2”.

## Version

The dashboard shows its version at the top left (`v2/version.py`). Each production deploy
bumps it, adds an entry to `CHANGELOG.md` and tags the deployed commit `release-<version>`.
The older `v1.0.0` tag belongs to the v1 Excel recorder.

## Security

- Copy `PIONEX API.example.txt` to `PIONEX API.txt` locally. Line 1 = API key, line 2 = API secret.
- Enable **Bot reading** only. Do not enable Bot trading for this tool.
- Scripts only call GET endpoints. They never create, adjust, reduce, pause, or cancel bots.
- Never commit `PIONEX API.txt`, `GIST PUBLISH.txt`, `data/*.xlsx`, `*.sqlite`, logs, or `exports/`.

## v15.968 — Excel from the shared ledger

The original 16-column Excel report reads v2 SQLite through `v2.legacy_excel`.
`python -m v2.pipeline` at 08:15 captures that ledger, then writes both workbooks:
the 16-column file through `pionex-grid-daily.ps1`, and the daily report through
`python -m v2.daily_report`. A logon catch-up runs `python -m v2.pipeline --if-missing`.
Export runs only when the ledger date is newer than `v2-data/pipeline-state.json`.
An Excel failure is written to `v2-data/pipeline.log` and does not fail the capture.

Until deploy, the old 08:10 and 08:12 tasks still update those files. After
`install-v2-daily-capture.ps1` is run again, disable those two tasks. Do not delete them.

Every export rebuilds ledger dates, including late settlement corrections. Withdrawn
profit is already included in API gridProfit, closing paired increments appear once,
and native-coin daily profits exclude price revaluation. The original Excel cumulative
column remains recorded-period income (baseline zero), not lifetime profit.
The savings sheet and table structure remain intact. Timestamped backups and atomic
replacement protect existing files; stale/missing data and unknown dates stop the export.
`-DryRun` previews metadata; `-Backfill` / `-AllowStale` allow historical ledger exports.

## SQLite 本機儀表板

Worktree `D:\My-project\pionex grid record-v2` (branch `v2/sqlite-dashboard`). Records into `v2-data/pionex-grid.sqlite` and serves `http://127.0.0.1:8787`. The 08:15 pipeline writes this ledger and then both Excel workbooks.

Double-click `儀表板開關.cmd` to start or stop the local server (works even when the page is down). Double-click `抓取SQLite紀錄.cmd` (or `capture-v2.cmd`) to snapshot Pionex into SQLite.

```powershell
cd "D:\My-project\pionex grid record-v2"
python -m v2.capture
python -m v2.server
```

Architecture notes: `http://127.0.0.1:8787/architecture.html`.

After login / reboot the SQLite dashboard can start by itself:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-dashboard-autostart.ps1
```

That registers Task Scheduler `Pionex Grid SQLite Dashboard`: start at logon, then re-check `http://127.0.0.1:8787` every 30 minutes (HTTP down → restart). It does not change the Excel export task. Logs: `v2-data/dashboard.log` and `v2-data/dashboard-server.log` (local only).

- SQLite file: `v2-data/pionex-grid.sqlite` (local only)
- Export: `exports/` (does not overwrite v1 Excel)
- First successful v2 capture is a baseline (daily profit 0)
- Server binds `127.0.0.1` only and never reads the API key

Daily pipeline (08:15 capture, then both Excel exports):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-v2-daily-capture.ps1
```

That registers `Pionex Grid v2 Daily Capture` at 08:15 (`python -m v2.pipeline`), plus `Pionex Grid v2 Daily Capture Catch-up` at logon (`python -m v2.pipeline --if-missing`). If Windows restarted overnight and the PC sat at the login screen at 08:15, the catch-up captures today's record after login. It skips the capture when today already exists, so it never replaces the 08:15 snapshot, and it still exports an Excel file when the ledger date is newer than the last export.

A failed capture is retried three times, 10 minutes apart; a run is stopped after 30 minutes, and the Node collector after 5 (each Pionex request after 20 s, with HTTP 429/5xx and dropped connections retried). A second capture on a day that already has its 08:15 record is skipped, so it never moves the cut-off; `python -m v2.capture --replace-date` replaces it on purpose. If an open position is in neither the running nor the finished list, the capture writes nothing and fails, because Pionex returned an incomplete list; rerun later, or add `--allow-missing` if the position is really gone (it is then marked 關倉待補).

Trial (2026-10-20 to 2026-10-26, ended): `Pionex Grid v2 Shadow 0815` ran `python -m v2.shadow` at 08:15 and saves the snapshot to `v2-data/shadow/` only, never into the ledger, Gist or Excel. At the old 08:05 cut-off some positions had not posted the 08:00 funding settlement yet. `python -m v2.shadow --report` shows, per day, how many had posted at the ledger capture and at 08:15. On 5 of 6 days 08:05 missed 2-3 positions and 08:15 caught all, so the daily capture moved to 08:15 and the shadow task was disabled. It keeps its own closed-bot cache in that folder. `install-v2-shadow-capture.ps1` registers the task.

Hardening (2026-10-26): the dashboard answers only requests addressed to `127.0.0.1`/`localhost` on its own port, so a DNS-rebinding page cannot read it, and `export.xlsx` always writes `exports/pionex-grid-export.xlsx`. The server records its pid in `v2-data/dashboard-server-<port>.pid`; the start and switch scripts stop only that server or a `v2.server` listening on the port, never a preview on another port. `live-snapshot.json` and `finished-details.json` are written atomically under a cross-process lock, and the USDT/TWD rate is fetched before the ledger write starts (after a failed fetch it waits 10 minutes). Every Gist PATCH appends GitHub's remaining hourly quota to `v2-data/gist-rate.log`. After a successful legacy export only the newest 14 `snapshot-legacy-excel-*.xlsx` are kept; a hung export also stops the Excel it started; from 09:00 the 30-minute live task re-runs an export the daily run could not finish (e.g. the workbook was open). The Gist ledger keeps per-position rows for the latest 90 days; older days keep their day totals.

A day that was still missed can be filled from the 30-minute intraday record of that day (first record at or after 08:15): `python -m v2.backfill --date YYYY-MM-DD` previews, `--apply` writes the ledger, logs the change in `corrections[]` and republishes the Gist ledger. After ingest it PATCHes `pionex-grid-ledger.json` on the same private Gist used by Portfolio Tracker. Copy `GIST PUBLISH.example.txt` to `GIST PUBLISH.txt` (line 1 = Gist ID, line 2 = Gist token). Missing that file skips publish; sqlite capture still succeeds. Use `--skip-publish` to ingest only.

Current positions (does **not** write daily sqlite or Excel):

```powershell
python -m v2.capture --live
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-v2-live-publish.ps1
```

That registers `Pionex Grid v2 Live Publish` every 30 minutes on :00 and :30: fetch running grids into `v2-data/live-snapshot.json`, append an intraday point to `v2-data/intraday.sqlite`, and PATCH Gist third file `pionex-grid-live.json`. Manual 「更新目前倉位」 on the dashboard does the same. The dashboard at `http://127.0.0.1:8787` reads the live file on open. Re-run the installer once to move an older task onto :00/:30.

效率走勢（每小時效率）：
- `v2-data/intraday.sqlite`（local only）keeps every 30-minute capture and the 08:15 capture. The Pionex API has no fill history, so missed slots can never be re-fetched; the 08:15 run backs the file up to `v2-data/backups/intraday-YYYYMMDD.sqlite` (7 kept).
- Interval profit reuses the daily-ledger rules (reinvest / add / withdraw, coin-margined revaluation). Buckets start on Taipei clock boundaries (00:00); gaps over 75 minutes are spread evenly and marked as estimates. 1 day / week / month views fill earlier history from the daily ledger (08:15 cut-off, marked).
- `GET /api/v1/efficiency?from=&to=&g=30m|1h|4h|1d|1w|1M&ids=`, `GET /api/v1/efficiency/meta`, `GET /api/v1/efficiency/sparklines?days=14`, `GET|POST /api/v1/efficiency/views` (saved views in `v2-data/efficiency-views.json`).
- Charts are drawn by `v2/static/chart.js` (plain SVG, no CDN). `python -m v2.server --intraday-db <file>` previews another intraday file.

## Portfolio Tracker

關倉紀錄（v15.968）：本機新增「關倉紀錄」分頁，可補抓 API 或依派網結算明細人工補登。
API 未確認的最終淨損益保持待補；配對增量回補原快照區間，關倉淨損益不混入配對利潤或再次扣錢包。
人工補登保留修改歷史，清空欄位代表取消該欄人工覆蓋、回到 API 值或待補。
`GET /api/v1/settlements` 讀清單；`POST /api/v1/settlements/refresh` 補抓；
`POST /api/v1/settlements/{order_id}` 保存完整人工覆蓋表單（closed_at、grid_profit、lifetime_grid_profit、net_profit、own_cash_in、funding_fee、fee、settlement_usdt_rate、note）；`GET /api/v1/settlements/{order_id}/audit` 讀修改紀錄。

關倉淨損益自動確認：淨損益＝實際回到錢包的錢（API unlockQuoteAmount）＋期間已提領 − 自己投入（投入額＋追加保證金−複投；體驗金為 0）。
另用「已實現損益＋手續費＋資金費」交叉核對，差距過大時標示需核對，只要補「自己投入」一格即可重算。
幣本位關倉自動以 API 關倉價換算，不必手填。已記錄日子被重算時寫入修正紀錄（`ledger_corrections`，並隨帳本發布 `corrections[]`）；
帳本若與上次成功發布不同，30 分鐘 live 排程會補發，「執行狀態」頁也能 `POST /api/v1/publish/ledger` 手動重新發布。
資料庫 schema 11 自動相容升級；Gist schema 維持 1。補抓不覆寫當日錢包快照。

每日配對核對（v15.968）：

- API `gridProfit` 已包含已提領收益，`profitWithdrawn` 只標示提領事件，不再次加到每日／歷史配對利潤。近 24 小時另讀 API `gridProfit24h`，與每日快照區間不同。
- 每日擷取、即時快照寫入今日、關倉補抓、人工補登及歷史重算共用帳本算法；本機月曆、每日詳情、Excel 匯出、Gist／Portfolio 從帳本讀取結果。
- 寫入今日必須使用台北當日的即時快照，保留原擷取時間。跨日或時間缺失時須先更新即時資料，不能把舊快照改標為現在。
- 關倉待補時保留訂單 ID、標的及已知歷史收益；補到最後配對值時回填首次發現關倉的區間，之後日期不重複入帳。關倉總損益不作配對收益。
- 08:15 的 `v2.pipeline` 寫入每日報表與原格式 Excel，兩者都讀取此帳本。修正歷史後重跑即可回填關倉原區間與後續累計。原 08:00 Excel 已移除獨立相減算法，不再混入幣價重估或漏掉關倉最後配對增量。

Portfolio Tracker reads `pionex-grid-ledger.json` from the private Gist. It never holds Pionex API keys and never PATCHes that file. Holdings stay in `portfolio-tracker-holdings.json`. The local dashboard remains `127.0.0.1:8787` only.

**Before changing capture, ledger JSON, Gist publish, or PT 網格頁:** read [PORTFOLIO-TRACKER-GIST.md](PORTFOLIO-TRACKER-GIST.md). Changing `schema`, the filename, or removing fields PT already reads will blank the remote 網格 tab.

## License of data

Account balances and profits in local `data/` files are private. Fixtures under `fixtures/` are synthetic.
