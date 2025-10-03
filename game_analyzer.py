# game_analyzer.py
# Анализ PGN с MultiPV (1–3), короткими PV (по умолчанию 5 ходов),
# CPL-знаками и экспортом аннотированного PGN для ChessBase/Lichess.
# Совместимо с python-chess 1.999.
from engine_core import ENGINE_PATH
from collections.abc import Mapping
import sys
import asyncio
import json
import argparse
from pathlib import Path
import shutil
from typing import Optional, Callable
import chess
import chess.pgn
import chess.engine
from paths import PGN_DIR, ensure_dirs
ensure_dirs()

# На Windows снижает риск подвисаний UCI
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# === Конфиг ===
STOCKFISH_PATH = str(ENGINE_PATH)
DEFAULT_DEPTH = 18
DEFAULT_MULTIPV = 3
DEFAULT_PV_MOVES = 5  # СКОЛЬКО ХОДОВ показывать в каждой PV (по умолчанию 5 полных ходов)

# Скрывать варианты/оценку в дебюте
OPENING_SKIP_FULLMOVES = 5  # первые 5 полных ходов

# Не печатать «плоские» оценки (в пределах ±0.30)
EVAL_HIDE_ABS = 0.30

DECISIVE_HIDE_ABS = 10.0
DECISIVE_HIDE_CP = int(DECISIVE_HIDE_ABS * 100)

# "Не очень" ход: падение оценки на 0.50 пешки и больше
BAD_DROP_CP_FOR_VARIATION = 50

def cp_to_eval(cp: int, places: int = 2) -> str:
    """Печать оценки в виде ±0.24 из центропешек (всегда POV белых)."""
    return f"{cp / 100.0:+.{places}f}"

# Компактный стиль PGN-комментариев
ALT_TOL_CP = 25      # равносильные альтернативы (в пределах 25cp от лучшего)
UNIQUE_GAP_CP = 60   # разрыв, чтобы считать лучший «единственным»
BAD_SHOW = 2         # сколько линий показать при ошибке
SUGGEST_LEN = 3      # длина подсказки в полных ходах для PGN (кратко)

# Пороги для аннотаций (в центропешках)
THRESH_INACCURACY = 50   # -> ?!
THRESH_MISTAKE    = 150  # -> ?
THRESH_BLUNDER    = 300  # -> ??

BIG_MATE_CP = 100000

# Для NAG-меток (PGN)
try:
    from chess.pgn import NAG_MISTAKE, NAG_BLUNDER, NAG_DUBIOUS_MOVE
except Exception:
    NAG_MISTAKE = 2
    NAG_BLUNDER = 4
    NAG_DUBIOUS_MOVE = 6

# === Утилиты ===
def normalize_info_to_list(info):
    if isinstance(info, list):
        return sorted(info, key=lambda x: x.get("multipv", 1))
    return [info]

def annotate_by_cpl(cpl: int) -> str:
    c = abs(int(cpl))
    if c >= THRESH_BLUNDER:
        return "??"
    if c >= THRESH_MISTAKE:
        return "?"
    if c >= THRESH_INACCURACY:
        return "?!"
    return ""

def mark_to_nag(mark: str):
    if mark == "??":
        return NAG_BLUNDER
    if mark == "?":
        return NAG_MISTAKE
    if mark == "?!":
        return NAG_DUBIOUS_MOVE
    return None

def _to_int(val):
    if isinstance(val, (int, float)):
        return int(val)
    for name in ("cp", "value"):
        x = getattr(val, name, None)
        if x is not None:
            try:
                return int(x)
            except Exception:
                pass
    try:
        return int(val)
    except Exception:
        return None

def info_get_pv(item):
    """Безопасно достаём список ходов PV из info-элемента."""
    try:
        if isinstance(item, Mapping):
            return item.get("pv") or []
        pv = getattr(item, "pv", None)
        return pv or []
    except Exception:
        return []

