# === [ADDED] Lightweight exact-FEN advice layer (non-destructive) ===
from __future__ import annotations
from helpers import material_signature
from typing import Dict, Any, Optional
import re
import sqlite3
from pathlib import Path

DB_PATH = Path("chess_kb.sqlite3")

def _conn():
    return sqlite3.connect(DB_PATH)

def _fen4(fen: str) -> str:
    return " ".join(fen.split()[:4])  # первые 4 поля FEN

def _sort_rows(rows):
    return rows

# Попробуем взять уже существующие функции, если они есть у вас.
try:
    import db as _cache_db  
except Exception:
    _cache_db = None

def get_position(fen: str):
    return _cache_db.get_position(fen) if (_cache_db and hasattr(_cache_db, "get_position")) else None

def upsert_position(fen, side, phase, sf_eval, sf_best, pv, depth, motifs):
    if _cache_db and hasattr(_cache_db, "upsert_position"):
        return _cache_db.upsert_position(fen, side, phase, sf_eval, sf_best, pv, depth, motifs)

def get_plan(fen: str):
    return _cache_db.get_plan(fen) if (_cache_db and hasattr(_cache_db, "get_plan")) else None

def upsert_plan(fen, advice, opponent_ideas="", pitfalls="", examples=None):
    if _cache_db and hasattr(_cache_db, "upsert_plan"):
        return _cache_db.upsert_plan(fen, advice, opponent_ideas, pitfalls, examples)

_FEN_RE = re.compile(r"^(\S+\s+[wb]\s+\S+)(?:\s+\d+\s+\d+)?$")

def normalize_fen(fen: str) -> str:
    parts = fen.strip().split()
    return " ".join(parts[:4]) if len(parts) >= 4 else fen

def _infer_phase(fen: str) -> str:
    parts = fen.split()
    board = parts[0] if parts else ""
    heavy = sum(board.count(p) for p in "qrbnQRBN")
    pawns = sum(board.count(p) for p in "pP")
    if heavy > 10 and pawns >= 12: return "opening"
    if heavy <= 6 and pawns <= 10: return "endgame"
    return "middlegame"

def _engine_analyze(fen: str, depth: int = 18) -> Optional[Dict[str, Any]]:
    try:
        # ожидается ваша функция с сигнатурой analyze_fen(fen, depth) -> {"eval","best","pv","depth"}
        from engine_core import analyze_fen
        return analyze_fen(fen, depth=depth)
    except Exception:
        return None

def _draft_plan_from_pv(sf_eval: float, best: str, pv: str, phase: str) -> Dict[str, str]:
    sign = "≈" if abs(sf_eval) < 0.3 else ("+" if sf_eval > 0 else "−")
    advice_lines = [
        f"Сыграть {best}: главный ход по оценке движка ({sign}{abs(sf_eval):.2f}).",
        "Улучшить координацию фигур и не пустить контригру соперника.",
        "Следовать первому варианту (PV) 1–2 хода, пока структура не изменится."
    ]
    return {
        "advice": "• " + "\n• ".join(advice_lines),
        "opponent_ideas": "Соперник стремится активизировать фигуры на уязвимых полях; проверяйте тактику на ближайшие 1–2 хода.",
        "pitfalls": "Типовые промахи: ослабление ключевых полей и игнор первых двух ходов PV."
    }

