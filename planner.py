from __future__ import annotations
from typing import Dict, Any, Optional
import chess
from chess.engine import Score, PovScore
from collections.abc import Mapping
from phase import detect as detect_phase
from engine_core import analyse_with_phase
from helpers import make_prompt as make_sf_prompt
from llm_util import ask
from kb import auto_query


try:
    from kb import kb_get_advice_for_fen
except Exception:
    kb_get_advice_for_fen = None

def make_plan_for_fen(fen: str, depth: int = 18) -> Dict[str, Any]:
    try:
        from kb import kb_get_advice_for_fen
        return kb_get_advice_for_fen(fen, depth=depth)
    except Exception:
        pass

    # Фолбэк: старый планер → приводим к новой схеме
    base = plan_for_fen(fen)
    # вытащим из sf_lines "первую" строку как PV, остальное – как есть
    pv = ""
    if base.get("sf_lines"):
        pv = base["sf_lines"][0].split("  (eval")[0].strip()
    return {
        "found": True,
        "fen": fen,
        "phase": base.get("phase","middlegame"),
        "sf_eval": 0.0,           # оценки нет в старом формате — оставим 0.0
        "best": "",               # SAN лучшего хода из старой формы достать нельзя надёжно
        "pv": pv,
        "depth": 0,
        "plan": base.get("advice",""),
        "opponent_ideas": "",
        "pitfalls": "",
        "examples": base.get("examples", []),
    }


def render_plan_text(fen: str, depth: int = 18) -> str:
    """
    Устойчивый вывод:
    - если make_plan_for_fen вернул KB-формат (found=True) — используем его;
    - если вернулась «старая» форма (plan_for_fen) — корректно рендерим её.
    """
    data = make_plan_for_fen(fen, depth=depth)

    # Ветка 1: "новый" KB-формат
    if isinstance(data, dict) and data.get("found") is True:
        lines = [
            f"Фаза: {data.get('phase','?')}",
            "",
            "Stockfish:",
            f"  Лучший ход: {data.get('best','')}  (eval {data.get('sf_eval',0):+.2f}, depth {data.get('depth','?')})",
            f"  PV: {data.get('pv','')}",
            "",
            "Advice:",
            str(data.get('plan','')),
            "",
            "Идеи соперника:",
            str(data.get('opponent_ideas') or ''),
            "",
            "Подводные камни:",
            str(data.get('pitfalls') or ''),
        ]
        return "\n".join(lines)

    # Ветка 2: "старая" форма (как у plan_for_fen)
    if isinstance(data, dict) and ("advice" in data or "sf_lines" in data):
        lines = [f"Фаза: {data.get('phase','?')}", "", "Stockfish:"]
        for s in data.get("sf_lines", []) or []:
            lines.append(f"  {s}")
        if not data.get("sf_lines"):
            lines.append("  (нет данных)")
        lines += [
            "",
            "Advice:",
            str(data.get("advice","")),
            "",
            "Идеи соперника:",
            "",  # старый формат их не отдаёт
            "",
            "Подводные камни:",
            "",  # старый формат их не отдаёт
        ]
        return "\n".join(lines)

    # Если пришло что-то совсем неожиданное
    err = (data.get("error") if isinstance(data, dict) else None) or "unknown"
    return f"Не удалось получить анализ позиции ({err})."


# ──────────────────────────────────────────────────────────────────────────
def _format_eval_generic(score: Score | PovScore | None, turn: chess.Color) -> str:
    """Форматтер, дружелюбный к разным типам score и версиям python-chess."""
    if score is None:
        return "0.00"
    try:
        pov = getattr(score, "pov", None)
        s = pov(turn) if callable(pov) else score

        # mate: бывает как атрибут (int), так и как вызываемое свойство
        mate_attr = getattr(s, "mate", None)
        m = mate_attr() if callable(mate_attr) else mate_attr
        if isinstance(m, (int, float)) and m is not None:
            # показываем абсолютное расстояние до мата
            return f"M{abs(int(m))}"

        # cp: поддержим .cp, .score() или .score
        cp_val = getattr(s, "cp", None)
        if cp_val is None:
            sc_attr = getattr(s, "score", None)
            cp_val = sc_attr() if callable(sc_attr) else sc_attr

        cp_num = float(cp_val) if isinstance(cp_val, (int, float)) else 0.0
        return f"{cp_num/100.0:+.2f}"
    except Exception:
        return "0.00"


def _first_nonempty_line(s: str | None) -> str:
    if not s:
        return ""
    for line in s.splitlines():
        t = line.strip()
        if t:
            return t
    return ""

