# db.py      just have this file (not uses now)
from __future__ import annotations
import sqlite3, json, threading, os
from typing import Optional, Any, Dict, List, Tuple

_DB_PATH = os.environ.get("CHESS_DB_PATH", "chess_assistant.sqlite3")
DB_SQLITE = str(_DB_PATH)
_lock = threading.RLock()

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS positions (
  fen TEXT PRIMARY KEY,
  side CHAR(1),
  phase TEXT,
  sf_eval REAL,
  sf_best TEXT,
  pv TEXT,
  depth INT,
  motifs TEXT    -- JSON array of tags
);
CREATE TABLE IF NOT EXISTS games (
  game_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event TEXT, site TEXT, date TEXT, white TEXT, black TEXT,
  result TEXT, white_elo INT, black_elo INT, eco TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS moves (
  game_id INTEGER, ply INTEGER, san TEXT,
  fen_before TEXT, fen_after TEXT,
  PRIMARY KEY (game_id, ply)
);
CREATE TABLE IF NOT EXISTS plans (
  fen TEXT PRIMARY KEY,
  advice TEXT,         -- краткий план (список строк в JSON или просто текст)
  opponent_ideas TEXT,
  pitfalls TEXT,
  examples TEXT        -- JSON: [{"game_id":..,"ply":..}, ...]
);
-- FTS для текстового поиска по планам
CREATE VIRTUAL TABLE IF NOT EXISTS plans_fts USING fts5(
  fen, advice, opponent_ideas, pitfalls, content='plans', content_rowid='rowid'
);
"""

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

_conn = _connect()
with _lock:
    _conn.executescript(_SCHEMA)
    _conn.commit()

def upsert_position(fen: str, side: str, phase: str,
                    sf_eval: float, sf_best: str, pv: str, depth: int,
                    motifs: Optional[List[str]] = None) -> None:
    motifs_json = json.dumps(motifs or [], ensure_ascii=False)
    with _lock:
        _conn.execute("""
            INSERT INTO positions(fen, side, phase, sf_eval, sf_best, pv, depth, motifs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fen) DO UPDATE SET
              side=excluded.side, phase=excluded.phase,
              sf_eval=excluded.sf_eval, sf_best=excluded.sf_best,
              pv=excluded.pv, depth=max(positions.depth, excluded.depth),
              motifs=excluded.motifs
        """, (fen, side, phase, sf_eval, sf_best, pv, depth, motifs_json))
        _conn.commit()

def get_position(fen: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _conn.execute("SELECT * FROM positions WHERE fen=?", (fen,)).fetchone()
    return dict(row) if row else None

def upsert_plan(fen: str,
                advice: str,
                opponent_ideas: str = "",
                pitfalls: str = "",
                examples: Optional[List[Dict[str, Any]]] = None) -> None:
    examples_json = json.dumps(examples or [], ensure_ascii=False)
    with _lock:
        _conn.execute("""
            INSERT INTO plans(fen, advice, opponent_ideas, pitfalls, examples)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(fen) DO UPDATE SET
              advice=excluded.advice,
              opponent_ideas=excluded.opponent_ideas,
              pitfalls=excluded.pitfalls,
              examples=excluded.examples
        """, (fen, advice, opponent_ideas, pitfalls, examples_json))
        # синхронизация с FTS
        _conn.execute("INSERT INTO plans_fts(rowid, fen, advice, opponent_ideas, pitfalls) "
                      "SELECT rowid, fen, advice, opponent_ideas, pitfalls FROM plans WHERE fen=?",
                      (fen,))
        _conn.commit()

def get_plan(fen: str) -> Optional[Dict[str, Any]]:
    with _lock:
        row = _conn.execute("SELECT * FROM plans WHERE fen=?", (fen,)).fetchone()
    return dict(row) if row else None

def get_examples(fen: str, limit: int = 3) -> List[Dict[str, Any]]:
    with _lock:
        row = _conn.execute("SELECT examples FROM plans WHERE fen=?", (fen,)).fetchone()
    if not row or not row["examples"]:
        return []
    try:
        data = json.loads(row["examples"])
        return data[:limit]
    except Exception:
        return []

# опционально: быстрый поиск позиций по участию в игре (для «перейти к моменту»)
def search_moves_by_fen(fen: str, limit: int = 20) -> List[Dict[str, Any]]:
    with _lock:
        rows = _conn.execute("""
            SELECT game_id, ply, san, fen_before, fen_after
            FROM moves
            WHERE fen_before=? OR fen_after=?
            LIMIT ?
        """, (fen, fen, limit)).fetchall()
    return [dict(r) for r in rows]
