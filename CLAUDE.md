# Notes for Claude Code

**Read `HANDOFF.md` first.** It describes what exists (especially day-trading mode, which the
README doesn't cover), how to work on the repo, and what's still open.

- Run the app: `python run.py` → http://127.0.0.1:8000
- Tests: `cd backend && python3 -m pytest -q`. Use `/usr/bin/python3` without `-I`; the
  dependencies live in the user site-packages.
- Data: `data/` (replay.db, snapshots/) is the user's real data and is gitignored. A worktree has
  its own empty `data/`. To test with real candles, copy `replay.db` with sqlite's backup API into a
  scratch folder and start with `REPLAY_DATA_DIR=<copy>`. Never run experiments against the real
  `data/`.
- DB columns are added only through `MIGRATIONS` in `backend/app/replay/day_store.py`.
- Match the surrounding code: explanatory comments, plain English UI texts, a test for every
  behaviour change.
- When you finish a change, update `HANDOFF.md` (History + Open / ideas) in the same commit.