def side_ru(turn: bool) -> str:
    return "Белых" if turn else "Чёрных"

def _field(d, name, default=None):
    try:
        if isinstance(d, Mapping):
            return d.get(name, default)
        return getattr(d, name, default)
    except Exception:
        return default

def _pick_examples(rows, max_n=3):
    """Унификация строк из БД + безопасный срез комментария."""
    out = []
    for r in rows[:max_n]:
        # Ожидаемый формат:
        # (fen, phase, comment, white, black, result, eco, opening, is_mainline, ply, nags?) — последние поля опциональны
        fen = r[0] if len(r) > 0 else ""
        phase = r[1] if len(r) > 1 else ""
        comment = r[2] if len(r) > 2 else ""
        w = r[3] if len(r) > 3 else ""
        b = r[4] if len(r) > 4 else ""
        result = r[5] if len(r) > 5 else ""
        eco = r[6] if len(r) > 6 else ""
        opening = r[7] if len(r) > 7 else ""
        title = f"{(w or '').strip()}–{(b or '').strip()} {(result or '').strip()}".strip(" –")
        info = " ".join(x for x in [eco or "", opening or ""] if x).strip()
        snippet = _first_nonempty_line(comment)[:160]
        out.append({"fen": fen, "title": title, "info": info, "comment": snippet})
    return out
# ──────────────────────────────────────────────────────────────────────────

def plan_for_fen(fen: str, multipv_open=3) -> dict:
    """Главная функция. Возвращает словарь с ключами:
       phase, sf_lines, examples, advice
       — даже если что-то упадёт, вернёт «пустой, но валидный» ответ.
    """
    try:
        board = chess.Board(fen)
    except Exception as e:
        # Совсем неправильный FEN — вернём stub
        return {
            "phase": "unknown",
            "sf_lines": [],
            "examples": [],
            "advice": f"Ошибка: некорректный FEN ({e})."
        }

    # 1) Фаза партии
    try:
        phase = detect_phase(board)
    except Exception:
        phase = "middlegame"

    # 2) Анализ движка (устойчиво)
    info = []
    try:
        raw = analyse_with_phase(board, phase, multipv_open=multipv_open)
        info = raw if isinstance(raw, list) else [raw]
    except Exception:
        info = []

    # 3) Локальная БД (устойчиво)
    examples = []
    try:
        q = auto_query(fen, limit=8)
        rows = q.get("rows", []) if isinstance(q, dict) else []
        examples = _pick_examples(rows, max_n=3)
    except Exception:
        examples = []

    # 4) Сборка строк SF (устойчиво к пустым pv/score)
    sf_lines = []
    try:
        btmp = chess.Board(fen)
        for d in info:
            pv_moves = _field(d, "pv") or []
            try:
                pv_san = btmp.variation_san(pv_moves[:5]) if pv_moves else "(нет pv)"
            except Exception:
                pv_san = "(не удалось сформировать pv)"
            sc = _field(d, "score", None)
            ev = _format_eval_generic(sc, btmp.turn) if sc is not None else "0.00"
            sf_lines.append(f"{pv_san}  (eval {ev})")
    except Exception:
        sf_lines = []
    
        # --- вместо старого блока с base_prompt/prompt/ask() — точечная сборка текста ---
    who = "Белых" if board.turn else "Чёрных"
    best = _best_move_san_from_info(fen, info)
    opp  = _counter_idea_from_info(fen, info)
    trap = _trap_from_alt_line(fen, info)

    # соберём вводные для LLM (короче и конкретнее)
    ex_text = ""
    if examples:
        bullets = []
        for ex in examples:
            line = f"• {ex['title']}"
            if ex['info']: line += f" ({ex['info']})"
            if ex['comment']: line += f": {ex['comment']}"
            bullets.append(line)
        ex_text = "Примеры (кратко):\n" + "\n".join(bullets) + "\n\n"

    base_prompt = (
        f"Ты тренер. Сторона на ходу: {who}. Ход-кандидат по движку: {best}.\n"
        f"Главный ресурс соперника (первый ответ): {opp if opp!='—' else 'невыражен'}.\n"
        "Сформулируй ответ строго в 4 пункта, по-русски и без англицизмов:\n"
        "1) Ход-кандидат(ы): коротко и по делу.\n"
        "2) План: 1–2 ключевые идеи (манёвры, куда ставим фигуры, какие линии вскрываем/закрываем).\n"
        "3) Идеи соперника: одна главная контригра и как её сдержать.\n"
        "4) Подводные камни: 1 типовая ошибка и чем она плоха.\n"
        "Не пиши длинных вариантов. Если примеры помогают, сошлись на них кратко.\n\n"
        + ex_text
    )
    try:
        if info:
            prompt = base_prompt + make_sf_prompt(board, info, phase)
        else:
            prompt = base_prompt + f"Фаза: {phase}\nFEN: {board.fen()}\n"

        advice_text = (ask(prompt) or "").strip()
    except Exception:
        advice_text = (
            f"1) Ход-кандидат: {best}\n"
            "2) План: улучшить координацию фигур и создать угрозы по слабым полям.\n"
            f"3) Идея соперника: {opp if opp!='—' else 'ликвидировать активность и разменять фигуры'}.\n"
            "4) Подводные камни: не ослабляйте короля преждевременными вскрытиями."
        )
    if trap:
        advice_text += f"\n\n[Ловушка] {trap}"
    return {
        "phase": phase,
        "sf_lines": sf_lines,
        "examples": examples,
        "advice": advice_text
    }

