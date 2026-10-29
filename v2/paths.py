"""Where this checkout and the folders around it live; other modules derive their paths from here.

ROOT is this checkout. PRODUCTION_REPO is the live checkout that the scheduled tasks and the 8787
dashboard run; a worktree such as -v2-claude is "outside production", which keeps tests and
previews away from the real Excel files. LEGACY_ROOT is the v1 folder beside it: it still holds
the Pionex API key file, the 16-column Excel and the daily report Excel.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_REPO = Path(r"D:\My-project\pionex grid record-v2")
LEGACY_ROOT = PRODUCTION_REPO.parent / "pionex grid record"
