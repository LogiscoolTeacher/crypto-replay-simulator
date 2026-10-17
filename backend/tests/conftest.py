"""Point the application at a throw-away database before `app.main` is imported."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="replay_test_"))
os.environ["REPLAY_DATA_DIR"] = str(_TMP)
