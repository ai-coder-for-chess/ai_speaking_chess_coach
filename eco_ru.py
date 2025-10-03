# eco_ru.py
# Full ECO -> Russian opening names, fetched from chessbase.ru (A..E pages) and cached locally.
from __future__ import annotations
import re
import json
import pathlib
import requests
import html as ihtml
from typing import Dict, Optional
from urllib.parse import urljoin
from paths import ECO_CACHE_FILE

# Toggle minimal debug prints
DEBUG = True

# Cache file lives next to this module
_CACHE = pathlib.Path(__file__).resolve().parent / "eco_ru_cache.json"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
}

def _candidate_urls() -> list[str]:
    """
    Real section pages come from the root menu (A00-A99..E00-E99).
    If discovery fails, fall back to static /a..e pages.
    """
    letters = _discover_letter_urls()
    if not letters:
        letters = [f"{ECO_CACHE_FILE}/{l}/" for l in ("a", "b", "c", "d", "e")]

    # dedupe while keeping order
    seen, out = set(), []
    for u in ECO_CACHE_FILE + letters:
        if u not in seen:
            seen.add(u); out.append(u)

    if DEBUG:
        print("[ECO] pages:", out)
    return out

def _canon_letter_url(href: str, text: str) -> Optional[str]:
    """Return canonical https://www.chessbase.ru/help/eco/{a..e}/ from an <a> href or its text."""
    m = re.search(r'/help/eco/([a-e])/?', href, re.IGNORECASE)
    if m:
        return f"{ECO_CACHE_FILE}/{m.group(1).lower()}/"
    # fallback: derive from link text like "A00-A99"
    m = re.match(r'([A-E])00-[A-E]99', text.strip())
    if m:
        return f"{ECO_CACHE_FILE}/{m.group(1).lower()}/"
    return None

def _discover_letter_urls() -> list[str]:
    """Parse /help/eco and extract absolute URLs for A..E pages."""
    try:
        r = requests.get(ECO_CACHE_FILE, headers=_HEADERS, timeout=12)
        if r.status_code != 200 or not (r.text or "").strip():
            return []
        html = r.text
    except Exception:
        return []

    # anchor whose TEXT looks like 'A00-A99', 'B00-B99', ...
    pat = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>\s*([A-E]00-[A-E]99)\s*</a>', re.IGNORECASE)
    seen, out = set(), []
    for m in pat.finditer(html):
        href, text = m.group(1), m.group(2)
        url = _canon_letter_url(href, text)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# Minimal fallback so we have something even if network fails.
_FALLBACK: Dict[str, str] = {
    "C60": "Испанская партия",
    "C65": "Испанская: вариант Чигорина",
    "C67": "Испанская: Берлинская защита",
    "C88": "Испанская: закрытая система",
    "C50": "Итальянская партия (Джуко Пиано и др.)",
    "B20": "Сицилианская защита",
    "B90": "Сицилианская: Найдорф",
    "D35": "Ферзевый гамбит: отказанный",
    "E20": "Нимцо-индийская защита",
    "E60": "Защита индийского короля",
}

def _is_bad_cache(d: Dict[str, str]) -> bool:
    """Heuristics to detect an obviously broken cache/fetch."""
    if not d or len(d) < 50:
        return True
    bad_keys = {"A00", "B00", "C00", "D00", "E00"}
    if set(d.keys()) <= bad_keys:
        return True
    return False

