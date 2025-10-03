# build_base.py — импорт всех .pgn из data/pgn/** в chess_kb.sqlite3 с обходом ВСЕХ вариаций
from helpers import material_signature
import sqlite3, json, os, sys
from pathlib import Path
import chess, chess.pgn

ROOT = Path(__file__).resolve().parent
PGN_ROOT = ROOT / "data" / "pgn"
DB_PATH = ROOT / "chess_kb.sqlite3"

def ensure_schema(con: sqlite3.Connection):
    cur = con.cursor()
    # games
    cur.execute("""
    CREATE TABLE IF NOT EXISTS games(
      game_id INTEGER PRIMARY KEY,
      event TEXT, site TEXT, date TEXT,
      white TEXT, black TEXT, result TEXT,
      white_elo INT, black_elo INT,
      eco TEXT, opening TEXT,
      source_tags TEXT,
      pgn TEXT,
      moves_san TEXT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_games_eco ON games(eco);")

    # positions (расширенная схема)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS positions(
      pos_id INTEGER PRIMARY KEY,
      game_id INT,
      ply INT,
      fen TEXT,
      phase TEXT,
      material_signature TEXT,
      comment TEXT,
      engine_eval INT,
      move_san TEXT,
      move_uci TEXT,
      nags TEXT,
      is_mainline INT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_fen ON positions(fen);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_phase ON positions(phase);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_msig ON positions(material_signature);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_main ON positions(is_mainline);")

    # миграция: добьём недостающие колонки (если БД старая)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(positions);")}
    def addcol(name, decl):
        if name not in cols:
            cur.execute(f"ALTER TABLE positions ADD COLUMN {name} {decl};")
    addcol("move_san", "TEXT")
    addcol("move_uci", "TEXT")
    addcol("nags", "TEXT")
    addcol("is_mainline", "INT")
    con.commit()

def guess_phase(board: chess.Board, ply: int, eco: str|None) -> str:
    if eco or ply <= 16: return "opening"
    minor = sum(len(board.pieces(p, chess.WHITE))+len(board.pieces(p, chess.BLACK)) for p in [chess.BISHOP, chess.KNIGHT])
    majors = sum(len(board.pieces(p, chess.WHITE))+len(board.pieces(p, chess.BLACK)) for p in [chess.QUEEN, chess.ROOK])
    pawns = len(board.pieces(chess.PAWN, chess.WHITE))+len(board.pieces(chess.PAWN, chess.BLACK))
    if majors <= 2 and (minor <= 2 or pawns <= 6):
        return "endgame"
    return "middlegame"

def insert_position(cur, game_id: int, ply: int, board: chess.Board, eco: str|None,
                    comment: str|None, move_san: str|None, move_uci: str|None,
                    nags_list: list[int]|None, is_mainline: int):
    fen = board.fen()
    phase = guess_phase(board, ply, eco)
    msig = material_signature(board) if phase == "endgame" else None
    cur.execute("""
      INSERT INTO positions (game_id, ply, fen, phase, material_signature, comment, engine_eval,
                             move_san, move_uci, nags, is_mainline)
      VALUES (?,?,?,?,?,?,NULL,?,?,?,?)
    """, (game_id, ply, fen, phase, msig, comment or None,
          move_san, move_uci, json.dumps(nags_list or []), is_mainline))

def traverse_all(node: chess.pgn.ChildNode | chess.pgn.GameNode,
                 board: chess.Board, cur: sqlite3.Cursor,
                 game_id: int, ply: int, eco: str|None, is_mainline: int):
    """
    Рекурсивный обход ВСЕХ вариаций.
    Вставляем позицию ПОСЛЕ выполнения хода текущего узла (node).
    """
    # node здесь — уже "после" хода родителя. У корня GameNode нет хода.
    for i, child in enumerate(node.variations):
        # SAN/uci считаем относительно доски до хода:
        pre_board = board.copy()
        san = child.san()  # python-chess выдаёт SAN для child.move на pre_board
        uci = child.move.uci()

        # применяем ход → вставляем позицию после хода
        board.push(child.move)
        ply_next = ply + 1
        insert_position(
            cur, game_id, ply_next, board, eco,
            getattr(child, "comment", None),
            san, uci,
            sorted(list(getattr(child, "nags", set()))) if hasattr(child, "nags") else [],
            1 if (is_mainline and i == 0) else 0
        )
        # рекурсивно обходим продолжения от child
        traverse_all(child, board, cur, game_id, ply_next, eco, 1 if (is_mainline and i == 0) else 0)
        board.pop()

def import_pgn(pgn_path: Path, source_tag: str):
    con = sqlite3.connect(DB_PATH)
    ensure_schema(con)
    cur = con.cursor()
    added_games = added_positions = 0

    with open(pgn_path, 'r', encoding='utf-8', errors='ignore') as f:
        while True:
            game = chess.pgn.read_game(f)
            game_id: int | None = None
            if game is None: break
            headers = game.headers
            eco = headers.get("ECO"); opening = headers.get("Opening")

            # соберём SAN мейнлайна ради колонки games.moves_san (не обязательно, но полезно)
            moves_san = []
            tmp = game
            while tmp.variations:
                tmp = tmp.variations[0]
                moves_san.append(tmp.san())

            cur.execute("""INSERT INTO games
              (event,site,date,white,black,result,white_elo,black_elo,eco,opening,source_tags,pgn,moves_san)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (headers.get("Event"), headers.get("Site"), headers.get("Date"),
               headers.get("White"), headers.get("Black"), headers.get("Result"),
               int(headers.get("WhiteElo") or 0), int(headers.get("BlackElo") or 0),
               eco, opening, json.dumps([source_tag]), str(game), json.dumps(moves_san)))
            game_id = cur.lastrowid
            added_games += 1
            assert game_id is not None

            # корневая позиция (ply=0)
            board = game.board()  # учитывает SetUp/FEN при наличии
            insert_position(cur, game_id, 0, board, eco, getattr(game, "comment", None),
                            move_san=None, move_uci=None, nags_list=[], is_mainline=1)
            added_positions += 1

            # обходим все вариации от корня
            traverse_all(game, board, cur, game_id, 0, eco, is_mainline=1)

    con.commit(); con.close()
    # оценим число позиций приближённо (точный счёт уже в БД)
    return added_games, added_positions

def main():
    if not PGN_ROOT.exists():
        print(f"[ERR] Не найдена папка {PGN_ROOT}")
        sys.exit(1)

    # Если хотите «чистый» импорт — удалите старую БД перед запуском
    # (иначе будут накапливаться дубликаты).
    # Сейчас просто импортируем поверх (для теста можно вручную удалить файл).

    total_g = total_p = 0
    found = list(PGN_ROOT.rglob("*.pgn"))
    if not found:
        print(f"[WARN] В {PGN_ROOT} нет .pgn файлов.")
    for p in found:
        g, _ = import_pgn(p, source_tag=p.parent.name)
        total_g += g
        # точное число позиций можно спросить по факту:
        # но оставим суммирование по играм, позиции уже в таблице

        print(f"[OK] {p.name}: игр добавлено={g}")
    # финальный отчёт из БД
    con = sqlite3.connect(DB_PATH)
    G = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    P = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    con.close()
    print(f"\nГотово. БД: {DB_PATH}\nИгр всего: {G}\nПозицій всего: {P}")

if __name__ == "__main__":
    main()
