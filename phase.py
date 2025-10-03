import chess

def detect(board: chess.Board) -> str:
    """'opening' / 'middlegame' / 'endgame' ‒ простая эвристика."""
    pieces = board.occupied
    total = chess.popcount(pieces)
    if total <= 7:
        return "endgame"
    if board.fullmove_number <= 15:
        return "opening"
    return "middlegame"
