# engine_core.py
import os, requests
from urllib.parse import quote as _urlq
import sys, asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import chess
from chess import engine
from chess.engine import PovScore
from pathlib import Path
from paths import ENGINE_PATH 


USE_ONLINE_TABLEBASE = True
USE_LICHESS_CLOUD = True

def _try_tablebase(fen: str):
    """Вернёт dict(eval, best, pv, depth, source) либо None."""
    board = chess.Board(fen)
    # Быстрый тест на кол-во фигур: если >7 — нет смысла спрашивать тб.
    if len(board.piece_map()) > 7:
        return None
    url = f"https://tablebase.lichess.ovh/standard/mainline?fen={_urlq(fen)}"
    try:
        r = requests.get(url, timeout=3.5)
        if r.status_code != 200:
            return None
        data = r.json()
        # mainline: список ходов в UCI; первый — лучший.
        line_uci = [m.get("uci") for m in (data.get("line") or []) if m.get("uci")]
        if not line_uci:
            return None
        # Преобразуем в SAN
        b = chess.Board(fen)
        san_moves = []
        for u in line_uci:
            mv = chess.Move.from_uci(u); san_moves.append(b.san(mv)); b.push(mv)
        best_san = san_moves[0]
        # Оценку дадим «табличную»: win≈+100, draw≈0, loss≈-100 (для маркировки)
        category = (data.get("category") or "").lower()
        cp = 100.0 if "win" in category else (0.0 if "draw" in category else -100.0)
        return {"eval": cp, "best": best_san, "pv": " ".join(san_moves), "depth": 99, "source": "syzygy"}
    except Exception:
        return None