def extract_cp(score_obj, pov_color: chess.Color) -> int:
    if score_obj is None:   # <— ДОБАВЬ ЭТО
        return 0
    s = score_obj
    try:
        if hasattr(s, "pov"):
            s = s.pov(pov_color)
    except Exception:
        pass
    try:
        sc = getattr(s, "score", None)
        if callable(sc):
            try:
                raw = sc(mate_score=BIG_MATE_CP)  # новые версии
            except TypeError:
                raw = sc()                        # старые версии
            iv = _to_int(raw)
            if iv is not None:
                return iv
    except Exception:
        pass
    iv = _to_int(getattr(s, "cp", None))
    if iv is not None:
        return iv
    try:
        if hasattr(s, "is_mate") and s.is_mate() and hasattr(s, "mate"):
            m = s.mate()
            if isinstance(m, (int, float)) and m != 0:
                return BIG_MATE_CP if m > 0 else -BIG_MATE_CP
    except Exception:
        pass
    return 0

def pv_to_san(board: chess.Board, pv_moves, max_full_moves: int | None = None) -> str:
    """Перевод PV в SAN, обрезая до max_full_moves (полных ходов: белые+чёрные)."""
    if pv_moves is None:
        return ""
    cap = len(pv_moves)
    if max_full_moves is not None:
        cap = min(cap, max_full_moves * 2)  # 1 полный ход = 2 полухода
    san_list = []
    b = board.copy()
    for mv in pv_moves[:cap]:
        san_list.append(b.san(mv))
        b.push(mv)
    return " ".join(san_list)

# === Анализ ===
def analyze_position(engine, board: chess.Board, depth: int, multipv: int, pv_moves_limit: int):
    """Список вариантов [{idx, cp, pv_san}] для позиции ДО хода (POV = side-to-move)."""
    turn = board.turn
    info = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
    items = normalize_info_to_list(info)
    lines = []
    for i, item in enumerate(items, start=1):
        raw = item["score"]
        cp = extract_cp(raw, turn)
        pv_moves = info_get_pv(item)
        lines.append({
            "idx": i,
            "cp": cp,
            "pv_san": pv_to_san(board, pv_moves, max_full_moves=pv_moves_limit)
        })
    return lines

def info_get_score(item):
    """Безопасно достаём объект оценки из info-словаря/объекта."""
    try:
        # обычный случай: dict c ключом "score"
        if isinstance(item, dict):
            return item.get("score")
        # на всякий: объект с атрибутом score
        return getattr(item, "score", None)
    except Exception:
        return None

def add_pv_variation(base_node: chess.pgn.GameNode,
                     base_board: chess.Board,
                     pv_moves,
                     max_full_moves: int,
                     cp_eval: int | None,
                     place_eval: str = "tail"):
    """
    Добавляет побочный вариант к узлу base_node по списку ходов pv_moves,
    обрезая до max_full_moves (полных ходов).
    place_eval: "head" — ставить оценку на первом ходе варианта,
                "tail" — на последнем ходе варианта.
    """
    if not pv_moves:
        return
    cap = min(len(pv_moves), max_full_moves * 2)
    if cap <= 0:
        return

    # первый ход варианта
    var_node = base_node.add_variation(pv_moves[0])
    last_node = var_node

    # остальные ходы короткой ветки
    for mv in pv_moves[1:cap]:
        last_node = last_node.add_variation(mv)

    # ставим оценку там, где нужно (в формате ±0.XX), POV = белые
    if cp_eval is not None:
        text = cp_to_eval(cp_eval)
        if place_eval == "head":
            var_node.comment = text
        else:
            last_node.comment = text



