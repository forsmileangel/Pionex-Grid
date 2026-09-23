# Pionex Grid

Read-only recorder for Pionex **contract grid** (futures grid) profits.

This public repository contains **program files only**. Live Excel ledgers, SQLite databases, API keys, logs, and exports stay on the local machine.

## Local paths

- Excel (Windows 08:12 task): `D:\My-project\pionex grid record`
- v2 (SQLite + HTML worktree, after checkout): `D:\My-project\pionex grid record-v2`

## Security

- Copy `PIONEX API.example.txt` to `PIONEX API.txt` locally. Line 1 = API key, line 2 = API secret.
- Enable **Bot reading** only. Do not enable Bot trading for this tool.
- Scripts only call GET endpoints. They never create, adjust, reduce, pause, or cancel bots.
- Never commit `PIONEX API.txt`, `data/*.xlsx`, `*.sqlite`, logs, or `exports/`.

## v15.968 — daily Excel from the v2 ledger

1. v2 captures into SQLite at 08:05. `pionex-grid-daily.ps1` reads it through `python -m v2.legacy_excel` at 08:12, without fetching the API or writing SQLite.
2. The original `GridLedgerTable`, 16 columns, formatting and savings worksheet are retained. Grid keys now use the unique order ID. Daily profits come directly from the ledger, including withdrawal, reinvestment, native-coin conversion and closing adjustments.
3. Each run rebuilds all available dates, so a late settlement repairs its original interval. Repeating the export does not duplicate rows. A pending close keeps its daily cell blank and is marked pending. Closing net losses are not paired income.
4. The existing cumulative column remains the sum of daily profit **during the recorded period**, with baseline zero. It is not the historical lifetime figure including baseline balances shown by Portfolio and the 08:10 report.
5. A timestamped `data/snapshot-legacy-excel-*.xlsx` backup precedes each update. The exporter saves to a temporary workbook, then atomically replaces the original. Missing/stale ledger data, unknown Excel dates, file locks or concurrent edits preserve the original.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\pionex-grid-daily.ps1 -DryRun
```

The existing task name `Pionex Grid Record - Daily API` is retained, but it now exports SQLite at **08:12**. Run `install-legacy-excel-task.ps1` to update its trigger and hidden action while preserving its principal/settings. The old task XML is saved in `runtime/` first.

`-Backfill` / `-AllowStale` rebuild a historical ledger even if its last date is before today. A supplied `-ReplaceDate` must exist in SQLite; the entire export still rebuilds to repair later cumulative amounts. `-SnapshotJson` and `-CaptureAt` cannot bypass v2: import such snapshots into v2 first, then rebuild Excel. The v2 worktree and its Python runtime must remain installed.

## v2.0 — local SQLite dashboard

v2 lives on branch `v2/sqlite-dashboard` and owns all daily-profit calculations. The user approved moving the legacy Excel report to this ledger in v15.968.

- Capture (from the v2 worktree): `python -m v2.capture`
- Dashboard: `python -m v2.server` then open `http://127.0.0.1:8787`
- The HTTP server binds localhost only. It reads SQLite, not the API key.

SQLite starts from the next successful API capture. It does not import the existing Excel history.

## Portfolio Tracker

Portfolio Tracker reads the v2 ledger published to a private Gist. Both Excel reports now read the same SQLite daily amounts. Excel export never updates the Gist or holdings.

## License of data

Account balances and profits in local `data/` files are private. Fixtures under `fixtures/` are synthetic.