def _try_lichess_cloud(fen: str, depth: int, multipv: int = 1):
    """
    Берём кешированную оценку, если глубина близка к нужной.
    Чтобы не душить лимиты, держим timeout маленьким.
    Можно задать токен в LICHESS_TOKEN для более щедрых лимитов.
    """
    if multipv < 1: multipv = 1
    url = f"https://lichess.org/api/cloud-eval?fen={_urlq(fen)}&multiPv={multipv}"
    headers = {"Accept": "application/json"}
    tok = os.environ.get("LICHESS_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.get(url, headers=headers, timeout=3.5)
        if r.status_code != 200:
            return None
        j = r.json()
        cloud_depth = int(j.get("depth") or 0)
        pvs = j.get("pvs") or []
        if not pvs:
            return None
        # принимаем кеш, если он не «слишком мелкий»
        if cloud_depth < max(8, depth - 2):
            return None
        # Разбираем первую линию в SAN
        board = chess.Board(fen)
        seq = (pvs[0].get("moves") or "").split()
        san, b = [], board.copy()
        for u in seq:
            try:
                mv = chess.Move.from_uci(u); san.append(b.san(mv)); b.push(mv)
            except Exception:
                break
        # Оценка из cp/mate
        cp = pvs[0].get("cp"); mate = pvs[0].get("mate")
        ev = 100.0 if (mate and mate > 0) else (-100.0 if (mate and mate < 0) else ((cp or 0)/100.0))
        best = ""
        if seq:
            try: best = board.san(chess.Move.from_uci(seq[0]))
            except Exception: pass
        return {"eval": ev, "best": best, "pv": " ".join(san), "depth": cloud_depth, "source": "lichess_cloud"}
    except Exception:
        return None
    
def fetch_opening_stats(fen: str, max_moves: int = 20, top_n: int = 6):
    """
    Возвращает словарь с 'opening_name' (если есть) и топ-ходами книги Lichess.
    """
    url = f"https://explorer.lichess.ovh/lichess?variant=standard&fen={_urlq(fen)}&moves={max_moves}&topGames=0&recentGames=0"
    try:
        r = requests.get(url, timeout=3.5)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        moves = data.get("moves") or []
        eco = (data.get("opening") or {}).get("eco")
        name_en = (data.get("opening") or {}).get("name")

        out = {
            "eco": eco,
            "opening_name": name_en,
            "moves": []  # ← только обработанные записи
        }
        for m in moves[:top_n]:
            total = int(m.get("white", 0)) + int(m.get("draws", 0)) + int(m.get("black", 0))
            out["moves"].append({
                "san":  m.get("san"),
                "uci":  m.get("uci"),
                "played": total,
                "white": int(m.get("white", 0)),
                "draws": int(m.get("draws", 0)),
                "black": int(m.get("black", 0)),
                "perf":  m.get("performance")
            })
        return out
    except Exception:
        return None

# ─── анализ с учётом фазы партии ─────────────────────────────────────────
def analyse_with_phase(
    board: chess.Board,
    phase: str,
    multipv_open: int = 3,
    target_depth: int | None = None,
):
    """
    Возвращает либо dict (наш нормализованный ответ от облака/таблабазы),
    либо Info от python-chess, как и раньше (когда считаем локально).
    """
    fen = board.fen()

    # Выставим лимит, количество линий и «желаемую глубину» под Cloud Eval
    if phase == "opening":
        limit = engine.Limit(time=0.3)         # раньше 0.7
        lines = max(1, int(multipv_open))
        depth_for_cloud = target_depth or 16
    elif phase == "endgame":
        limit = engine.Limit(depth=15)         # раньше 25
        lines = 1
        depth_for_cloud = 15
    else:  # middlegame
        limit = engine.Limit(time=0.6)         # раньше 1.5
        lines = 2
        depth_for_cloud = target_depth or 18
    
    opening_stats = None
    if phase == "opening":
        opening_stats = fetch_opening_stats(fen, max_moves=20, top_n=6)

    res = None
    if 'USE_ONLINE_TABLEBASE' in globals() and USE_ONLINE_TABLEBASE:
        tb = _try_tablebase(fen)
        if tb:
            res = tb
    if res is None and 'USE_LICHESS_CLOUD' in globals() and USE_LICHESS_CLOUD:
        cloud = _try_lichess_cloud(fen, depth=depth_for_cloud, multipv=lines)
        if cloud:
            res = cloud
    if res is None:
        with engine.SimpleEngine.popen_uci(str(ENGINE_PATH)) as sf:
            res = sf.analyse(board, limit, multipv=lines)

    # Подмешаем книгу как дополнительное поле (не ломает существующую логику)
    if isinstance(res, dict):
        res.setdefault("aux", {})["opening_stats"] = opening_stats
    else:
        # InfoDict от движка — завернём в словарь совместимый с остальным кодом, если хочешь.
        # Но если далее твой код ожидает InfoDict — просто верни как есть и прикрути книгу в том месте.
        pass

    return res


# ─── универсальный анализ позиции (для kb.py и др.) ──────────────────────
def analyze_fen(fen: str, depth: int = 18) -> dict:
    """
    Вернуть словарь {"eval": float, "best": str, "pv": str, "depth": int}
    eval — оценка в пешках (POV side-to-move).
    best — лучший ход в SAN.
    pv   — короткий вариант (до 5 ходов).
    """
    board = chess.Board(fen)
    with engine.SimpleEngine.popen_uci(str(ENGINE_PATH)) as sf:
        info = sf.analyse(board, engine.Limit(depth=depth), multipv=1)

    # python-chess может вернуть dict или объект
    if isinstance(info, list):
        info = info[0]

    score = info.get("score")
    cp = 0.0
    if score is not None:
        if isinstance(score, PovScore):
            score = score.pov(board.turn)
        mate = score.mate()
        if mate is not None:
            cp = 100.0 if mate > 0 else -100.0
        else:
            cp = (score.score() or 0) / 100.0
    pv_moves = info.get("pv") or []
    try:
        pv_san = board.variation_san(pv_moves[:5])
    except Exception:
        pv_san = ""

    best_move = ""
    if pv_moves:
        try:
            best_move = board.san(pv_moves[0])
        except Exception:
            best_move = pv_moves[0].uci()

    return {
        "eval": cp,
        "best": best_move,
        "pv": pv_san,
        "depth": depth,
    }