def _best_move_san_from_info(fen: str, info_list) -> str:
    import chess
    b = chess.Board(fen)
    if not info_list: return "—"
    pv = (info_list[0].get("pv") or [])
    if not pv: return "—"
    try:
        san = b.san(pv[0])
    except Exception:
        san = pv[0].uci() if pv else "—"
    return san

def _counter_idea_from_info(fen: str, info_list) -> str:
    # «главный ресурс соперника» = первый ответный ход в лучшем PV
    import chess
    b = chess.Board(fen)
    if not info_list: return "—"
    pv = (info_list[0].get("pv") or [])
    if len(pv) < 2: return "—"
    try:
        b.push(pv[0])
        san_reply = b.san(pv[1])
    except Exception:
        san_reply = pv[1].uci()
    return san_reply

def _trap_from_alt_line(fen: str, info_list) -> str | None:
    # если у второй линии eval значительно хуже (например, хуже на ≥ 0.7 пешки),
    # покажем «подводный камень»: первый ход альтернативы
    import chess
    if len(info_list) < 2: return None
    b = chess.Board(fen)
    pv2 = info_list[1].get("pv") or []
    s1 = info_list[0].get("score")
    s2 = info_list[1].get("score")
    def cp(sc):
        try:
            if hasattr(sc, "pov"): sc = sc.pov(b.turn)
            m = sc.mate()
            if m is not None:
                # трактуем мат как очень большую оценку
                return 10000 if m > 0 else -10000
            return float(sc.score() or 0)
        except Exception:
            return 0.0
    if pv2 and (cp(s1) - cp(s2) >= 70):  # 0.70 пешки
        try:
            bad = b.san(pv2[0])
        except Exception:
            bad = pv2[0].uci()
        return f"Не делайте {bad}: оценка заметно хуже по альтернативной линии."
    return None
def make_plan(fen: Optional[str] = None, board: Optional[chess.Board] = None) -> dict:
    """
    Алиас к plan_for_fen для удобства.
    Можно вызывать либо с fen, либо с готовым board.
    """
    if board is not None and fen is None:
        fen = board.fen()
    if fen is None:
        raise ValueError("make_plan: укажите fen или board")
    return plan_for_fen(fen)

try:
    from kb import pick_rook_4v3_candidate
except Exception:
    pick_rook_4v3_candidate = None

def suggest_theme_rook_4v3() -> dict:
    """
    Вернуть предложение позиции под тему «ладейник 4×3 на одном фланге».
    Формат: {"found": bool, "fen": ..., "title": ..., "info": ..., "comment": ...}
    """
    if pick_rook_4v3_candidate is None:
        return {"found": False, "error": "kb_missing"}
    cand = pick_rook_4v3_candidate()
    if not cand:
        return {"found": False, "error": "not_found"}
    out = cand.copy()
    out["found"] = True
    return out

def analyze_theme_rook_4v3(accept: bool = True) -> dict:
    """
    1) Подбирает позицию по теме.
    2) Если accept=True — сразу делает полноценный анализ через plan_for_fen и возвращает расширенный ответ.
       Если accept=False — вернёт только «предложение» без анализа.
    """
    sug = suggest_theme_rook_4v3()
    if not sug.get("found"):
        return {"found": False, "error": sug.get("error", "not_found")}
    if not accept:
        return {"found": True, "suggestion": sug}
    # полноценный анализ вашей штатной функцией
    analysis = plan_for_fen(sug["fen"])
    return {"found": True, "suggestion": sug, "analysis": analysis}
# === [/ADDED] ===
