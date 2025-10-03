# paths.py
# English comments for clarity.

from __future__ import annotations
import os
from pathlib import Path

# Project root = folder where this file lives
ROOT: Path = Path(__file__).resolve().parent

# Data dirs/files
DATA_DIR: Path = ROOT / "data"
PGN_DIR: Path = DATA_DIR / "pgn"
ECO_CACHE_FILE: Path = ROOT / "eco_ru_cache.json"

# Engine path (override via ENV if needed)
# Example for Windows: set CHESS_ENGINE=E:\engines\stockfish\stockfish.exe
ENGINE_PATH: Path = Path(os.environ.get("CHESS_ENGINE", str(ROOT / "stockfish.exe")))

# SQLite DB (if you keep it in repo root)
DB_PATH: Path = ROOT / "chess_assistant.sqlite3"

def ensure_dirs() -> None:
    """Create required dirs if missing (idempotent)."""
    PGN_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
