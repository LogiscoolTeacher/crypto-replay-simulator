"""Start the Replay Trading server.

    python run.py              # http://127.0.0.1:8000
    python run.py --port 9000
    python run.py --reload     # auto-reload on code changes (development)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

BACKEND = Path(__file__).resolve().parent / "backend"


def main() -> int:
    p = argparse.ArgumentParser(description="Run the Replay Trading API server.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    sys.path.insert(0, str(BACKEND))
    print(f"Replay Trading -> http://{args.host}:{args.port}  (docs at /docs)")
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(BACKEND)] if args.reload else None,
        app_dir=str(BACKEND),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