def _parse_html_to_map(html: str) -> Dict[str, str]:
    """
    Extract rows like (both supported):
      1) <a href="/help/eco/e07/">E07 - Каталонское начало: ...</a>
      2) <a href="/help/eco/e07/"><b>E07</b></a> - Каталонское начало: ...
         (name can be plain text OR inside another <a>)
    We read the ECO code from the href (e07 -> E07), so nested tags (<b>) don't break us.
    """
    eco_map: Dict[str, str] = {}

    # Case A: code+name inside the SAME anchor
    pat_same = re.compile(
        r'<a[^>]+href="/help/eco/([a-e]\d{2})/?"[^>]*>.*?[–—\-]\s*([^<]+?)</a>',
        re.IGNORECASE | re.DOTALL
    )
    for m in pat_same.finditer(html):
        code = m.group(1).upper()          # e07 -> E07
        name = ihtml.unescape(m.group(2)).strip()
        name = re.sub(r"\s+", " ", name)
        if code and name:
            eco_map[code] = name

    # Case B: first anchor has the code (possibly with <b>), name follows after the anchor,
    # optionally wrapped in its own <a>.
    pat_split = re.compile(
        r'<a[^>]+href="/help/eco/([a-e]\d{2})/?"[^>]*>.*?</a>'
        r'\s*(?:&nbsp;|\s)*[–—\-](?:&nbsp;|\s)*'
        r'(?:<a[^>]*>)?([^<\r\n]+)',
        re.IGNORECASE | re.DOTALL
    )
    for m in pat_split.finditer(html):
        code = m.group(1).upper()
        if code in eco_map:
            continue
        name = ihtml.unescape(m.group(2)).strip()
        name = re.sub(r"\s+", " ", name)
        if code and name:
            eco_map[code] = name

    # Safety net: if still too few, try a very loose pattern but do NOT overwrite existing keys.
    if len(eco_map) < 50:
        loose = re.compile(r'\b([A-E]\d{2})\b\s*[–—\-]\s*([^<\r\n]{2,120})')
        for m in loose.finditer(html):
            code = m.group(1).upper()
            if code in eco_map:
                continue
            name = ihtml.unescape(m.group(2)).strip()
            name = re.sub(r"\s+", " ", name)
            if 2 <= len(name) <= 120:
                eco_map[code] = name

    return eco_map


def _load_cache() -> Dict[str, str]:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_cache(data: Dict[str, str]) -> None:
    try:
        _CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _fetch_remote_all() -> Dict[str, str]:
    """
    Fetch ECO->RU mapping from chessbase.ru, walking through known pages A..E.
    We merge results from all pages; two consecutive empty/failed pages -> stop early.
    """
    eco_map: Dict[str, str] = {}
    empty_streak = 0

    for url in _candidate_urls():
        try:
            r = requests.get(url, headers=_HEADERS, timeout=12)
            if r.status_code != 200 or not (r.text or "").strip():
                empty_streak += 1
                if DEBUG:
                    print(f"[ECO] fetch FAIL {url} status={r.status_code}")
                if empty_streak >= 2:
                    break
                continue

            # Ensure encoding
            try:
                if not r.encoding:
                    r.encoding = r.apparent_encoding or "utf-8"
            except Exception:
                r.encoding = "utf-8"

            part = _parse_html_to_map(r.text)
            if DEBUG:
                print(f"[ECO] {url} -> {len(part)} entries")
            if part:
                eco_map.update(part)
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak >= 2:
                    break
        except Exception as e:
            empty_streak += 1
            if DEBUG:
                print(f"[ECO] fetch ERROR {url}: {e}")
            if empty_streak >= 2:
                break

    if DEBUG:
        print(f"[ECO] total merged: {len(eco_map)}")
    return eco_map

def get_eco_map() -> Dict[str, str]:
    """
    Prefer cache; refetch if cache looks wrong; always merge with FALLBACK.
    """
    cache = _load_cache()
    if cache and not _is_bad_cache(cache):
        if DEBUG:
            print(f"[ECO] using cache {len(cache)} @ {_CACHE}")
        return {**_FALLBACK, **cache}

    remote = _fetch_remote_all()
    if remote and not _is_bad_cache(remote):
        _save_cache(remote)
        if DEBUG:
            print(f"[ECO] saved cache {len(remote)} @ {_CACHE}")
        return {**_FALLBACK, **remote}

    if DEBUG:
        print("[ECO] using FALLBACK only")
    return dict(_FALLBACK)

# Public API
ECO_RU: Dict[str, str] = get_eco_map()

def name_from_eco(code: Optional[str], fallback_en: Optional[str] = None) -> Optional[str]:
    """Return Russian opening name by ECO, fallback to English if unknown."""
    if not code:
        return fallback_en
    return ECO_RU.get(code.upper(), fallback_en)

