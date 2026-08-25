# Pionex Grid

Read-only recorder for Pionex **contract grid** (futures grid) profits.

This public repository contains **program files only**. Live Excel ledgers, SQLite databases, API keys, logs, and exports stay on the local machine.

## Local paths

- v1 (production Excel, Windows 08:00 task): `D:\My-project\pionex grid record`
- v2 (SQLite + HTML worktree, after checkout): `D:\My-project\pionex grid record-v2`

## Security

- Copy `PIONEX API.example.txt` to `PIONEX API.txt` locally. Line 1 = API key, line 2 = API secret.
- Enable **Bot reading** only. Do not enable Bot trading for this tool.
- Scripts only call GET endpoints. They never create, adjust, reduce, pause, or cancel bots.
- Never commit `PIONEX API.txt`, `data/*.xlsx`, `*.sqlite`, logs, or `exports/`.

## v1.0 — daily Excel

1. `pionex-grid-api.mjs` pages running `futures_grid` orders and fetches each detail.
2. `pionex-grid-daily.ps1` writes a complete snapshot to Excel, or restores the `.bak` on failure.
3. First appearance of a grid is a baseline (daily profit 0). Later days use `today − previous`.
4. Savings products are skipped when the public API does not return them.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\pionex-grid-daily.ps1 -DryRun
```

Windows Task Scheduler `Pionex Grid Record - Daily API` runs v1 at 08:00 from the v1 folder. Do not point that task at the v2 worktree.

## v2.0 — local SQLite dashboard

v2 lives on branch `v2/sqlite-dashboard`. It does not replace the 08:00 Excel job until you explicitly confirm.

- Capture: `python v2\capture.py`
- Dashboard: `python v2\server.py` then open `http://127.0.0.1:8787`
- The HTTP server binds localhost only. It reads SQLite, not the API key.

SQLite starts from the next successful API capture. It does not import the existing Excel history.

## Portfolio Tracker

Portfolio Tracker is already a deployed webpage. This repo does **not** push live grid numbers there. A later step can let Portfolio Tracker call the local `GET /api/v1/summary/latest` endpoint or read a local folder. That work is out of scope until the SQLite dashboard feels right.

## License of data

Account balances and profits in local `data/` files are private. Fixtures under `fixtures/` are synthetic.