def kb_get_advice_for_fen(fen: str, depth: int = 18) -> Dict[str, Any]:
    """
    Лёгкая надстройка над кэшем и движком:
      1) Преобразуем входной FEN к полному (6 полей) и ключу кэша (первые 4 поля).
      2) Пытаемся взять позицию из кэша по fen_key.
      3) Если в кэше нет – считаем движком по fen_full и кладём в кэш (positions).
      4) Возвращаем короткий план; если его нет – делаем черновик и сохраняем (plans).
    Возвращаем единый словарь для planner: eval/best/pv/depth/phase + advice/opponent_ideas/pitfalls.
    """
    fen = (fen or "").strip()
    parts = fen.split()

    # --- Полный FEN для движка и ключ кэша для БД -------------------------
    if len(parts) >= 6:
        fen_full = " ".join(parts[:6])              # ровно 6 полей
    elif len(parts) >= 4:
        fen_full = " ".join(parts[:4] + ["0", "1"]) # добавим halfmove/fullmove
    else:
        # пришёл некорректный/короткий FEN — вернём ошибку честно
        return {"found": False, "error": "bad_fen", "fen": fen}

    fen_key = " ".join(fen_full.split()[:4])        # ключ кэша == первые 4 поля
    side = fen_full.split()[1][0] if len(fen_full.split()) > 1 else "w"

    # --- 1) Попытка прочитать кэш ------------------------------------------
    pos = None
    try:
        pos = get_position(fen_key)
    except Exception:
        pos = None

    if pos:
        sf_eval = float(pos.get("sf_eval") or 0.0)
        best    = pos.get("sf_best") or ""
        pv      = pos.get("pv") or ""
        d       = int(pos.get("depth") or depth)
        phase   = pos.get("phase") or _infer_phase(fen_full)
        if not side:
            side = (pos.get("side") or "w")
    else:
        # --- 2) Анализ движком --------------------------------------------
        eng = _engine_analyze(fen_full, depth=depth)
        if not eng:
            return {"found": False, "error": "engine_unavailable", "fen": fen_full}

        sf_eval = float(eng.get("eval", 0.0))
        best    = eng.get("best", "") or ""
        pv      = eng.get("pv", "") or ""
        d       = int(eng.get("depth", depth))
        phase   = _infer_phase(fen_full)

        # Сохраняем результат в кэш позиций
        try:
            upsert_position(
                fen_key, side=side, phase=phase,
                sf_eval=sf_eval, sf_best=best, pv=pv, depth=d, motifs=[]
            )
        except Exception:
            # Не мешаем UI из-за проблем БД
            pass

    # --- 3) План: берём из БД или генерим черновик -------------------------
    plan_row: Optional[Dict[str, Any]] = None
    try:
        plan_row = get_plan(fen_key)
    except Exception:
        plan_row = None

    if not plan_row:
        draft = _draft_plan_from_pv(sf_eval, best, pv, phase)
        try:
            upsert_plan(
                fen_key,
                advice=draft["advice"],
                opponent_ideas=draft["opponent_ideas"],
                pitfalls=draft["pitfalls"],
                examples=[]
            )
            plan_row = get_plan(fen_key)  # попробуем перечитать то, что записали
        except Exception:
            # Если запись не удалась — вернём черновик напрямую
            plan_row = draft

    # --- 4) Сбор единого ответа -------------------------------------------
    res: Dict[str, Any] = {
        "found": True,
        "fen":   fen_full,   # наружу всегда отдаём полный FEN (6 полей)
        "phase": phase,
        "sf_eval": sf_eval,
        "best":   best,
        "pv":     pv,
        "depth":  d,
    }

    # Присоединим текст плана из БД или черновик
    if isinstance(plan_row, dict):
        res["advice"]         = plan_row.get("advice", "")
        res["opponent_ideas"] = plan_row.get("opponent_ideas", "")
        res["pitfalls"]       = plan_row.get("pitfalls", "")
        # examples можно добавить позже при необходимости

    return res

# === [ADDED] Тематический поиск эндшпилей: ладейник 4x3 на одном фланге ===
import chess, random
from typing import Optional, Literal, Dict, Any, List

def _pawn_files_mask(board: chess.Board, color: bool) -> int:
    """Битовая маска файлов с пешками: бит i = есть пешка на файле 'a'+i."""
    mask = 0
    for sq in board.pieces(chess.PAWN, color):
        file_idx = chess.square_file(sq)  # 0..7
        mask |= (1 << file_idx)
    return mask

def _only_one_wing(mask: int) -> Optional[str]:
    """True-‘wing’ если все пешки на одной стороне доски.
       Возвращает 'a-c' (ферзевый), 'f-h' (королевский) или None.
       Условие: файлы только в {a,b,c} ИЛИ только в {f,g,h}. d/e считаем центром → не допускаем.
    """
    queenside = (mask & 0b00000111)   # a,b,c
    kingside  = (mask & 0b11100000)   # f,g,h
    center    = (mask & 0b00011000)   # d,e
    if center:
        return None
    if mask != 0 and mask == queenside:
        return "a-c"
    if mask != 0 and mask == kingside:
        return "f-h"
    return None

def _is_rook_4v3_same_wing(board: chess.Board) -> bool:
    """Строгая проверка шаблона: у каждой стороны ровно 1 ладья и только короли/ладьи/пешки;
       пешки 4 vs 3; у обеих сторон пешки на одном и том же фланге (a-c или f-h)."""
    # материал только K,R,P?
    for p in [chess.QUEEN, chess.BISHOP, chess.KNIGHT]:
        if board.pieces(p, chess.WHITE) or board.pieces(p, chess.BLACK):
            return False

    WR = len(board.pieces(chess.ROOK, chess.WHITE))
    BR = len(board.pieces(chess.ROOK, chess.BLACK))
    WP = len(board.pieces(chess.PAWN, chess.WHITE))
    BP = len(board.pieces(chess.PAWN, chess.BLACK))

    if WR != 1 or BR != 1:
        return False
    # допускаем 4x3 в любой ориентации (белые=4/чёрные=3 или наоборот)
    pair_4v3 = (WP == 4 and BP == 3) or (WP == 3 and BP == 4)
    if not pair_4v3:
        return False

    wmask = _pawn_files_mask(board, chess.WHITE)
    bmask = _pawn_files_mask(board, chess.BLACK)
    wwing = _only_one_wing(wmask)
    bwing = _only_one_wing(bmask)
    # «на одном фланге» = и у белых, и у чёрных на одном (одинаковом) крыле
    return (wwing is not None) and (bwing is not None) and (wwing == bwing)

