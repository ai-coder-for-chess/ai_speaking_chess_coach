# coach_session.py
# Voice-first chess coaching session:
# - Finds games by opponent from analysis_out / Analyze Out / Analyse Out
# - Keeps a single Stockfish instance alive
# - Auto-jumps to the first branching point (moves 4–6 when possible)
# - Proposes alternate lines and waits for voice commands
# - Deep "what-if" analysis to depth=23 with visible progress in console
#
# Python: 3.13.6
# python-chess: 1.11.2
#
# Comments are in English (as requested).

from __future__ import annotations
from speech_ru import opening_title_to_speech, san_to_speech, pv_to_speech, apply_san_sequence
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from pathlib import Path
from eco_ru import name_from_eco
import threading
import time
import unicodedata
import chess
import chess.pgn
import chess.engine
import re

from planner import plan_for_fen
from llm_util import ask
from name_normalize import match_names
from engine_core import ENGINE_PATH, fetch_opening_stats
from paths import DATA_DIR, PGN_DIR, ECO_CACHE_FILE, ENGINE_PATH, DB_PATH

PGN_PATH  = str(PGN_DIR)
ECO_PATH  = str(ECO_CACHE_FILE)
DB_SQLITE = str(DB_PATH)
ENGINE    = str(ENGINE_PATH)
ECO_CACHE_PATH = ECO_PATH
DB_FILE        = DB_SQLITE
STOCKFISH_PATH = ENGINE


# --- Mistake thresholds in centipawns (same logic as game_analyzer.py) ---
THRESH_INACCURACY = 50    # ?!
THRESH_MISTAKE    = 150   # ?
THRESH_BLUNDER    = 300   # ??
ALT_TOL_CP        = 25    # равносильные альтернативы (в пределах 25cp)


# ---------- State containers ----------

@dataclass
class CoachState:
    game: Optional[chess.pgn.Game] = None
    nodes_mainline: List[chess.pgn.ChildNode] = field(default_factory=list)
    board: chess.Board = field(default_factory=chess.Board)
    ply_idx: int = 0
    file_name: str = ""
    user_side: Optional[bool] = None  # True=White, False=Black, None=Unknown
    last_branch: Optional[Dict[str, Any]] = None
    opening_announced: bool = False
    last_opening_eco: Optional[str] = None

# ---------- Engine wrapper ----------

class EngineSession:
    """Thread-safe wrapper around a single Stockfish process."""
    def __init__(self, path: str, threads: int = 2, hash_mb: int = 256):
        self._lock = threading.RLock()
        self._engine = chess.engine.SimpleEngine.popen_uci(path)
        self._engine.configure({"Threads": threads, "Hash": hash_mb})

    def analyse(self, board: chess.Board, depth: int = 18, multipv: int = 2):
        with self._lock:
            return self._engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)

    def quit(self):
        with self._lock:
            try:
                self._engine.quit()
            except Exception:
                pass


# ---------- Coach session ----------