def analyze_game(pgn_path: Path, depth=DEFAULT_DEPTH, multipv=DEFAULT_MULTIPV,
                 pv_moves_limit=DEFAULT_PV_MOVES, out_dir: Path = Path("analysis_out"),
                 progress_cb: Optional[Callable[[int, int], None]] = None):
    if not pgn_path.exists():
        raise FileNotFoundError(f"PGN не найден: {pgn_path}")

    if shutil.which(STOCKFISH_PATH) is None and not Path(STOCKFISH_PATH).exists():
        raise FileNotFoundError(
            "Stockfish не найден. Укажи корректный STOCKFISH_PATH или добавь бинарь в PATH."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    base = pgn_path.stem.replace(" ", "_")
    jsonl_path = out_dir / f"{base}.jsonl"
    md_path    = out_dir / f"{base}.md"
    pgn_out    = out_dir / f"{base}_annotated.pgn"

    with open(pgn_path, "r", encoding="utf-8", errors="ignore") as f:
        game = chess.pgn.read_game(f)
    if game is None:
        raise ValueError(f"В файле {pgn_path} не найдено ни одной партии.")
    
    total_plies = sum(1 for _ in game.mainline())

    board = game.board()
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    engine.configure({"Threads": 2, "Hash": 256})  # MultiPV НЕ настраиваем здесь

    # Markdown шапка
    md_lines = []
    md_lines.append(f"# Анализ партии: {game.headers.get('White','?')} — {game.headers.get('Black','?')}")
    md_lines.append(f"Event: {game.headers.get('Event','?')}, Date: {game.headers.get('Date','?')}")
    md_lines.append("")
    md_lines.append(f"Depth: {depth}, MultiPV: {multipv}, PV length: {pv_moves_limit} ходов")
    md_lines.append("")

    # Аннотированная партия для PGN
    annotated = chess.pgn.Game()
    for k, v in game.headers.items():
        if v:
            annotated.headers[k] = v
    ann_node = annotated

    key_recs = []
    ply = 0

    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for node in game.mainline():
            # --- позиция ДО хода ---
            pre_board = board.copy()
            turn = pre_board.turn
            pre_node = ann_node  # узел PGN до хода

            # Сырые варианты
            info = engine.analyse(pre_board, chess.engine.Limit(depth=depth), multipv=multipv)
            items = normalize_info_to_list(info)

            # Красивые строки для MD (CP в POV белых)
            lines = []
            for i, item in enumerate(items, start=1):
                cp_white = extract_cp(info_get_score(item), chess.WHITE)
                pv_moves = info_get_pv(item)
                lines.append({
                    "idx": i,
                    "cp": cp_white,
                    "pv_san": pv_to_san(pre_board, pv_moves, max_full_moves=pv_moves_limit)
                })
            

            # Лучшая оценка: для CPL берём POV стороны на ходу; для вывода — POV белых
            best_cp_stm   = extract_cp(info_get_score(items[0]), turn)       if items else 0
            best_cp_white = extract_cp(info_get_score(items[0]), chess.WHITE) if items else 0
            decisive_now = (abs(best_cp_white) >= DECISIVE_HIDE_CP)

            # --- сыгранный ход партии ---
            move = node.move
            move_san = pre_board.san(move)

            # Главную линию ведём как основную вариацию
            ann_node = pre_node.add_variation(move)

            # Совпадает ли сыгранный ход с одной из PV?
            played_idx = None
            for i, it in enumerate(items, start=1):
                pv0 = info_get_pv(it)
                if pv0 and pv0[0] == move:
                    played_idx = i
                    break

            # --- заранее подберём индексы альтернатив, но вставлять будем ПОСЛЕ расчёта CPL ---
            alt_idxs = []
            if (pre_board.fullmove_number > OPENING_SKIP_FULLMOVES) and (not decisive_now):
                if played_idx is not None:
                    # если ход не "единственный лучший", предложим равносильные альтернативы к лучшему
                    gap = (lines[0]["cp"] - lines[1]["cp"]) if len(lines) >= 2 else (UNIQUE_GAP_CP + 1)
                    if not (played_idx == 1 and gap >= UNIQUE_GAP_CP):
                        best_cp_local = lines[0]["cp"]
                        for i, L in enumerate(lines, start=1):
                            if i == played_idx:
                                continue
                            if abs(best_cp_local - L["cp"]) <= ALT_TOL_CP:
                                alt_idxs.append(i)
                            if len(alt_idxs) >= 2:
                                break
                else:
                    # ход не входит в топ — 1–2 лучшие короткие линии
                    for i in range(1, min(BAD_SHOW, len(lines)) + 1):
                        pv0 = info_get_pv(items[i-1])
                        if pv0 and pv0[0] == move:
                            continue
                        alt_idxs.append(i)

            # --- применяем ход и считаем post ---
            board.push(move)
            ply += 1
            if progress_cb:
                try:
                    progress_cb(ply, total_plies)
                except Exception:
                    pass

            # Кто только что сходил
            mover = chess.BLACK if board.turn == chess.WHITE else chess.WHITE

            post_info  = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=1)
            post_items = normalize_info_to_list(post_info)
            post_score = info_get_score(post_items[0])

            # Для CPL/знаков — POV сыгравшего, для вывода — POV белых
            post_cp_mover = extract_cp(post_score, mover)
            post_cp_white = extract_cp(post_score, chess.WHITE)
            decisive_now = decisive_now or (abs(post_cp_white) >= DECISIVE_HIDE_CP)

            cpl  = best_cp_stm - post_cp_mover
            mark = annotate_by_cpl(cpl)

            # --- теперь вставляем побочные ветки в PGN и решаем, где печатать оценку ---
            place_eval = "head" if cpl >= BAD_DROP_CP_FOR_VARIATION else "tail"

            if (pre_board.fullmove_number > OPENING_SKIP_FULLMOVES) and (not decisive_now):
                for i in alt_idxs:
                    pv = info_get_pv(items[i-1])
                    cp_i_white = extract_cp(info_get_score(items[i-1]), chess.WHITE)
                    if pv and pv[0] != move:  # не дублируем основной ход
                        add_pv_variation(pre_node, pre_board, pv, SUGGEST_LEN, cp_i_white, place_eval=place_eval)

            # NAG на основном ходе
            nag = mark_to_nag(mark)
            if (not decisive_now) and nag:
                ann_node.nags.add(nag)

            # JSONL запись
            rec = {
                "ply": ply,
                "fen_before": pre_board.fen(),
                "move_san": move_san,
                "lines": lines,                       # cp тут уже POV белых
                "best_cp_before_stm": best_cp_stm,    # для CPL/теханализа
                "best_cp_before_white": best_cp_white,# для вывода
                "post_cp_for_mover": post_cp_mover,   # для CPL/знаков
                "post_cp_white": post_cp_white,       # для вывода
                "cpl": cpl,
                "mark": mark,
            }
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")

            # --- Markdown вывод ---
            num = f"{(ply+1)//2}." if board.turn == chess.BLACK else f"{(ply)//2}..."

            eval_section = ""
            if (pre_board.fullmove_number > OPENING_SKIP_FULLMOVES) and (not decisive_now):
                hide_best = abs(best_cp_white) <= int(EVAL_HIDE_ABS * 100)
                hide_post = abs(post_cp_white) <= int(EVAL_HIDE_ABS * 100)
                if not (hide_best and hide_post):
                    eval_section = f" (CPL: {cpl:+}, best: {cp_to_eval(best_cp_white)}, post: {cp_to_eval(post_cp_white)})"

            md_lines.append(f"**{num} {move_san}{mark if not decisive_now else ''}**{eval_section}")

            if (pre_board.fullmove_number > OPENING_SKIP_FULLMOVES) and (not decisive_now):
                if cpl >= BAD_DROP_CP_FOR_VARIATION:
                    for L in lines:
                        md_lines.append(f"  • {L['idx']}) {cp_to_eval(L['cp'])} — {L['pv_san']}")
                else:
                    for L in lines:
                        md_lines.append(f"  • {L['idx']}) {L['pv_san']} — {cp_to_eval(L['cp'])}")
            md_lines.append("")
            # Критические моменты для сводки
            if (not decisive_now) and mark:
                key_recs.append({
                    "ply": ply,
                    "move_san": move_san,
                    "mark": mark,
                    "cpl": cpl,
                    "pv1": (lines[0]["pv_san"] if lines else ""),
                    "cp1": (lines[0]["cp"] if lines else 0),
                })


    engine.quit()

    # Сводка «Критические моменты»
    md_lines.append("## Критические моменты")
    if not key_recs:
        md_lines.append("_Нет ходов с ?!/?/??._")
    else:
        key_recs.sort(key=lambda r: abs(r["cpl"]), reverse=True)
        for r in key_recs:
            md_lines.append(
                f"- ply {r['ply']}: **{r['move_san']}{r['mark']}** "
                f"(CPL {r['cpl']:+}) — 1) {cp_to_eval(r['cp1'])} — {r['pv1']}"
            )
    md_lines.append("")

    # Сохраняем Markdown
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    # Сохраняем аннотированный PGN
    with open(pgn_out, "w", encoding="utf-8") as f:
        exporter = chess.pgn.FileExporter(f)
        annotated.accept(exporter)

    return {"jsonl": str(jsonl_path), "md": str(md_path), "pgn": str(pgn_out)}



