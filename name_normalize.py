import unicodedata
from typing import List, Tuple

# --- 1) Minimal Cyrillic→Latin map (ASCII only) ---
_RU = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
    "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
    "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh",
    "щ":"shch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
}

def ru2lat(s: str) -> str:
    out = []
    for ch in s:
        lo = ch.lower()
        out.append(_RU.get(lo, lo))
    return "".join(out)

# --- 2) Core normalization ---
def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))

def normalize_tokens(s: str) -> List[str]:
    """Lowercase, strip accents, transliterate ru→lat, collapse spaces/punct."""
    s = s.strip().lower()
    s = ru2lat(s)                  # кириллица → латиница
    s = strip_accents(s)           # убираем диакритику (å→a, ø→o, æ→ae)
    # убрать пунктуацию, оставить буквы/цифры/пробел
    s = "".join(c if (c.isalnum() or c.isspace()) else " " for c in s)
    s = " ".join(s.split())
    return s.split()

# --- 3) Heuristic RU→EN/NO variants ---
def phonetic_rules(word: str) -> List[str]:
    """Return small set of likely variants for a single token."""
    w = word
    variants = {w}

    # Common RU→EN/NO conflations
    repl = [
        ("ule", "ole"), ("uli", "ole"), ("ulli", "ole"), ("uly", "ole"),
        ("kristian", "christian"), ("kris", "chris"),
        ("sergei", "sergey"), ("sergey", "sergej"),
        ("alexey", "aleksei"), ("aleksey", "aleksei"), ("alexei", "aleksei"),
        ("yuri", "yury"), ("yury", "juri"), ("mikhail", "michail"),
        ("evgeny", "yevgeny"),
    ]
    for a,b in repl:
        if a in w:
            variants.add(w.replace(a, b))
        if b in w:
            variants.add(w.replace(b, a))

    # Norwegian letters already stripped (å→a, ø→o, æ→ae), но добавим ae→e
    if "ae" in w:
        variants.add(w.replace("ae", "e"))

    # y/i/j drift
    variants.add(w.replace("y", "i"))
    variants.add(w.replace("i", "y"))
    variants.add(w.replace("j", "y"))

    # ch/k/c near (‘kristian’↔‘cristian’)
    variants.add(w.replace("k", "c"))
    variants.add(w.replace("c", "k"))

    # collapse double letters
    while "ll" in w:
        variants.add(w.replace("ll", "l"))
        break

    return list({v for v in variants if v})

def make_name_keys(s: str) -> Tuple[str, List[str]]:
    """
    Build a primary key and a small bag of alternate tokens for fuzzy matching.
    Returns (primary_key, alternates_list)
    """
    toks = normalize_tokens(s)
    # Generate alternates per token, keep top-N to avoid blow-up
    alts = []
    for t in toks:
        alts.extend(phonetic_rules(t)[:4])
    primary = " ".join(toks)
    # Dedup and cut
    alts = sorted(set(alts))[:20]
    return primary, alts

# --- 4) Matching ---
def match_names(a: str, b: str) -> bool:
    """
    Fuzzy match two name strings (order-insensitive, prefix-friendly).
    Rule: need >=2 token overlaps by prefix, or primary substring match.
    """
    a_key, a_alt = make_name_keys(a)
    b_key, b_alt = make_name_keys(b)

    if a_key in b_key or b_key in a_key:
        return True

    # token bags with alternates
    a_bag = set(a_key.split()) | set(a_alt)
    b_bag = set(b_key.split()) | set(b_alt)

    # prefix overlap count
    score = 0
    for x in a_bag:
        for y in b_bag:
            if not x or not y:
                continue
            if x.startswith(y) or y.startswith(x):
                score += 1
                if score >= 2:
                    return True
    return False