class CoachSession:
    """High-level coach: open game, navigate, propose branches, answer, run deep what-if."""

    def __init__(self, engine_path: str):
        self.state = CoachState()
        self.sf = EngineSession(engine_path)

    # ===== Name normalization & matching =====

    @staticmethod
    def _ru2lat(s: str) -> str:
        """Rough Cyrillic→Latin transliteration for fuzzy name match."""
        tbl = {
            "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y","к":"k",
            "л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"kh","ц":"ts",
            "ч":"ch","ш":"sh","щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
        }
        out = []
        for ch in s.lower():
            out.append(tbl.get(ch, ch))
        return "".join(out)

    @staticmethod
    def _norm(s: str) -> str:
        """
        Normalize: lowercase, ru→lat, strip accents, collapse spaces.
        Also map 'ule'→'ole', 'kristian'→'christian' to handle 'Уле Кристиан' == 'Ole Christian'.
        """
        s = s.strip().lower()
        s = CoachSession._ru2lat(s)
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = " ".join(s.split())
        s = s.replace("ule", "ole")
        s = s.replace("kristian", "christian")
        return s

    @staticmethod
    def _name_match(hay: str, needle: str) -> bool:
        """Token-wise fuzzy match (prefix overlap)."""
        h = CoachSession._norm(hay)
        n = CoachSession._norm(needle)
        if not n:
            return False
        if n in h:
            return True
        ht = h.replace("-", " ").split()
        nt = [t for t in n.replace("-", " ").split() if len(t) >= 3]
        score = 0
        for t in nt:
            if any(x.startswith(t) or t.startswith(x) for x in ht):
                score += 1
        need = 2 if len(nt) >= 2 else 1
        return score >= need

    # ===== PGN discovery =====

    @staticmethod
    def find_latest_pgn(candidates: List[Path], today_only: bool = True) -> Optional[Path]:
        files: List[Path] = []
        for d in candidates:
            if not d or not d.exists():
                continue
            files.extend(sorted(d.glob("*_annotated.pgn"), key=lambda p: p.stat().st_mtime, reverse=True))
            files.extend(sorted(d.glob("*.pgn"), key=lambda p: p.stat().st_mtime, reverse=True))
        if not files:
            return None
        if today_only:
            now = time.localtime()
            for p in files:
                t = time.localtime(p.stat().st_mtime)
                if t.tm_year == now.tm_year and t.tm_yday == now.tm_yday:
                    return p
        return files[0]

    @staticmethod
    def find_pgn_by_opponent(candidates: List[Path], opponent: str) -> Optional[Path]:
        """
        Search candidate folders for *.pgn that matches opponent in [White]/[Black] tags or filename.
        Fuzzy, accent-insensitive, Cyrillic-tolerant (via name_normalize.match_names).
        """
        found: List[Path] = []
        for d in candidates:
            if not d or not d.exists():
                continue
            # если хочешь искать и в подпапках — замени glob("*.pgn") на rglob("*.pgn")
            for p in sorted(d.glob("*.pgn"), key=lambda q: q.stat().st_mtime, reverse=True):
                try:
                    # 1) по имени файла
                    if match_names(p.name, opponent):
                        found.append(p); continue
                    # 2) по тегам White/Black в заголовке
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        headers = chess.pgn.read_headers(f)
                    if not headers:
                        continue
                    w = headers.get("White", "") or ""
                    b = headers.get("Black", "") or ""
                    if match_names(w, opponent) or match_names(b, opponent):
                        found.append(p)
                except Exception:
                    continue
        return found[0] if found else None


    # ===== Load & navigation =====

    def load_pgn(self, path: Path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            game = chess.pgn.read_game(f)
        if not game:
            raise ValueError("В PGN не нашлось партии.")
        self.state.game = game
        self.state.file_name = str(path)

        # Build mainline
        ml: List[chess.pgn.ChildNode] = []
        node: chess.pgn.GameNode = game
        while node.variations:
            node = node.variations[0]
            ml.append(node)

        self.state.nodes_mainline = ml
        self.state.board = game.board()
        self.state.ply_idx = 0
        self.state.user_side = self._detect_user_side()
        self.state.last_branch = None
        return self.current_status()

    def _detect_user_side(self) -> Optional[bool]:
        """Detect user's color by headers; returns True=White, False=Black, None=Unknown."""
        if not self.state.game:
            return None
        h = self.state.game.headers
        w = h.get("White", "") or ""
        b = h.get("Black", "") or ""
        me_tokens = ["mitusov", "Mitusov", "митусов", "Митусов", "Семен Митусов",
                    "semen", "семен", "семён", "semen mitusov", "Semen Mitusov"]
        w_hit = any(match_names(w, t) for t in me_tokens)
        b_hit = any(match_names(b, t) for t in me_tokens)
        if w_hit and not b_hit:
            return True
        if b_hit and not w_hit:
            return False
        return None
    
    def goto_by_san(self, san_query: str) -> Dict[str, Any]:
        """
        Find first mainline move whose SAN equals san_query (ignoring +/#/annotations)
        and go to the position **before** that move.
        """
        if not self.state.game:
            return {"error": "no_game"}
        # Normalize query
        q = san_query.strip()
        q = q.replace("+", "").replace("#", "").replace("!", "").replace("?", "")
        b = self.state.game.board()
        for i, node in enumerate(self.state.nodes_mainline):
            try:
                san = b.san(node.move)
            except Exception:
                san = ""
            norm = san.replace("+", "").replace("#", "").replace("!", "").replace("?", "")
            if norm.lower() == q.lower():
                # go to move i (before it is played)
                return self.goto_ply(i)
            b.push(node.move)
        return {"error": "not_found"}

    def goto_ply(self, ply: int):
        if not self.state.game:
            return {"error": "no_game"}
        ply = max(0, min(ply, len(self.state.nodes_mainline)))
        b = self.state.game.board()
        for i in range(ply):
            b.push(self.state.nodes_mainline[i].move)
        self.state.board = b
        self.state.ply_idx = ply
        return self.current_status()

    def current_status(self) -> Dict[str, Any]:
        b = self.state.board
        return {
            "file": self.state.file_name,
            "ply": self.state.ply_idx,
            "fullmove": b.fullmove_number,
            "turn": "White" if b.turn else "Black",
            "fen": b.fen(),
            "moves_total": len(self.state.nodes_mainline),
            "user_side": self.state.user_side,
        }

    # ===== Branching =====

    def autoplay_to_first_branch(self, min_fullmove: int = 4, max_fullmove: int = 6) -> Dict[str, Any]:
        """
        Advance along mainline until first node having parent with multiple variations appears.
        Prefer a branch within [min_fullmove..max_fullmove]; otherwise take the earliest in the game.
        Keeps board at the branching point (before the main move).
        Returns info with 'path_san': list of SAN moves from start to the branching point.
        """
        if not self.state.game:
            return {"error": "no_game"}

        chosen_idx = None
        fallback_idx = None
        b_scan = self.state.game.board()

        for i, node in enumerate(self.state.nodes_mainline):
            fullmove_before = b_scan.fullmove_number
            if len(node.parent.variations) > 1:
                if fallback_idx is None:
                    fallback_idx = i
                if chosen_idx is None and (min_fullmove <= fullmove_before <= max_fullmove):
                    chosen_idx = i
            b_scan.push(node.move)

        if chosen_idx is None:
            chosen_idx = fallback_idx if fallback_idx is not None else 0

        # Build SAN path from start to chosen_idx
        path_san: list[str] = []
        b_tmp = self.state.game.board()
        for i in range(chosen_idx):
            mv = self.state.nodes_mainline[i].move
            try:
                path_san.append(b_tmp.san(mv))
            except Exception:
                path_san.append("...")
            b_tmp.push(mv)

        # Position board at the branching point
        self.goto_ply(chosen_idx)

        parent = self.state.nodes_mainline[chosen_idx].parent
        vars_all = parent.variations
        main = vars_all[0] if vars_all else None
        alts = vars_all[1:] if len(vars_all) > 1 else []

        alt_moves = []
        for v in alts[:2]:
            try:
                alt_moves.append(v.san())
            except Exception:
                alt_moves.append("(неизвестно)")

        info = {
            "branch_ply": chosen_idx,
            "branch_fullmove": self.state.board.fullmove_number,
            "main_first": main.san() if main else None,
            "alt_first": alt_moves,
            "path_san": path_san,  # <<--- добавили список ходов до развилки
        }
        self.state.last_branch = info
        return info


    # ===== Engine helpers =====

    @staticmethod
    def _score_to_cp(board: chess.Board, score_obj) -> int:
        """Return centipawns relative to side-to-move. Robust for python-chess 1.11.x."""
        try:
            if hasattr(score_obj, "pov"):
                s = score_obj.pov(board.turn)
                m = getattr(s, "mate", None)
                m = m() if callable(m) else m
                if isinstance(m, (int, float)) and m:
                    return 100000 if m > 0 else -100000
                val = getattr(s, "score", None)
                val = val() if callable(val) else val
                if isinstance(val, (int, float)):
                    return int(val)
            m = getattr(score_obj, "mate", None)
            m = m() if callable(m) else m
            if isinstance(m, (int, float)) and m:
                return 100000 if m > 0 else -100000
            cp = getattr(score_obj, "cp", None)
            cp = cp() if callable(cp) else cp
            if isinstance(cp, (int, float)):
                return int(cp)
        except Exception:
            pass
        return 0

    def _format_lines(self, board: chess.Board, results) -> List[Dict[str, Any]]:
        """Convert engine results (InfoDict or list thereof) to compact cp + SAN PV."""
        out: List[Dict[str, Any]] = []
        for i, it in enumerate(results, start=1):
            # pv
            try:
                pv = it.get("pv") or []
            except Exception:
                pv = []
            try:
                pv_san = board.variation_san(pv[:12]) if pv else ""
            except Exception:
                pv_san = ""
            # score → cp
            try:
                sc = it.get("score")
            except Exception:
                sc = None
            cp = self._score_to_cp(board, sc) if sc is not None else 0
            out.append({"idx": i, "cp": cp, "pv_san": pv_san})
        return out

    def quick_eval(self, depth: int = 18, multipv: int = 2) -> Dict[str, Any]:
        res = self.sf.analyse(self.state.board, depth=depth, multipv=multipv)
        results = res if isinstance(res, list) else [res]
        return {"fen": self.state.board.fen(), "lines": self._format_lines(self.state.board, results)}

    def opening_info_current(self, top_n: int = 3) -> dict:
        """
        Query Lichess explorer for current FEN (fast).
        Returns {"eco":str|None, "name_en":str|None, "name":str|None,
                "top":[{"san":str,"played":int},...]}
        """
        try:
            data = fetch_opening_stats(self.state.board.fen(), max_moves=20, top_n=max(1, top_n))
            if not data:
                return {"eco": None, "name_en": None, "name": None, "top": []}

            eco = (data.get("eco") or None)
            name_en = (data.get("opening_name") or None)
            # Prefer Russian by ECO; fallback to English name if unknown
            name_ru = name_from_eco(eco, fallback_en=name_en)
            top = []
            for m in (data.get("moves") or [])[:top_n]:
                san = (m.get("san") or "").strip()
                played = int(m.get("played") or 0)
                if san:
                    top.append({"san": san, "played": played})

            return {"eco": eco, "name_en": name_en, "name": name_ru, "top": top}
        except Exception:
            return {"eco": None, "name_en": None, "name": None, "top": []}

    def first_line_and_equal_alts(self, depth: int = 16, multipv: int = 3) -> dict:
        """
        На текущей позиции оценивает 1–3 линии. Возвращает:
        {
          "best_san": "...",      # первый ход главной линии
          "equal_alts": ["...", "..."]  # альтернативы ~равной силы (<= ALT_TOL_CP)
        }
        """
        res = self.sf.analyse(self.state.board, depth=depth, multipv=multipv)
        items = res if isinstance(res, list) else [res]
        if not items:
            return {"best_san": "", "equal_alts": []}

        # Соберём cp POV белых + SAN первого хода
        lines = []
        b = self.state.board
        for it in items[:multipv]:
            pv = it.get("pv") or []
            try:
                san0 = b.san(pv[0]) if pv else ""
            except Exception:
                san0 = ""
            cp = self._score_to_cp(b, it.get("score")) if it.get("score") is not None else 0
            # Переведём в POV белых (как в game_analyzer) для простоты сравнения
            # NB: _score_to_cp уже даёт POV side-to-move; хватит относительной разницы
            lines.append({"san": san0, "cp": cp})

        if not lines:
            return {"best_san": "", "equal_alts": []}

        best = lines[0]
        eq = []
        for L in lines[1:]:
            if abs(best["cp"] - L["cp"]) <= ALT_TOL_CP:
                if L["san"] and L["san"] != best["san"]:
                    eq.append(L["san"])
        return {"best_san": best["san"], "equal_alts": eq[:2]}

    def detect_mistake_on_next_main_move(self, depth: int = 16) -> dict:
        if not self.state.game:
            return {"played_san": "", "best_san": "", "cpl": 0, "mark": ""}

        idx = self.state.ply_idx
        if idx >= len(self.state.nodes_mainline):
            return {"played_san": "", "best_san": "", "cpl": 0, "mark": ""}

        move_node = self.state.nodes_mainline[idx]
        pre_board = self.state.board

        # --- (A) дебютный фильтр: до 6-го полного хода не ругаем за микропросадки ---
        if pre_board.fullmove_number <= 6:
            try:
                played_san = pre_board.san(move_node.move)
            except Exception:
                played_san = ""
            return {"played_san": played_san, "best_san": "", "cpl": 0, "mark": ""}

        # --- (B) предоценка MultiPV: нужен cp лучшего и cp сыгранного хода ДО выполнения ---
        info_pre = self.sf.analyse(pre_board, depth=depth, multipv=4)
        items_pre = info_pre if isinstance(info_pre, list) else [info_pre]
        best_pre = items_pre[0] if items_pre else None
        best_cp_stm = self._score_to_cp(pre_board, best_pre.get("score")) if (best_pre and best_pre.get("score") is not None) else 0

        try:
            played_san = pre_board.san(move_node.move)
        except Exception:
            played_san = ""

        # найдём элемент MultiPV, где первый ход совпадает с сыгранным
        cp_played_pre = None
        played_is_best = False
        for it in items_pre:
            pv0 = (it.get("pv") or [])
            if pv0:
                try:
                    san0 = pre_board.san(pv0[0])
                except Exception:
                    san0 = ""
                if san0 == played_san:
                    cp_played_pre = self._score_to_cp(pre_board, it.get("score")) if (it.get("score") is not None) else None
                    if it is best_pre:
                        played_is_best = True
                    break

        # если это лучший ход ИЛИ предоценка сыгранного близка к лучшей — не считаем ошибкой
        if played_is_best or (cp_played_pre is not None and abs(best_cp_stm - cp_played_pre) <= ALT_TOL_CP):
            return {"played_san": played_san, "best_san": played_san if played_is_best else (pre_board.san((best_pre.get('pv') or [])[0]) if best_pre and (best_pre.get('pv') or []) else ""), "cpl": 0, "mark": ""}

        # --- (C) “пост” оценка после применения хода (для CPL) ---
        mover = (not pre_board.turn)
        b_after = pre_board.copy(); b_after.push(move_node.move)
        info_post = self.sf.analyse(b_after, depth=depth, multipv=1)
        item_post = info_post if isinstance(info_post, dict) else (info_post[0] if info_post else None)

        post_cp_for_mover = 0
        try:
            b_tmp = b_after.copy()
            b_tmp.turn = mover
            post_cp_for_mover = self._score_to_cp(b_tmp, item_post.get("score")) if (item_post and item_post.get("score") is not None) else 0
        except Exception:
            post_cp_for_mover = 0

        cpl = int(best_cp_stm - post_cp_for_mover)

        best_san = ""
        if best_pre:
            pv0 = best_pre.get("pv") or []
            if pv0:
                try:
                    best_san = pre_board.san(pv0[0])
                except Exception:
                    pass

        mark = ""
        ac = abs(cpl)
        if ac >= THRESH_BLUNDER:
            mark = "??"
        elif ac >= THRESH_MISTAKE:
            mark = "?"
        elif ac >= THRESH_INACCURACY:
            mark = "?!"

        return {"played_san": played_san, "best_san": best_san, "cpl": cpl, "mark": mark}


    def what_if_san(self, san_line: str, depth: int = 23, multipv: int = 2,
                    show_progress: bool = True) -> Dict[str, Any]:
        """
        Apply SAN sequence from current position and run deep analysis (default d=23).
        Progress is printed to stdout at each depth.
        """
        b = self.state.board.copy()
        try:
            moves = apply_san_sequence(b, san_line)   # понимает SAN, UCI, запятые, «...», номера ходов
            for mv in moves:
                b.push(mv)
        except Exception as e:
            return {"error": f"Неверная запись варианта: {e}"}

        last_results = []
        start_depth = 10 if depth >= 10 else depth
        for d in range(start_depth, depth + 1):
            res = self.sf.analyse(b, depth=d, multipv=multipv)
            results = res if isinstance(res, list) else [res]
            # прогресс
            if show_progress:
                try:
                    pv = results[0].get("pv")
                except Exception:
                    pv = None
                try:
                    pv_san = b.variation_san((pv or [])[:8]) if pv else ""
                except Exception:
                    pv_san = ""
                print(f"[ANALYZE d{depth}] depth={d:>2} pv={pv_san}")
            last_results = results

        return {"fen_after": b.fen(), "lines": self._format_lines(b, last_results)}
    # ===== Conversational short answer =====

    def coach_reply(self, user_text: str, depth: int = 18) -> str:
        st = self.current_status()
        qe = self.quick_eval(depth=depth, multipv=2)
        lines = qe.get("lines", [])
        pv1 = lines[0]["pv_san"] if lines else ""
        eval_cp = lines[0]["cp"] if lines else 0
        eval_pawns = (eval_cp / 100.0) if abs(eval_cp) < 9000 else (99.9 if eval_cp > 0 else -99.9)
        prompt = (
            "Ты русский шахматный тренер-гроссмейстер. Отвечай кратко (до 5 пунктов).\n"
            f"Позиция (FEN): {st['fen']}\n"
            f"Сторона на ходу: {'Белые' if 'White' in st['turn'] else 'Чёрные'}.\n"
            f"Оценка движка: {eval_pawns:+.2f}. Главная линия: {pv1 or '(нет PV)'}.\n"
            "Запрос ученика:\n"
            f"«{user_text}»\n\n"
            "Дай конкретные рекомендации: 1) кандидаты и план, 2) идеи соперника, 3) чего избегать, 4) ближайшая тактика."
        )
        try:
            return ask(prompt)
        except Exception:
            plan = plan_for_fen(st["fen"])
            return plan.get("advice", "Сыграй по плану: усили слабые поля, активизируй фигуры, следи за ресурсами соперника.")
        
    def opening_name_once(self) -> str:
        """
        Return opening name only once per session.
        On the first call, stores ECO and marks as announced.
        Later calls return "" (silence), even if ECO changes deeper in theory.
        """
        try:
            info = self.opening_info_current(top_n=0)
        except Exception:
            return ""
        eco = info.get("eco")
        name = info.get("name") or info.get("name_en") or ""
        if (not name) and self.state.game:
            h = self.state.game.headers
            eco_hdr = (h.get("ECO") or "").strip() or None
            name_hdr_en = (h.get("Opening") or "").strip() or None
            name = name_from_eco(eco_hdr, fallback_en=name_hdr_en) or ""
            eco = eco or eco_hdr
        if not name and not eco:
            return ""
        if self.state.opening_announced:
            return ""
        # Remember and announce once
        self.state.opening_announced = True
        self.state.last_opening_eco = eco
        return name or ""

        # ===== Speech helpers (RU) =====

    @staticmethod
    def _file_ru(c: str) -> str:
        """Map file letter to spoken Russian."""
        m = {"a":"а","b":"бэ","c":"це","d":"дэ","e":"е","f":"эф","g":"же","h":"аш"}
        return m.get(c.lower(), c)

    @staticmethod
    def _rank_ru(d: str) -> str:
        """Map rank digit to spoken Russian."""
        m = {"1":"один","2":"два","3":"три","4":"четыре","5":"пять","6":"шесть","7":"семь","8":"восемь"}
        return m.get(d, d)

    @classmethod
    def _square_ru(cls, sq: str) -> str:
        """e4 -> 'е четыре'."""
        if len(sq) != 2:
            return sq
        return f"{cls._file_ru(sq[0])} {cls._rank_ru(sq[1])}"

    @staticmethod
    def _piece_ru(letter: str) -> str:
        """N,B,R,Q,K -> конь, слон, ладья, ферзь, король."""
        m = {"N":"конь", "B":"слон", "R":"ладья", "Q":"ферзь", "K":"король"}
        return m.get(letter, "")

    @classmethod
    def san_to_speech(cls, san: str) -> str:
        """
        Convert SAN like 'Nf3', 'Bxd5', 'O-O', 'exd5', 'Qe8=Q+', 'R1e2', 'axb5 e.p.' to spoken RU.
        Rules:
        - Pieces by full name; pawn is silent unless capture ('пешка бьёт ...' -> we just say 'бьёт ...').
        - Capture 'x' -> 'бьёт'.
        - Check '+' -> 'шах'; mate '#' -> 'мат'.
        - O-O / O-O-O -> 'рокировка в короткую/длинную'.
        - Promotion '=Q' -> '… ферзём/ладьёй/слоном/конём'.
        - Disambiguation like 'Nbd2' / 'R1e2' -> add source file/rank: 'конь с бэ на дэ два'.
        """
        s = san.strip()

        # Castling
        if s.startswith("O-O-O"):
            tail = " мат" if san.endswith("#") else (" шах" if san.endswith("+") else "")
            return "длинная рокировка" + tail
        if s.startswith("O-O"):
            tail = " мат" if san.endswith("#") else (" шах" if san.endswith("+") else "")
            return "короткая рокировка" + tail

        # Strip check/mate signs for parsing
        tail = ""
        if s.endswith("#"):
            tail = " мат"
            s = s[:-1]
        elif s.endswith("+"):
            tail = " шах"
            s = s[:-1]

        # Promotion
        promo_piece = None
        if "=" in s:
            base, promo = s.split("=", 1)
            s = base
            promo_map = {"Q":"ферзём", "R":"ладьёй", "B":"слоном", "N":"конём"}
            promo_piece = promo_map.get(promo[0].upper(), None)

        capture = "x" in s
        s = s.replace("e.p.", "").strip()

        # Disambiguation (Nbd2 / R1e2)
        disamb_src = ""
        piece = ""
        to_sq = ""
        if s and s[0].isupper() and s[0] in "NBRQK":
            piece = cls._piece_ru(s[0])
            body = s[1:]
        else:
            body = s  # pawn move

        # Split by capture or plain move
        parts = body.split("x") if "x" in body else body.split("x")  # keep same
        # Determine destination square (last two chars of the string)
        # Examples:
        #   'f3'           -> pawn
        #   'bd2'          -> disamb 'b' + 'd2'
        #   '1e2'          -> disamb '1' + 'e2'
        #   'bxd5' (already stripped piece) -> parts=['b','d5']; we will parse from original s
        core = body.replace("x", "")
        if len(core) >= 2:
            to_sq = core[-2:]

        # Disambiguation source (file or rank), if present
        mid = core[:-2]
        if mid:
            # one char usually
            ch = mid[-1]
            if ch.isalpha():
                disamb_src = f" с {cls._file_ru(ch)}"
            elif ch.isdigit():
                disamb_src = f" с {cls._rank_ru(ch)}"

        dest = cls._square_ru(to_sq) if to_sq else ""

        # Build speech
        if piece:  # piece move
            if capture:
                text = f"{piece}{disamb_src} бьёт {dest}"
            else:
                text = f"{piece}{disamb_src} {dest}"
        else:
            # pawn move
            if capture:
                # 'exd5' -> 'бьёт д пять'
                text = f"бьёт {dest}"
            else:
                text = dest  # just 'е четыре'

        if promo_piece:
            text += f" {promo_piece}"

        return (text + tail).strip()

    @classmethod
    def san_list_to_speech(cls, sans: List[str]) -> str:
        """Join SAN list to a single spoken phrase."""
        spoken = [cls.san_to_speech(s) for s in sans if s]
        return ", ".join(spoken) + ( "." if spoken else "" )
    
    @classmethod
    def pv_san_to_speech(cls, pv_san: str, max_san: int = 6) -> str:
        """
        Convert a full PV string like '1. d4 Nf6 2. c4 e6 3. Nc3' into spoken RU.
        - Strips move numbers like '1.' or '1...'
        - Keeps only SAN tokens and speaks first `max_san` tokens (6 ≈ 3 full moves).
        """
        if not pv_san:
            return ""
        # Normalize whitespace
        s = " ".join(pv_san.replace("\n", " ").split())

        tokens = []
        for tok in s.split():
            # skip '1.' or '1...' etc
            if re.fullmatch(r"\d+\.(\.\.)?", tok):
                continue
            tokens.append(tok)

        # keep only first N SAN tokens
        tokens = tokens[:max_san]

        # to speech
        spoken = [cls.san_to_speech(t) for t in tokens if t]
        return ", ".join(spoken) + ( "." if spoken else "" )

    def close(self):
        self.sf.quit()
