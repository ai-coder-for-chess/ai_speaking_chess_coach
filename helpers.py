import re
import chess
from llm_util import ask
# Регулярка для полного FEN (8 рангов + метаполя)
FEN_REGEX = re.compile(
    r'([rnbqkpRNBQKP1-8]+(?:/[rnbqkpRNBQKP1-8]+){7}\s+[wb]\s+(?:K?Q?k?q?|-)' \
    r"\s+(?:[a-h][36]|-)\s+\d+\s+\d+)"
)

# Маппинг названий фигур на символы FEN
PIECE_MAP = {
    "король": "k", "ферзь":    "q", "ладья": "r",
    "слон":   "b", "офицер":   "b", "конь":  "n",
    "пешка":  "p"
}

def is_valid_board_part(board_part: str) -> bool:
    """
    Проверяет, что в board_part ровно 8 рангов и каждая строка суммируется в 8 клеток.
    """
    rows = board_part.split("/")
    if len(rows) != 8:
        return False
    for row in rows:
        total = 0
        for ch in row:
            total += int(ch) if ch.isdigit() else 1
        if total != 8:
            return False
    return True


def generate_fen_from_description(description: str) -> str:
    """
    Спрашивает у LLM FEN по описанию, извлекает полную FEN-строку через regex,
    проверяет её и при необходимости уточняет до 3 попыток.
    """
    prompt = (
        f"Опиши позицию «{description}» в формате FEN: "
        "8 рангов разделённых '/', затем через пробел ход (w или b), "
        "права на рокировку (KQkq или -), en passant (e3 или -), "
        "полупроход и номер хода. Ответ — только FEN-строка."
    )
    last_resp = ""
    for _ in range(3):
        resp = ask(prompt).strip()
        last_resp = resp
        m = FEN_REGEX.search(resp)
        if m:
            return m.group(1)
        prompt = (
            f"Твой предыдущий ответ («{resp}») не является корректным FEN. "
            "Пожалуйста, ответь только FEN-строкой без лишних слов."
        )
    # fallback: возьмём первую часть до пробела и проверим board-part
    parts = last_resp.strip().split(None, 1)
    candidate = parts[0] if parts else last_resp
    if is_valid_board_part(candidate):
        if len(parts) == 1:
            candidate = candidate + " w - - 0 1"
        return candidate
    raise ValueError(f"Не удалось получить корректный FEN после 3 попыток. Последний ответ:\n{last_resp}")


def parse_placement(line: str) -> tuple[str,str]:
    """
    Парсит строку вида "поставь белую ладью на a1" → ("R","a1").
    """
    line = line.lower()
    m = re.search(r"\b([a-h][1-8])\b", line)
    if not m:
        raise ValueError("Не нашёл клетку в команде")
    square = m.group(1)
    color = "white" if "бел" in line else "black"
    for name, ch in PIECE_MAP.items():
        if name in line:
            piece = ch.upper() if color=="white" else ch.lower()
            return piece, square
    raise ValueError("Не нашёл название фигуры в команде")


def placements_to_fen(placements: dict[str,str], turn: str="w") -> str:
    """
    Из словаря {'a1':'R','e4':'p',...} собирает полный FEN:
    8 рангов, метаполя default (turn, '-', '-', 0, 1).
    """
    board_arr = [["1"]*8 for _ in range(8)]
    for sq, piece in placements.items():
        file = ord(sq[0]) - ord('a')
        rank = 8 - int(sq[1])
        board_arr[rank][file] = piece
    ranks = []
    for row in board_arr:
        fen_row = ""
        empty = 0
        for cell in row:
            if cell == "1":
                empty += 1
            else:
                if empty:
                    fen_row += str(empty)
                    empty = 0
                fen_row += cell
        if empty:
            fen_row += str(empty)
        ranks.append(fen_row)
    board_part = "/".join(ranks)
    return f"{board_part} {turn} - - 0 1"


def make_prompt(board: chess.Board, info: list, phase: str) -> str:
    """
    Сформировать краткий промпт для LLM о плане из анализов Stockfish.
    """
    lines = []
    for d in info:
        pv_san = board.variation_san(d["pv"][:5])
        cp = d["score"].pov(board.turn).score()
        lines.append(f"{pv_san}  (eval {cp:+})")
    return (
        f"Фаза: {phase}\n"
        f"FEN: {board.fen()}\n"
        "Варианты Stockfish:\n" + "\n".join(lines) + "\n\n"
        "Сформулируй план пунктами, максимум 2 строки, без приветствий."
    )

def material_signature(board: chess.Board) -> str:
    def side_str(color):
        pieces=[]
        for p, sym in [(chess.QUEEN,'Q'),(chess.ROOK,'R'),(chess.BISHOP,'B'),(chess.KNIGHT,'N'),(chess.PAWN,'P')]:
            n = len(board.pieces(p, color))
            if n: pieces.append(sym*n)
        return "K"+("".join(sorted(pieces)) if pieces else "")
    a = side_str(chess.WHITE); b = side_str(chess.BLACK)
    return " vs ".join(sorted([a,b]))