# === "А что если..." ===
def what_if(jsonl_path: Path, ply: int, san_line: str, depth=DEFAULT_DEPTH,
            multipv=DEFAULT_MULTIPV, pv_moves_limit=DEFAULT_PV_MOVES):
    if not jsonl_path.exists():
        raise FileNotFoundError(f"Не найден JSONL: {jsonl_path}")

    fen_before = None
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("ply") == ply:
                fen_before = rec["fen_before"]
                break
    if not fen_before:
        raise ValueError(f"Не нашёл ply={ply} в {jsonl_path}")

    board = chess.Board(fen_before)
    for token in san_line.split():
        move = board.parse_san(token)
        board.push(move)

    if shutil.which(STOCKFISH_PATH) is None and not Path(STOCKFISH_PATH).exists():
        raise FileNotFoundError("Stockfish не найден для режима 'what if'.")

    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    engine.configure({"Threads": 2, "Hash": 256})
    lines = analyze_position(engine, board, depth, multipv, pv_moves_limit)
    engine.quit()
    return lines

# === CLI ===
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Анализ PGN: MultiPV, короткие PV, знаки, экспорт PGN и Q&A")
    ap.add_argument("--pgn", type=str, help="Путь к PGN")
    ap.add_argument("--analyze", action="store_true", help="Проанализировать партию")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--multipv", type=int, default=DEFAULT_MULTIPV)
    ap.add_argument("--pv-moves", type=int, default=DEFAULT_PV_MOVES, help="Длина PV в полных ходах")
    ap.add_argument("--out", type=str, default="analysis_out")

    ap.add_argument("--whatif", action="store_true", help="Режим 'а что если…'")
    ap.add_argument("--jsonl", type=str, help="JSONL из анализа")
    ap.add_argument("--ply", type=int, help="ply (полуход) ДО которого применяем SAN")
    ap.add_argument("--san", type=str, help="SAN-строка, например 'Rb1' или 'Rb1 h5'")

    args = ap.parse_args()

    if args.analyze:
        if not args.pgn:
            ap.error("--pgn обязателен для --analyze")
        paths = analyze_game(
            Path(args.pgn),
            depth=args.depth,
            multipv=args.multipv,
            pv_moves_limit=args.pv_moves,
            out_dir=Path(args.out),
        )
        print(json.dumps(paths, ensure_ascii=False, indent=2))

    elif args.whatif:
        if not (args.jsonl and args.ply and args.san):
            ap.error("--whatif требует --jsonl, --ply и --san")
        lines = what_if(
            Path(args.jsonl),
            args.ply,
            args.san,
            depth=args.depth,
            multipv=args.multipv,
            pv_moves_limit=args.pv_moves,
        )
        for L in lines:
            print(f"{L['idx']}) {L['cp']:+} cp — {L['pv_san']}")
