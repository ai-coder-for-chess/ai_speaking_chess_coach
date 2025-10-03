# speech_ru.py
# Russian speech utilities for chess: SAN/coords → natural Russian speech.
# Python 3.13.6, python-chess 1.11.2

from __future__ import annotations
import re
import chess

__all__ = [
    "coord_to_ru", "san_to_speech", "pv_to_speech",
    "strip_move_numbers", "opening_title_to_speech",
    "apply_san_sequence"
]

# Files and digits → Russian syllables
FILE_RU = {"a":"а", "b":"бэ", "c":"це", "d":"дэ", "e":"е", "f":"эф", "g":"же", "h":"аш"}
DIGIT_RU = {"1":"один", "2":"два", "3":"три", "4":"четыре", "5":"пять", "6":"шесть", "7":"семь", "8":"восемь"}
PIECE_RU = {"K":"король", "Q":"ферзь", "R":"ладья", "B":"слон", "N":"конь", "P":""}

_RE_NUM = re.compile(r"^\d+\.{0,3}$")     # 1. 1... 12 ..
_RE_ELLIPSIS = re.compile(r"^\.\.\.$")

def coord_to_ru(sq: str) -> str:
    """Translate 'g3' → 'же три' (simple coordinate)."""
    sq = sq.strip().lower()
    if len(sq) != 2 or sq[0] not in FILE_RU or sq[1] not in DIGIT_RU:
        return sq
    return f"{FILE_RU[sq[0]]} {DIGIT_RU[sq[1]]}"

def _clean_token(tok: str) -> str:
    tok = tok.strip()
    if _RE_NUM.match(tok) or _RE_ELLIPSIS.match(tok):
        return ""
    return tok

def strip_move_numbers(text: str) -> str:
    """Remove 1., 1... and '...' from a raw variant sentence."""
    toks = re.split(r"(\s+|,)", text)
    out = []
    for t in toks:
        if _clean_token(t) == "":
            continue
        out.append(t)
    return "".join(out)

def _take_piece_letter(san: str) -> str:
    # SAN starts with piece letter or file letter; castling handled elsewhere.
    if san and san[0] in "KQRBN":
        return san[0]
    return "P"

def _square_to_ru(square: chess.Square) -> str:
    return coord_to_ru(chess.square_name(square))

def san_to_speech(san: str) -> str:
    """
    Convert a SAN like 'Nf3', 'Bxb5+', 'exd5', 'O-O', 'c8=Q#' to Russian speech:
    - no 'на': say 'слон б5', 'конь эф три'
    - capture uses 'бьёт'
    - checks → 'шах', mate → 'мат'
    """
    san = san.strip()
    if san in ("O-O", "0-0"):
        return "короткая рокировка"
    if san in ("O-O-O", "0-0-0"):
        return "длинная рокировка"

    # Remove annotations like !? etc.
    san_core = re.sub(r"[!?]+", "", san)

    # Promotions c8=Q#
    promo = None
    m = re.search(r"=([QRBN])", san_core)
    if m:
        promo = m.group(1)
        san_core = san_core.replace(f"={promo}", "")

    # Check/mate markers
    tail = []
    if san_core.endswith("#"):
        tail.append("мат")
        san_core = san_core[:-1]
    elif san_core.endswith("+"):
        tail.append("шах")
        san_core = san_core[:-1]

    # Disambiguation like Nbd2 or R1e1
    m = re.match(r"([KQRBN])?([a-h1-8])?x?([a-h][1-8])", san_core)
    capture = "x" in san_core
    piece = _take_piece_letter(san_core)
    if m:
        if m.group(1):
            piece = m.group(1)
        to_sq = m.group(3)
        piece_ru = PIECE_RU[piece]
        to_ru = coord_to_ru(to_sq)
        segs = []
        if piece_ru:
            segs.append(piece_ru)
        if capture:
            segs.append("бьёт")
        segs.append(to_ru)
        if promo:
            segs.append(f"с превращением в {PIECE_RU[promo] or 'ферзя'}".strip())
        if tail:
            segs.extend(tail)
        return " ".join(segs).replace("  ", " ").strip()

    # Pawn quiet move like 'e4'
    m2 = re.match(r"([a-h][1-8])", san_core)
    if m2:
        res = coord_to_ru(m2.group(1))
        if promo:
            res += f" с превращением в {PIECE_RU[promo] or 'ферзя'}"
        if tail:
            res += " " + " ".join(tail)
        return res

    # Fallback: return as-is
    return san

def pv_to_speech(pv_sans: list[str]) -> str:
    """List of SAN → one readable sentence."""
    words = [san_to_speech(s) for s in pv_sans]
    return ", ".join(words)