def suggest_rook_4v3_same_wing(limit: int = 5, sample: int = 50) -> List[Dict[str, Any]]:
    """Выбрать кандидатов из БД: phase='endgame' и material_signature в {KRPPPP vs KRPPP, KRPPP vs KRPPPP},
       затем отфильтровать «один фланг». Возвращает список словарей с fen+метаданными.
       sample — сколько сырых строк взять на предварительную проверку (экономим время).
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # учтём обе ориентации из-за сортировки в material_signature
    sigA = "KRPPPP vs KRPPP"
    sigB = "KRPPP vs KRPPPP"
    cur.execute("""
      SELECT positions.fen, positions.phase, positions.comment,
             games.white, games.black, games.result, games.eco, games.opening
      FROM positions JOIN games USING(game_id)
      WHERE positions.phase='endgame'
        AND positions.material_signature IN (?,?)
      LIMIT ?;
    """, (sigA, sigB, max(sample, limit)))
    rows = cur.fetchall()
    con.close()

    out = []
    for r in rows:
        fen = r[0]
        try:
            b = chess.Board(fen)
        except Exception:
            continue
        if _is_rook_4v3_same_wing(b):
            out.append({
                "fen": fen,
                "phase": r[1],
                "title": f"{(r[3] or '').strip()}–{(r[4] or '').strip()} {(r[5] or '').strip()}".strip(" –"),
                "info": " ".join(x for x in [(r[6] or ""), (r[7] or "")] if x).strip(),
                "comment": (r[2] or "").strip()
            })
    # перемешаем слегка и отдадим top-N
    random.shuffle(out)
    return out[:limit]

def pick_rook_4v3_candidate() -> Optional[Dict[str, Any]]:
    """Удобный вход: вернуть ОДНОГО кандидата или None."""
    lst = suggest_rook_4v3_same_wing(limit=1, sample=80)
    return lst[0] if lst else None
# === [/ADDED] ===
# --- оставить как есть: GOOD_NAGS/BAD_NAGS/_sort_rows/_conn/_fen4 ---

def find_exact_by_fen(fen: str, limit=8):
    prefix = _fen4(fen) + " "
    con=_conn(); cur=con.cursor()
    cur.execute("""
      SELECT positions.fen, positions.phase, positions.comment,
             games.white, games.black, games.result, games.eco, games.opening,
             COALESCE(positions.is_mainline,0) AS is_mainline,
             positions.ply,
             COALESCE(positions.nags,'[]') as nags
      FROM positions JOIN games USING(game_id)
      WHERE positions.fen LIKE ?
      ORDER BY is_mainline DESC, positions.ply ASC
      LIMIT ?;
    """, (prefix+"%", limit))
    rows=cur.fetchall(); con.close(); return rows

def find_similar_endgame_by_material(fen: str, limit=8):
    board=chess.Board(fen); msig= material_signature(board)
    con=_conn(); cur=con.cursor()
    cur.execute("""
      SELECT positions.fen, positions.phase, positions.comment,
             games.white, games.black, games.result, games.eco, games.opening,
             COALESCE(positions.is_mainline,0) AS is_mainline,
             positions.ply,
             COALESCE(positions.nags,'[]') as nags
      FROM positions JOIN games USING(game_id)
      WHERE positions.phase='endgame' AND positions.material_signature=?
      ORDER BY is_mainline DESC, positions.ply ASC
      LIMIT ?;
    """, (msig, limit))
    rows=cur.fetchall(); con.close(); return rows

def find_opening_by_eco_prefix(eco: str, limit=12):
    if not eco: return []
    con=_conn(); cur=con.cursor()
    like = eco.strip().upper()[:2] + "%"
    cur.execute("""
      SELECT positions.fen, positions.phase, positions.comment,
             games.white, games.black, games.result, games.eco, games.opening,
             COALESCE(positions.is_mainline,0) AS is_mainline,
             positions.ply,
             COALESCE(positions.nags,'[]') as nags
      FROM positions JOIN games USING(game_id)
      WHERE games.eco LIKE ? AND positions.ply <= 20
      ORDER BY is_mainline DESC, positions.ply ASC
      LIMIT ?;
    """, (like, limit))
    rows=cur.fetchall(); con.close(); return rows

def auto_query(fen: str, limit=8):
    exact = _sort_rows(find_exact_by_fen(fen, limit*3))
    if exact:
        return {"mode":"exact", "rows": exact[:limit]}
    # попытаемся найти по материалу (сработает только если эндшпиль)
    try:
        ms = _sort_rows(find_similar_endgame_by_material(fen, limit*3))
        if ms:
            return {"mode":"endgame_msig", "rows": ms[:limit]}
    except Exception:
        pass
    return {"mode":"none", "rows":[]}

