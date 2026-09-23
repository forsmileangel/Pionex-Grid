# Pionex Grid

Read-only recorder for Pionex **contract grid** (futures grid) profits.

This public repository contains **program files only**. Live Excel ledgers, SQLite databases, API keys, logs, and exports stay on the local machine.

## Local paths

Two recorders, different ledgers:

- **Excel 每日紀錄** (08:00 production): `D:\My-project\pionex grid record`
- **SQLite 本機儀表板** (HTML at 127.0.0.1:8787): `D:\My-project\pionex grid record-v2`

The folder name still has `-v2` because it is a git worktree. On screen it is the SQLite dashboard, not “v2”.

## Security

- Copy `PIONEX API.example.txt` to `PIONEX API.txt` locally. Line 1 = API key, line 2 = API secret.
- Enable **Bot reading** only. Do not enable Bot trading for this tool.
- Scripts only call GET endpoints. They never create, adjust, reduce, pause, or cancel bots.
- Never commit `PIONEX API.txt`, `GIST PUBLISH.txt`, `data/*.xlsx`, `*.sqlite`, logs, or `exports/`.

## v1.0 — daily Excel

1. `pionex-grid-api.mjs` pages running `futures_grid` orders and fetches each detail.
2. `pionex-grid-daily.ps1` writes a complete snapshot to Excel, or restores the `.bak` on failure.
3. First appearance of a grid is a baseline (daily profit 0). Later days use `today − previous`.
4. Savings products are skipped when the public API does not return them.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\pionex-grid-daily.ps1 -DryRun
```

Windows Task Scheduler `Pionex Grid Record - Daily API` runs the **Excel** recorder at 08:00 from `pionex grid record`. Do not point that task at the SQLite folder.

## SQLite 本機儀表板

Worktree `D:\My-project\pionex grid record-v2` (branch `v2/sqlite-dashboard`). Records into `v2-data/pionex-grid.sqlite` and serves `http://127.0.0.1:8787`. The 08:00 Excel task is unchanged.

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

That registers Task Scheduler `Pionex Grid SQLite Dashboard`: start at logon, then re-check `http://127.0.0.1:8787` every 30 minutes (HTTP down → restart). It does not change the 08:00 Excel task. Logs: `v2-data/dashboard.log` and `v2-data/dashboard-server.log` (local only).

- SQLite file: `v2-data/pionex-grid.sqlite` (local only)
- Export: `exports/` (does not overwrite v1 Excel)
- First successful v2 capture is a baseline (daily profit 0)
- Server binds `127.0.0.1` only and never reads the API key

Daily sqlite capture (does **not** replace the v1 08:00 Excel task):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-v2-daily-capture.ps1
```

That registers `Pionex Grid v2 Daily Capture` at 08:05. After ingest it PATCHes `pionex-grid-ledger.json` on the same private Gist used by Portfolio Tracker. Copy `GIST PUBLISH.example.txt` to `GIST PUBLISH.txt` (line 1 = Gist ID, line 2 = Gist token). Missing that file skips publish; sqlite capture still succeeds. Use `--skip-publish` to ingest only.

Current positions (does **not** write daily sqlite or Excel):

```powershell
python -m v2.capture --live
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-v2-live-publish.ps1
```

That registers `Pionex Grid v2 Live Publish` every 30 minutes: fetch running grids into `v2-data/live-snapshot.json` and PATCH Gist third file `pionex-grid-live.json`. Manual 「更新目前倉位」 on the dashboard does the same publish. The dashboard at `http://127.0.0.1:8787` reads the live file on open.

## Portfolio Tracker

關倉紀錄（v15.968）：本機新增「關倉紀錄」分頁，可補抓 API 或依派網結算明細人工補登。
API 未確認的最終淨損益保持待補；配對增量回補原快照區間，關倉淨損益不混入配對利潤或再次扣錢包。
人工補登保留修改歷史，清空欄位代表取消該欄人工覆蓋、回到 API 值或待補。
`GET /api/v1/settlements` 讀清單；`POST /api/v1/settlements/refresh` 補抓；
`POST /api/v1/settlements/{order_id}` 保存完整人工覆蓋表單（closed_at、grid_profit、lifetime_grid_profit、net_profit、funding_fee、fee、settlement_usdt_rate、note）。
資料庫 schema 11 自動相容升級；Gist schema 維持 1。補抓不覆寫當日錢包快照。

每日配對核對（v15.968）：

- API `gridProfit` 已包含已提領收益，`profitWithdrawn` 只標示提領事件，不再次加到每日／歷史配對利潤。近 24 小時另讀 API `gridProfit24h`，與每日快照區間不同。
- 每日擷取、即時快照寫入今日、關倉補抓、人工補登及歷史重算共用帳本算法；本機月曆、每日詳情、Excel 匯出、Gist／Portfolio 從帳本讀取結果。
- 寫入今日必須使用台北當日的即時快照，保留原擷取時間。跨日或時間缺失時須先更新即時資料，不能把舊快照改標為現在。
- 關倉待補時保留訂單 ID、標的及已知歷史收益；補到最後配對值時回填首次發現關倉的區間，之後日期不重複入帳。關倉總損益不作配對收益。
- 08:10 `pionex grid daily report` 報表讀取此帳本，修正歷史後須重跑報表才能更新既有 Excel。舊版 08:00 `pionex-grid-daily.ps1` 獨立相減運行中倉位，未補抓最終關倉增量，也未排除幣本位匯率重估；它不是 v2 帳本的同口徑報表，依既有要求保留原流程。

Portfolio Tracker reads `pionex-grid-ledger.json` from the private Gist. It never holds Pionex API keys and never PATCHes that file. Holdings stay in `portfolio-tracker-holdings.json`. The local dashboard remains `127.0.0.1:8787` only.

**Before changing capture, ledger JSON, Gist publish, or PT 網格頁:** read [PORTFOLIO-TRACKER-GIST.md](PORTFOLIO-TRACKER-GIST.md). Changing `schema`, the filename, or removing fields PT already reads will blank the remote 網格 tab.

## License of data

Account balances and profits in local `data/` files are private. Fixtures under `fixtures/` are synthetic.