def opening_title_to_speech(title_ru: str, moves_line: str | list[str] | None = None) -> str:
    """
    Read the ECO title INCLUDING its built-in move tail (e.g. ': 5 c3 Сd7 6 d4 без ...g6')
    in natural Russian: letters → 'це/дэ/же', pieces → 'слон/конь/ладья/ферзь',
    castling, and 'без g6' → 'без же шесть'.
    Fallback: if title has no moves, use moves_line.
    """
    title_full = (title_ru or "").strip().rstrip(".")
    head = title_full
    tail = ""

    # Split by colon: "<name>: <moves/без ...>"
    if ":" in title_full:
        parts = title_full.split(":", 1)
        head = parts[0].strip()
        tail = parts[1].strip()

    # Helper: RU letters → SAN letters for pieces (rough but good for ECO tails)
    def _ru_fig_to_en(tok: str) -> str:
        # Cyrillic letters commonly found in ECO tails:
        # С(slоn)→B, К(kon')→N, Л(lad'ya)→R, Ф(ferz')→Q; also normalize castling
        rep = (
            ("С", "B"), ("с", "B"),
            ("К", "N"), ("к", "N"),
            ("Л", "R"), ("л", "R"),
            ("Ф", "Q"), ("ф", "Q"),
            ("О-О-О", "O-O-O"), ("о-о-о", "O-O-O"),
            ("О-О", "O-O"),     ("о-о", "O-O"),
        )
        for a, b in rep:
            tok = tok.replace(a, b)
        return tok

    # Tokenize the tail; allow commas and multiple spaces
    tokens = []
    if tail:
        # drop commas to simplify, keep '...g6' intact
        raw = tail.replace(",", " ")
        tokens = [t for t in re.split(r"\s+", raw) if t]

    spoken = []
    # Decide correct gender for the verb based on the Russian opening name.
    # Masculine: "гамбит", "дебют", "вариант" => "был сыгран"
    # Feminine:  "защита", "система", "атака", "партия" => "была сыграна"
    # Neuter:    "начало" => "было сыграно"
    def _verb_for_title(head_ru: str) -> str:
        h = (head_ru or "").strip().lower()
        fem_keys = ("защита", "система", "атака", "партия")
        masc_keys = ("гамбит", "дебют", "вариант")
        neut_keys = ("начало",)
        if any(k in h for k in fem_keys):
            return "была сыграна"
        if any(k in h for k in neut_keys):
            return "было сыграно"
        # default to masculine (covers most cases)
        return "был сыгран"

    bez_square = None
    want_bez = False
    # Patterns
    re_num   = re.compile(r"^\d+\.?$")
    re_cast  = re.compile(r"^(O-O(-O)?|0-0(-0)?)$", re.IGNORECASE)
    re_san   = re.compile(r"^[KQRBN]?[a-h]x?[a-h][1-8][+#]?$", re.IGNORECASE)
    re_sq    = re.compile(r"^[a-h][1-8]$", re.IGNORECASE)
    re_ell   = re.compile(r"^\.\.\.([a-h][1-8])$", re.IGNORECASE)

    for t in tokens:
        t0 = t.strip().strip(".")
        if not t0:
            continue

        # 'без' handler — pick the next square ('g6' or '...g6')
        if want_bez:
            m = re_ell.match(t0)
            if m:
                bez_square = m.group(1).lower()
            elif re_sq.match(t0):
                bez_square = t0.lower()
            want_bez = False
            continue

        if t0.lower() == "без":
            want_bez = True
            continue
        if t0 in ("...", "…") or re_num.match(t0):
            continue

        # Normalize russian letters to SAN letters before recognition
        t1 = _ru_fig_to_en(t0)
        if not (re_cast.match(t1) or re_san.match(t1) or re_sq.match(t1) or re_ell.match(t1) or t0.lower() == "без"):
            continue

        # Recognize and speak
        if re_cast.match(t1):
            spoken.append(san_to_speech("O-O-O" if "O-O-O" in t1 or "0-0-0" in t1 else "O-O"))
        elif re_san.match(t1):
            spoken.append(san_to_speech(t1))
        elif re_sq.match(t1):
            spoken.append(coord_to_ru(t1))
        else:
            m = re_ell.match(t1)  # e.g. '...g6'
            if m:
                spoken.append(coord_to_ru(m.group(1).lower()))
            # otherwise skip unknown fragments like words

    # Fallback to moves_line if title had no usable moves
    if not spoken and moves_line:
        if isinstance(moves_line, str):
            ml = strip_move_numbers(moves_line)
            toks = [t for t in re.split(r"[,\s]+", ml) if t]
        else:
            toks = list(moves_line)
        for t in toks:
            t = t.strip(".")
            if re_cast.match(t):
                spoken.append(san_to_speech("O-O-O" if "O-O-O" in t or "0-0-0" in t else "O-O"))
            elif re_san.match(t):
                spoken.append(san_to_speech(t))
            elif re_sq.match(t):
                spoken.append(coord_to_ru(t))
            elif re_ell.match(t):
                m = re_ell.match(t)
                if m:
                    spoken.append(coord_to_ru(m.group(1).lower()))

    # Build final phrase with correct gender
    tail_text = ", ".join(spoken) if spoken else ""
    verb = _verb_for_title(head)
    if tail_text:
        return f"У вас в партии {verb} {head}: {tail_text}"
    else:
        return f"У вас в партии {verb} {head}"


# ---------- Variant parsing for 'what-if' ----------

def apply_san_sequence(board: chess.Board, text: str) -> list[chess.Move]:
    """
    Apply a free-form sequence like 'Nf3, Nc6, e4' or 'e2e4 e7e5' to a copy of board.
    - Skips move numbers and '...'
    - Tries SAN first, then UCI
    Returns the list of legal moves applied (raises ValueError with message on failure).
    """
    raw = strip_move_numbers(text)
    tokens = [t.strip(",") for t in raw.split() if t.strip(",")]
    b = board.copy()
    out: list[chess.Move] = []
    for tok in tokens:
        # SAN first
        try:
            mv = b.parse_san(tok)
            b.push(mv); out.append(mv); continue
        except Exception:
            pass
        # UCI next: e2e4, g1f3, with optional promotion
        if re.match(r"^[a-h][1-8][a-h][1-8][qrbn]?$", tok.lower()):
            try:
                mv = chess.Move.from_uci(tok.lower())
                if mv in b.legal_moves:
                    b.push(mv); out.append(mv); continue
            except Exception:
                pass
        raise ValueError(f"Неверный ход в варианте: «{tok}»")
    return out
