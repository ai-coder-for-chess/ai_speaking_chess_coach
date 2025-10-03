# main.py — GUI + ASR + TTS, understanding commands via LLM
# Dependencies:
# OS: Windows (using PowerShell/COM for audio)
import chess
import pyaudio, audioop, wave, io, collections
import os
import re
import time, json
import queue
import tempfile
import threading
import subprocess
import webbrowser
import unicodedata
import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from tkinter import filedialog, messagebox, StringVar, ttk
from pathlib import Path
from typing import Optional, Any, Tuple, List, Dict
from asr_backend import init_asr, transcribe_wav
from coach_session import CoachSession
from engine_core import ENGINE_PATH, analyze_fen
from speech_ru import strip_move_numbers, san_to_speech, pv_to_speech, opening_title_to_speech
from paths import ensure_dirs, ROOT, DATA_DIR, PGN_DIR, ECO_CACHE_FILE, ENGINE_PATH, DB_PATH
ensure_dirs()


# ---------- LLM ----------
from llm_util import ask

# ---------- ASR: faster-whisper ----------
from faster_whisper import WhisperModel

_ASR = None
_CALIB_PATH = Path.home() / ".ai_chess_eta_calib.json"

# unified paths (string form where needed)
ENGINE_PATH = str(ENGINE_PATH)         # engine code may expect str
PGN_PATH    = str(PGN_DIR)
ECO_PATH    = str(ECO_CACHE_FILE)
DB_SQLITE   = str(DB_PATH)
ECO_CACHE_PATH = ECO_PATH
DB_FILE        = DB_SQLITE

# Where to look for analyzed games
ANALYSIS_DIRS = [
    Path("analysis_out"),
    Path(os.environ.get("ANALYSIS_OUT", "")) if os.environ.get("ANALYSIS_OUT") else None,
    Path(os.environ.get("ANALYZE_OUT", ""))  if os.environ.get("ANALYZE_OUT")  else None,
]
ANALYSIS_DIRS = [p for p in ANALYSIS_DIRS if p]  # drop Nones

_RE_JUST_ASK_CORRECT = re.compile(r"\b(как\s+было\s+правильно|что\s+нужно\s+было|какой\s+правильный\s+ход)\b", re.IGNORECASE)
# Примитивный детектор одного SAN-хода внутри фразы (O-O, O-O-O, фигуры/пешки)
_RE_SAN_IN_TEXT = re.compile(r"\b(O-O(-O)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?)\b", re.IGNORECASE)

def maybe_extract_san(text: str) -> str | None:
    m = _RE_SAN_IN_TEXT.search(text or "")
    return m.group(0) if m else None
def extract_variant_san(text: str, data: dict, board: chess.Board) -> str:
    """
    Try to get a SAN sequence to analyze from:
    1) data.moves_san
    2) natural phrases about castling ("короткая/длинная рокировка")
    3) data.moves_uci -> SAN
    4) single SAN guess inside the text
    """
    san = (data.get("moves_san") or "").strip()
    if san:
        return san

    t = (text or "").lower()
    if re.search(r"коротк\w*\s+рокиров", t, re.IGNORECASE):
        return "O-O"
    if re.search(r"длинн\w*\s+рокиров", t, re.IGNORECASE):
        return "O-O-O"

    uci_list = data.get("moves_uci") or []
    if uci_list:
        try:
            return uci_list_to_san_line(board, uci_list)
        except Exception:
            pass

    guess = maybe_extract_san(text)
    return (guess or "").strip()

def is_ask_how_correct(text: str) -> bool:
    return bool(_RE_JUST_ASK_CORRECT.search(text or ""))

def _load_eta_calib():
    if _CALIB_PATH.exists():
        try:
            return json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Стартовые параметры, близкие к твоим замерам:
    # r ~ 1.36 на +1 ply глубины, base_per_ply_15 ~ 0.40 c/полуход при depth=15, MultiPV=3
    return {"r": 1.44, "alpha_mpv": 0.95, "base_per_ply_15": 0.40, "t0": 20.0}

def _save_eta_calib(data: dict):
    try:
        _CALIB_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def count_plies_in_pgn(path: str | Path) -> int:
    try:
        import chess.pgn
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            game = chess.pgn.read_game(f)
        if game:
            return sum(1 for _ in game.mainline())
    except Exception:
        pass
    # страховка: оценим по числу полных ходов
    try:
        import re
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read(200_000)
        fullmoves = len(re.findall(r"\b\d+\.", txt))
        return max(10, min(400, (fullmoves or 30) * 2))
    except Exception:
        return 60

def eta_predict_seconds(plies: int, depth: int, multipv: int, pvlen: int) -> int:
    """
    Экспоненциальная модель по глубине + линейная поправка MultiPV + постоянная надбавка t0.
    PV длина на время почти не влияет — игнорируем.
    """
    C = _load_eta_calib()
    r = float(C.get("r", 1.36))
    alpha = float(C.get("alpha_mpv", 0.95))
    base = float(C.get("base_per_ply_15", 0.40))  # сек/полуход при depth=15, MPV=3
    t0 = float(C.get("t0", 20.0))                 # постоянная надбавка на прогрев/файлы/GUI

    scale_depth = r ** max(0, (depth - 15))
    scale_mpv = (max(1, multipv) / 3.0) ** alpha
    seconds = t0 + base * float(plies) * scale_depth * scale_mpv
    return int(max(1, seconds))

def eta_update_calibration_after_run(plies: int, depth: int, multipv: int, elapsed_sec: float):
    """
    Update ETA calibration after a finished run.
    Now we also estimate 'r' (depth growth factor) from the observed time.
    """
    C = _load_eta_calib()
    r = float(C.get("r", 1.44))
    alpha = float(C.get("alpha_mpv", 0.95))
    old_base = float(C.get("base_per_ply_15", 0.40))
    old_t0 = float(C.get("t0", 20.0))

    # Depth & MultiPV scaling (without r for now)
    denom_mpv_plies = ((max(1, multipv) / 3.0) ** alpha) * max(1, plies)
    depth_power = max(0, (depth - 15))

    # Recompute base/t0 using current r
    measured_t0 = max(0.0, elapsed_sec - old_base * (r ** depth_power) * denom_mpv_plies)
    measured_base = max(0.0, (elapsed_sec - old_t0) / ((r ** depth_power) * denom_mpv_plies))

    # One-run estimate for r if depth != 15 and numbers are sane
    r_est = r
    if depth_power > 0 and elapsed_sec > old_t0 and old_base > 0:
        K = (elapsed_sec - old_t0) / (old_base * denom_mpv_plies)
        # Guard against numerical issues
        K = max(1.0, K)
        try:
            r_est = K ** (1.0 / depth_power)
        except Exception:
            r_est = r
        # Keep r in a reasonable chess-engine range
        r_est = max(1.18, min(1.70, r_est))

    # Exponential smoothing
    C["base_per_ply_15"] = 0.7 * old_base + 0.3 * measured_base
    C["t0"] = max(0.0, min(120.0, 0.7 * old_t0 + 0.3 * measured_t0))
    C["r"]  = 0.8 * r + 0.2 * r_est

    _save_eta_calib(C)

# ---------- normalization utilities / microphone recording ----------
def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("ø", "o").replace("æ", "ae").replace("å", "a")

# ====== VAD-settings ======
_VAD_SAMPLE_RATE = 16000
_VAD_CHANNELS = 1
_VAD_SAMPLE_WIDTH = 2
_VAD_FRAME_MS = 30

# start/stopp
_VAD_START_MS = 90
_VAD_END_MS = 700
_VAD_MAX_SEG_SEC = 30
_VAD_PREROLL_MS = 600
_VAD_MIN_SEG_MS = 450

def _frames_per_buffer():
    return int(_VAD_SAMPLE_RATE * _VAD_FRAME_MS / 1000)

def _frames_count(ms: int) -> int:
    return max(1, ms // _VAD_FRAME_MS)

def _pcm16_to_wav_bytes(pcm: bytes, sr: int = _VAD_SAMPLE_RATE) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, 'wb') as wf:
        wf.setnchannels(_VAD_CHANNELS)
        wf.setsampwidth(_VAD_SAMPLE_WIDTH)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return bio.getvalue()

def _calibrate_threshold(stream, seconds=1.0, floor=250, mult=1.7):
    n_frames = int((seconds * 1000) / _VAD_FRAME_MS)
    vals = []
    for _ in range(n_frames):
        raw = stream.read(_frames_per_buffer(), exception_on_overflow=False)
        vals.append(audioop.rms(raw, _VAD_SAMPLE_WIDTH))
    base = sum(vals) / max(1, len(vals))
    return max(int(base * mult), floor)

def _stream_segments(
    stop_event: Optional[threading.Event] = None,
    device_index: Optional[int] = None,
):
    """PCM speech segment generator (bytes) with auto start/stop on silence."""
    pa = pyaudio.PyAudio()
    try:
        # --- select input device ---
        if device_index is None:
            try:
                default_info = pa.get_default_input_device_info()
                device_index = int(default_info.get("index", -1))
            except Exception:
                device_index = -1

            if device_index < 0:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    max_in = int(info.get("maxInputChannels", 0))
                    if max_in > 0:
                        device_index = i
                        break

        if device_index is None or device_index < 0:
            raise RuntimeError("No available audio input device found.")

        # --- open the stream ---
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=_VAD_CHANNELS,
            rate=_VAD_SAMPLE_RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=_frames_per_buffer(),
        )

        try:
            # --- calibration and preparation ---
            THRESHOLD = _calibrate_threshold(stream, seconds=1.0, floor=300, mult=1.9)
            voiced_needed  = _frames_count(_VAD_START_MS)
            silence_needed = _frames_count(_VAD_END_MS)
            preroll_len    = _frames_count(_VAD_PREROLL_MS)

            recent   = collections.deque(maxlen=voiced_needed)
            noise_ema: Optional[int] = None
            preroll  = collections.deque(maxlen=preroll_len)

            in_seg = False
            buf = bytearray()
            silence = 0
            start_time: Optional[float] = None

            # --- main reading loop ---
            while True:
                if stop_event and stop_event.is_set():
                    break

                raw = stream.read(_frames_per_buffer(), exception_on_overflow=False)
                preroll.append(raw)

                rms = audioop.rms(raw, _VAD_SAMPLE_WIDTH)
                is_speech = rms >= THRESHOLD

                if not in_seg:
                    noise_ema = rms if noise_ema is None else int(0.9 * noise_ema + 0.1 * rms)
                    THRESHOLD = max(int((noise_ema or 1) * 1.7), 250)
                    recent.append(is_speech)

                    if len(recent) == recent.maxlen and sum(recent) >= (voiced_needed - 1):
                        in_seg = True
                        buf.clear()
                        silence = 0
                        start_time = time.time()
                        for fr in preroll:
                            buf += fr
                        preroll.clear()

                else:
                    buf += raw
                    silence = 0 if is_speech else (silence + 1)

                    elapsed = (time.time() - start_time) if start_time else 0.0
                    if silence >= silence_needed or (start_time and elapsed >= _VAD_MAX_SEG_SEC):
                        yield bytes(buf)
                        in_seg = False
                        buf.clear()
                        silence = 0
                        start_time = None

        finally:
            # close stream even if it crashes in the loop
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    finally:
        # guaranteed to terminate PyAudio
        try:
            pa.terminate()
        except Exception:
            pass

def listen_vad_once(lang_code: str, log_fn, stop_event: threading.Event) -> str:
    """
    One phrase: we wait for speech → we finish after silence → we recognize strictly in the selected language.
    """
    # a small gap to avoid catching the TTS tail
    time.sleep(0.1)

    # we take exactly ONE segment from the VAD generator
    gen = _stream_segments(stop_event=stop_event, device_index=None)
    try:
        pcm_seg = next(gen)  # blockingly waits for a phrase
    except StopIteration:
        return ""

    # pack into WAV bytes and write to a temporary file (for transcribe_wav)
    wav_bytes = _pcm16_to_wav_bytes(pcm_seg, sr=_VAD_SAMPLE_RATE)
    tmp = tempfile.mkstemp(suffix=".wav")[1]
    try:
        with open(tmp, "wb") as f: f.write(wav_bytes)
        # listen ONLY to the selected language
        langs = (lang_code,)
        text = transcribe_wav(tmp, langs=langs)
        if text:
            log_fn(f"Heard: {text}")
        return (text or "").strip()
    finally:
        try: os.remove(tmp)
        except OSError: pass

# ======================= LLM intents =======================
INTENT_SYSTEM = """
Ты ассистент голосового шахматного тренера. Определи намерение пользователя по короткой русской фразе
и верни ОДНУ строку JSON без лишних слов и форматирования.

Схема:
{"intent": <start_opponent|start_latest|confirm|continue|choose|what_if|goto|step|start|end|ask|repeat|exit|unknown>,
 "lang":"ru",
 "data": {
   "today": true|false,
   "opponent":"...",                   // для start_opponent
   "index": 1,                         // для choose (1 или 2)
   "moves_san":"...",                  // what_if
   "moves_uci":["e2e4","e7e5"],        // what_if (альтернатива)
   "move": 25,                         // goto (полный ход)
   "side": "white"|"black"|null,       // goto
   "san":"..."                         // для goto_by_san — один ход в SAN, например "Bxe6"
   "delta": -2,                        // step (в полуходах)
   "question":"..."                    // ask
 }}

Правила:
- "проанализируй партию против <ИМЯ>", "открой партию с <ИМЯ>" → start_opponent.
- "проанализируй сегодняшнюю/последнюю партию" → start_latest (today=true|false).
- "да", "поехали", "начинай", "готов", "я готов", "давай начнём", "давай начинаем" → confirm.
- "дальше", "продолжай", "идём дальше" → continue.
- "первый вариант", "второй вариант", "давай второй" → choose (index=1|2).
- "что если ..." → what_if (желательно SAN; если не уверен — UCI).
- "посмотрим <SAN/рокировку>", "вариант <SAN>", "давай посмотрим <SAN>" → what_if.
- "к 25-му ходу", "к 18 чёрных" → goto.
- "вернись к ходу <SAN>", "вернуться к ходу <SAN>", "вернись к моменту <SAN>" → goto_by_san.
- "вперёд на два хода", "назад на один ход" → step (ход=2 полухода).
- "в начало"/"в конец" → start/end.
- Любой шахматный вопрос → ask.
- "повтори" → repeat, "выход" → exit.

Примеры:
"хочу проанализировать партию против Уле Кристиана" => {"intent":"start_opponent","lang":"ru","data":{"opponent":"Уле Кристиан"}}
"вернись к ходу Bxe6" => {"intent":"goto_by_san","lang":"ru","data":{"san":"Bxe6"}}
"давай начнём" => {"intent":"confirm","lang":"ru","data":{}}
"второй вариант" => {"intent":"choose","lang":"ru","data":{"index":2}}
"что если Nf3 Nc6 e4" => {"intent":"what_if","lang":"ru","data":{"moves_san":"Nf3 Nc6 e4"}}
"дальше" => {"intent":"continue","lang":"ru","data":{}}
"давай посмотрим ход d4" => {"intent":"what_if","lang":"ru","data":{"moves_san":"d4"}}
"посмотрим короткую рокировку" => {"intent":"what_if","lang":"ru","data":{"moves_san":"O-O"}}
"""

def understand_with_llm(text: str, prefer_lang: str) -> dict:
    prompt = (
        f"{INTENT_SYSTEM}\n\n"
        f"Фраза: «{text}»\n"
        f"Предпочтительный язык интерфейса: {prefer_lang}\n"
        f"Верни только JSON."
    )
    try:
        import json
        resp = ask(prompt)
        resp = resp.strip().strip("`")
        start = resp.find("{")
        end = resp.rfind("}")
        obj = json.loads(resp[start:end+1])
        # страховка по полям
        if "intent" not in obj:
            obj["intent"] = "unknown"
        if "lang" not in obj:
            obj["lang"] = prefer_lang
        if "data" not in obj:
            obj["data"] = {"raw": text}
        return obj
    except Exception:
        return {"intent": "unknown", "lang": prefer_lang, "data": {"raw": text}}

def uci_list_to_san_line(board: chess.Board, moves_uci: list[str]) -> str:
    """Convert list of UCI strings to a SAN string from given board."""
    b = board.copy()
    out = []
    for u in moves_uci:
        mv = chess.Move.from_uci(u)
        out.append(b.san(mv))
        b.push(mv)
    return " ".join(out)


# ======================= TTS MANAGER =======================
class TTSManager:
    """Dedicated voiceover stream. Backends:
    - 'edge' → Microsoft Edge Neural (online), male voices: ru-Dmitry, en-Guy, no-Finn;
    - 'sapi' → Windows System.Speech (offline via PowerShell), auto-selection by culture.
    Each line is spoken synchronously until the end.
    """
    def __init__(self, log_fn, backend: str = "edge"):
        self._log = log_fn
        self._backend = backend  # 'edge' | 'sapi'
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = threading.Event()
        self._sapi_voice_map = {"ru": None, "en": None, "no": None}

    def start(self):
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._q.put(("", "en", threading.Event()))

    def speak_sync(self, text: str, lang: str):
        done = threading.Event()
        self._q.put((text, lang, done))
        done.wait()

    # ---------- generall ----------
    def _detect_sapi_voices(self):
        ps_enum = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.GetInstalledVoices() | ForEach-Object {
  $vi = $_.VoiceInfo
  Write-Output ("{0}|{1}" -f $vi.Name, $vi.Culture)
}
"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ps1", mode="w", encoding="utf-8") as f:
            f.write(ps_enum)
            path = f.name
        try:
            res = subprocess.run(
                ["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",path],
                capture_output=True, text=True
            )
            out = (res.stdout or "").strip()
            ru=en=nb=None
            for line in out.splitlines():
                parts = line.split("|", 1)
                if len(parts) != 2: continue
                name, culture = parts[0].strip(), parts[1].strip().lower()
                if not ru and culture.startswith("ru"): ru = name
                if not en and culture.startswith("en"): en = name
                if not nb and (culture.startswith("nb") or culture.startswith("no")): nb = name
            if not en: en = "Microsoft Zira Desktop - English (United States)"
            if not ru: ru = en
            if not nb: nb = en
            self._sapi_voice_map = {"ru": ru, "en": en, "no": nb}
        finally:
            try: os.remove(path)
            except OSError: pass

    # ---------- SAPI (offline) ----------
    def _sapi_speak(self, text: str, lang: str):
        voice_name = self._sapi_voice_map.get(lang) or self._sapi_voice_map.get("en")
        if lang == "ru":
            text = (text.replace("±"," плюс-минус ").replace("−"," минус ")
                        .replace("+"," плюс ").replace("-"," минус "))
        ps = r"""
param([string]$voice,[string]$text)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $s.SelectVoice($voice) } catch {}
$s.Rate = 0; $s.Volume = 100
$s.Speak($text)
"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ps1", mode="w", encoding="utf-8") as f:
            f.write(ps)
            path = f.name

        try:
            args: List[str] = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", path,
                str(voice_name),
                str(text),
            ]
            completed = subprocess.run(
                args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError("PowerShell не найден в PATH. Установите/проверьте доступность `powershell`.") from e
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8", "ignore") if e.stderr else str(e)
            raise RuntimeError(f"TTS-скрипт PowerShell завершился с ошибкой:\n{err}") from e
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    # ---------- Edge Neural (online) ----------
    async def _edge_to_file(self, text: str, voice: str, path: str):
        import edge_tts
        tts = edge_tts.Communicate(text=text, voice=voice)
        with open(path, "wb") as f:
            async for chunk in tts.stream():
                #In practice, chunk is dict[str, Any]
                if isinstance(chunk, dict) and chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if isinstance(data, (bytes, bytearray)):
                        f.write(data)


    def _edge_play_mp3_sync(self, mp3_path: str):
        p = mp3_path.replace("'", "''")
        ps = f"""
    Add-Type -AssemblyName PresentationCore
    $u = New-Object System.Uri('{p}')
    $player = New-Object System.Windows.Media.MediaPlayer
    $player.Open($u)
    $player.Volume = 1.0
    $player.Play()
    while (-not $player.NaturalDuration.HasTimeSpan) {{ Start-Sleep -Milliseconds 100 }}
    while ($player.Position -lt $player.NaturalDuration.TimeSpan) {{ Start-Sleep -Milliseconds 100 }}
    $player.Close()
    """
        subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-Command", ps], check=True)

    def _edge_speak(self, text: str, lang: str):
        import asyncio, tempfile, os
        voice = {
            "ru": "ru-RU-DmitryNeural",
            "en": "en-US-GuyNeural",
            "no": "nb-NO-FinnNeural",
        }.get(lang, "en-US-GuyNeural")

        mp3 = tempfile.mkstemp(suffix=".mp3")[1]
        try:
            asyncio.run(self._edge_to_file(text, voice, mp3))
            self._edge_play_mp3_sync(mp3)
        except Exception:
            self._sapi_speak(text, lang)
        finally:
            try: os.remove(mp3)
            except OSError: pass

    # ---------- work flow ----------
    def _run(self):
        self._detect_sapi_voices()
        if self._backend == "edge":
            try:
                import edge_tts  # noqa
            except Exception:
                self._backend = "sapi"

        while not self._stop.is_set():
            try:
                text, lang, done = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if not text:
                    done.set(); continue
                if lang == "ru":
                    text = (text.replace("±"," плюс-минус ").replace("−"," минус ")
                                .replace("+"," плюс ").replace("-"," минус "))
                # NEW: log spoken line
                try:
                    self._log(f"Say: {text}")
                except Exception:
                    pass
                if self._backend == "edge":
                    self._edge_speak(text, lang)
                else:
                    self._sapi_speak(text, lang)
            except Exception as e:
                pass
            finally:
                time.sleep(0.1)
                done.set()

# ======================= Actions =======================
def open_analysis_startpos():
    webbrowser.open("https://lichess.org/analysis")

# ======================= Frases =======================
COMMANDS = {
    "ru": {
        "hello":  "Привет. Что будем сегодня делать?",
        "prompt": "",
        "opened": "Открыто.",
        "bye":    "Выход.",
        "unknown":"Не расслышал. Повтори, пожалуйста."
    },
    "en": {
        "hello":  "Ready.",
        "prompt": "Say a command: open analysis, or exit.",
        "opened": "Opened the analysis board.",
        "bye":    "Exiting.",
        "unknown":"Command not recognized."
    },
    "no": {
        "hello":  "Klar.",
        "prompt": "Si en kommando: åpne analyse, eller avslutt.",
        "opened": "Åpnet analysebrettet.",
        "bye":    "Avslutter.",
        "unknown":"Kommando ikke gjenkjent."
    }
}

# ======================= Рабочий цикл =======================
def run_assistant(lang_code: str, log_fn, stop_event: threading.Event, on_done, tts: TTSManager):
    coach: CoachSession | None = None
    last_reply: str = ""
    awaiting_confirm_to_start = False 
    awaiting_variant_choice = False   
    last_branch_info: dict | None = None
    awaiting_mistake_query = False      # ждём "как правильно?"
    last_mistake_hint: dict | None = None  # {"played_san":..., "best_san":..., "cpl":..., "mark":...}
    coach = None 

    try:
        init_asr(model_size="large-v3", device="cpu", compute_type="int8")
        tts.speak_sync(COMMANDS[lang_code]["hello"], lang_code)

        coach: Optional[CoachSession] = None      
        last_reply: str = ""                   

        while not stop_event.is_set():
            raw = listen_vad_once(lang_code, log_fn, stop_event)
            text = raw.strip()
            if not text:
                continue

            intent = understand_with_llm(text, lang_code)
            it = intent.get("intent", "unknown")

            if it == "exit":
                tts.speak_sync(COMMANDS[lang_code]["bye"], lang_code)
                break

            elif it == "start_opponent":
                data = intent.get("data", {}) or {}
                opp = (data.get("opponent") or "").strip()
                if not opp:
                    tts.speak_sync("Не расслышал имя соперника.", lang_code)
                    continue
                if coach:
                    coach.close()
                coach = CoachSession(str(ENGINE_PATH))
                pgn_path = CoachSession.find_pgn_by_opponent(ANALYSIS_DIRS, opp)
                if not pgn_path:
                    existing = [str(p) for p in ANALYSIS_DIRS if p.exists()]
                    log_fn(f"[search] Not found. Searched in: {', '.join(existing)}")
                    tts.speak_sync("Не нашел такую партию.", lang_code)
                    continue
                coach.load_pgn(pgn_path)
                awaiting_confirm_to_start = True
                awaiting_variant_choice = False
                last_branch_info = None
                tts.speak_sync(COMMANDS[lang_code]["opened"], lang_code)  # «Открыто. Готов начать просмотр?»

            elif it == "start_latest":
                if re.search(r"\bпротив\b", text, re.IGNORECASE):
                    tts.speak_sync("Не расслышал имя соперника.", lang_code)
                    continue
                data = intent.get("data", {}) or {}
                today_only = bool(data.get("today", True))
                if coach:
                    coach.close()
                coach = CoachSession(str(ENGINE_PATH))
                pgn_path = CoachSession.find_latest_pgn(ANALYSIS_DIRS, today_only=today_only)
                if not pgn_path:
                    existing = [str(p) for p in ANALYSIS_DIRS if p.exists()]
                    log_fn(f"[latest] Not found. Searched in: {', '.join(existing)}")
                    tts.speak_sync("Партия не найдена.", lang_code)
                    continue
                coach.load_pgn(pgn_path)
                awaiting_confirm_to_start = True
                awaiting_variant_choice = False
                last_branch_info = None
                tts.speak_sync(COMMANDS[lang_code]["opened"], lang_code)

            elif it == "goto_by_san":
                if not (coach and coach.state.game):
                    tts.speak_sync("Партия не открыта.", lang_code); continue

                data = intent.get("data", {}) or {}
                phrase = text or ""

                # --- эвристика: фразы типа «посмотрим/вариант/посмотри/покажи» => это анализ (what_if)
                wants_analysis = bool(re.search(r"\b(вариант|посмотр(им|и)|посмотри|покажи)\b", phrase, re.IGNORECASE))

                # распознаём рокировку в естественной форме
                castling_san = None
                if re.search(r"коротк\w*\s+рокиров", phrase, re.IGNORECASE):
                    castling_san = "O-O"
                elif re.search(r"длинн\w*\s+рокиров", phrase, re.IGNORECASE):
                    castling_san = "O-O-O"

                if wants_analysis or castling_san:
                    # → Перенаправляем в «анализ варианта»
                    san = extract_variant_san(phrase, data, coach.state.board)
                    if not san:
                        tts.speak_sync("Не распознала вариант. Повторите, пожалуйста.", lang_code); continue

                    res = coach.what_if_san(san, depth=23, multipv=2, show_progress=True)
                    if "error" in res:
                        tts.speak_sync("Ошибка в варианте: " + res["error"], lang_code); continue
                    lines = res.get("lines", [])
                    if not lines:
                        tts.speak_sync("Нет варианта от движка.", lang_code); continue

                    def fmt_cp(cp: int) -> str:
                        return "мат" if abs(cp) >= 9000 else f"{cp/100.0:+.2f}"

                    tts.speak_sync(f"Проверила: {san_to_speech(san)}. {fmt_cp(lines[0]['cp'])}.", lang_code)
                    try:
                        pv0 = (lines[0].get("pv_san") or "")
                        pv_clean = strip_move_numbers(pv0)
                        pv_tokens = [t for t in pv_clean.split() if t]
                        pv_spoken = pv_to_speech(pv_tokens[:6])  # до 3 полных ходов
                        if pv_spoken:
                            tts.speak_sync(pv_spoken, lang_code)
                    except Exception:
                        pass
                    # Ждём «продолжай» или новый запрос
                    continue

                # --- обычный сценарий «перейти к моменту SAN» (goto) ---
                san = (data.get("san") or "").strip()
                if not san:
                    san = (maybe_extract_san(phrase) or "").strip()
                if not san:
                    tts.speak_sync("Не расслышал ход.", lang_code); continue

                st = coach.goto_by_san(san)
                if "error" in st:
                    tts.speak_sync("Не нашел такой ход в мейнлайне.", lang_code)
                else:
                    tts.speak_sync("Готово.", lang_code)

            elif it == "confirm":
                if awaiting_confirm_to_start and coach and coach.state.game:
                    info = coach.autoplay_to_first_branch(min_fullmove=4, max_fullmove=6)
                    if "error" in info:
                        tts.speak_sync("Не удалось начать просмотр.", lang_code)
                        continue
                    awaiting_confirm_to_start = False
                    last_branch_info = info

                    # Say opening name only once per session
                    name = coach.opening_name_once()
                    if name:
                        # Собираем аккуратную русскую фразу про дебют
                        try:
                            # Попробуем вытащить пару первых «книжных» ходов для красивой концовки
                            book = coach.opening_info_current(top_n=3)
                            top = [m["san"] for m in (book.get("top") or [])]
                            phrase = opening_title_to_speech(title_ru=name, moves_line=top[:3])
                            tts.speak_sync(phrase, lang_code)
                        except Exception:
                            tts.speak_sync(f"Это {name}.", lang_code)
                    # Озвучим первые ходы партии до развилки
                    path = info.get("path_san") or []
                    if path:
                        tts.speak_sync("Начало партии: " + pv_to_speech(path), lang_code)
                    
                    mist = coach.detect_mistake_on_next_main_move(depth=18)
                    played = (mist.get("played_san") or "")
                    best_for_mist = (mist.get("best_san") or "")
                    mark = (mist.get("mark") or "")
                    quality = {"": "нормальный", "?!": "неточный", "?": "ошибочный", "??": "зевок"}.get(mark, "нормальный")

                    # Who is to move now?
                    is_user_to_move = (coach.state.user_side is not None and coach.state.user_side == coach.state.board.turn)
                    who = "Ты" if is_user_to_move else "Соперник"

                    if played:
                        if best_for_mist and best_for_mist == played:
                            # user's (or opponent's) move equals engine's first line
                            tts.speak_sync(f"В этой позиции {who.lower()} сыграл: {san_to_speech(played)}. Это первая линия.", lang_code)
                        else:
                            tts.speak_sync(f"В этой позиции {who.lower()} сыграл: {san_to_speech(played)}. Это {quality} ход.", lang_code)


                    # 3) Engine: best line + equal-strength alternatives (never duplicate the best)
                    opts = coach.first_line_and_equal_alts(depth=16, multipv=3)
                    best_san = opts.get("best_san") or ""
                    equal_alts_raw = opts.get("equal_alts") or []

                    played = (mist.get("played_san") or "")
                    equal_alts = [a for a in equal_alts_raw if a and a != best_san and a != played]
                    # Keep these engine alternatives for the 'choose' intent
                    if isinstance(last_branch_info, dict):
                        last_branch_info["engine_alts"] = equal_alts

                    # Short PV for the best line (≈2 full moves)
                    pv_spoken = ""
                    try:
                        fen_now = coach.state.board.fen()
                        best_info = analyze_fen(fen_now, depth=16) or {}
                        pv = best_info.get("pv") or []
                        # всегда приводим к списку SAN-токенов
                        if isinstance(pv, str):
                            pv = [t for t in strip_move_numbers(pv).split() if t]
                        if isinstance(pv, list):
                            pv = [t.strip() for t in pv if t.strip()]
                        pv_spoken = pv_to_speech(pv[:4]) if pv else ""
                    except Exception:
                        pv_spoken = ""

                    # Skip speaking engine's first line if it matches the played move
                    played_now = (mist.get("played_san") or "")
                    if played_now and best_san and played_now == best_san:
                        # Do not speak the first line block at all
                        pass
                    else:
                        if best_san:
                            if pv_spoken:
                                tts.speak_sync(f"Первая линия движка: {pv_spoken}.", lang_code)
                            else:
                                tts.speak_sync(f"Первая линия движка: {san_to_speech(best_san)}.", lang_code)

                    if best_san:
                        if pv_spoken:
                            tts.speak_sync(f"Первая линия движка: {pv_spoken}.", lang_code)
                        else:
                            tts.speak_sync(f"Первая линия движка: {san_to_speech(best_san)}.", lang_code)

                    if equal_alts:
                        alts_spoken = [san_to_speech(x) for x in equal_alts[:2]]
                        phr = "Альтернативы: " + ", ".join(alts_spoken)
                        tts.speak_sync(phr + ".", lang_code)
                        awaiting_variant_choice = True
                    else:
                        awaiting_variant_choice = False
                else:
                    it = "continue"

            elif it == "choose":
                if not (coach and coach.state.game and last_branch_info and awaiting_variant_choice):
                    tts.speak_sync("Сейчас не жду выбора варианта.", lang_code); continue
                idx = int((intent.get("data", {}) or {}).get("index") or 1)
                alt_list = (last_branch_info.get("engine_alts") if last_branch_info else None) or \
                            (last_branch_info.get("alt_first", []) if last_branch_info else []) or []
                if not (1 <= idx <= len(alt_list)):
                    tts.speak_sync("Такого варианта нет.", lang_code); continue
                first_move = alt_list[idx-1]
                # Глубокий анализ выбранной альтернативы (d=23) с прогрессом в консоли
                res = coach.what_if_san(first_move, depth=23, multipv=2, show_progress=True)
                if "error" in res:
                    tts.speak_sync("Ошибка в варианте.", lang_code); continue
                lines = res.get("lines", [])
                if not lines:
                    tts.speak_sync("Нет варианта от движка.", lang_code); continue
                def fmt_cp(cp: int) -> str:
                    return "мат" if abs(cp) >= 9000 else f"{cp/100.0:+.2f}"
                summary = " ; ".join([f"{i['idx']}) {fmt_cp(i['cp'])}" for i in lines[:2]])
                tts.speak_sync(f"Проверила: {first_move}. {summary}. Продолжать?", lang_code)
                # Озвучим первый вариант движка (2–3 полных хода)
                try:
                    pv0 = (lines[0].get("pv_san") if lines else "") or ""
                    pv_clean = strip_move_numbers(pv0)
                    pv_tokens = [t for t in pv_clean.split() if t]
                    pv_spoken = pv_to_speech(pv_tokens[:6])
                    if pv_spoken:
                        tts.speak_sync(pv_spoken, lang_code)
                except Exception:
                    pass

                awaiting_variant_choice = False  # дальше слушаем «продолжай» или новые запросы

            elif it == "continue":
                if not (coach and coach.state.game):
                    tts.speak_sync("Партия не открыта.", lang_code); continue
                # шаг по мейнлайну до следующей развилки (или просто +1/-? — выберем до следующей развилки)
                info = coach.autoplay_to_first_branch(
                    min_fullmove=coach.state.board.fullmove_number+1,
                    max_fullmove=999
                )
                if "error" in info:
                    tts.speak_sync("Не удалось перейти дальше.", lang_code); continue

                # 1) Проговорим, какие ходы прошли по мейнлайну с прошлой развилки
                prev_idx = 0
                try:
                    if last_branch_info and "branch_ply" in last_branch_info:
                        prev_idx = int(last_branch_info["branch_ply"])
                except Exception:
                    prev_idx = 0

                curr_idx = int(info.get("branch_ply", prev_idx))
                path_all = info.get("path_san") or []
                segment = path_all[prev_idx:curr_idx]
                if segment:
                    tts.speak_sync(pv_to_speech(segment), lang_code)

                last_branch_info = info

                # 2) Tell what you actually played on this new branching point
                mist = coach.detect_mistake_on_next_main_move(depth=18)
                played = (mist.get("played_san") or "")
                best_for_mist = (mist.get("best_san") or "")
                mark = (mist.get("mark") or "")
                quality = {"": "нормальный", "?!": "неточный", "?": "ошибочный", "??": "зевок"}.get(mark, "нормальный")

                # Who is to move now?
                is_user_to_move = (coach.state.user_side is not None and coach.state.user_side == coach.state.board.turn)
                who = "Ты" if is_user_to_move else "Соперник"

                if played:
                    if best_for_mist and best_for_mist == played:
                        # user's (or opponent's) move equals engine's first line
                        tts.speak_sync(f"{who.lower()} сыграл: {san_to_speech(played)}. Это первая линия.", lang_code)
                    else:
                        tts.speak_sync(f"{who.lower()} сыграл: {san_to_speech(played)}. Это {quality} ход.", lang_code)


                # 3) Engine best + equal alternatives (no duplication)
                opts = coach.first_line_and_equal_alts(depth=16, multipv=3)
                best_san = opts.get("best_san") or ""
                equal_alts_raw = opts.get("equal_alts") or []
                played = (mist.get("played_san") or "")
                equal_alts = [a for a in equal_alts_raw if a and a != best_san and a != played]
                if isinstance(last_branch_info, dict):
                    last_branch_info["engine_alts"] = equal_alts

                pv_spoken = ""
                try:
                    fen_now = coach.state.board.fen()
                    best_info = analyze_fen(fen_now, depth=16) or {}
                    pv = best_info.get("pv") or []
                    # всегда приводим к списку SAN-токенов
                    if isinstance(pv, str):
                        pv = [t for t in strip_move_numbers(pv).split() if t]
                    if isinstance(pv, list):
                        pv = [t.strip() for t in pv if t.strip()]
                    pv_spoken = pv_to_speech(pv[:4]) if pv else ""
                except Exception:
                    pv_spoken = ""
                
                # Skip speaking engine's first line if it matches the played move
                played_now = (mist.get("played_san") or "")
                if played_now and best_san and played_now == best_san:
                    # Do not speak the first line block at all
                    pass
                else:
                    if best_san:
                        if pv_spoken:
                            tts.speak_sync(f"Первая линия движка: {pv_spoken}.", lang_code)
                        else:
                            tts.speak_sync(f"Первая линия движка: {san_to_speech(best_san)}.", lang_code)

                if best_san:
                    if pv_spoken:
                        tts.speak_sync(f"Первая линия движка: {pv_spoken}.", lang_code)
                    else:
                        tts.speak_sync(f"Первая линия движка: {san_to_speech(best_san)}.", lang_code)

                if equal_alts:
                    alts_spoken = [san_to_speech(x) for x in equal_alts[:2]]
                    phr = "Альтернативы: " + ", ".join(alts_spoken)
                    tts.speak_sync(phr + ".", lang_code)
                    awaiting_variant_choice = True
                else:
                    awaiting_variant_choice = False

            elif it == "what_if":
                if not (coach and coach.state.game):
                    tts.speak_sync("Сначала откроем партию.", lang_code); continue
                data = intent.get("data", {}) or {}
                san_line = extract_variant_san(text, data, coach.state.board)
                if not san_line:
                    tts.speak_sync("Не распознала вариант. Повторите, пожалуйста.", lang_code); continue
                # Deep (d=23) with visible progress in console
                res = coach.what_if_san(san_line, depth=23, multipv=2, show_progress=True)
                if "error" in res:
                    tts.speak_sync("Ошибка в варианте: " + res["error"], lang_code); continue
                lines = res.get("lines", [])
                if not lines:
                    tts.speak_sync("Нет варианта от движка.", lang_code); continue

                # Возьмём главную PV, почистим от номеров и озвучим
                pv_full: str = (lines[0].get("pv_san") or "")
                pv_clean = strip_move_numbers(pv_full)
                pv_tokens: list[str] = [t for t in pv_clean.split() if t]
                pv_short: list[str] = pv_tokens[:6] if len(pv_tokens) >= 2 else pv_tokens  # до ~3 полных ходов

                if len(pv_short) > 0:
                    tts.speak_sync("Вариант: " + pv_to_speech(pv_short), lang_code)
                else:
                    try:
                        pv0 = (lines[0].get("pv_san") or "")
                        pv_clean = strip_move_numbers(pv0)
                        pv_tokens = [t for t in pv_clean.split() if t]
                        pv_spoken = pv_to_speech(pv_tokens[:6])
                        if pv_spoken:
                            tts.speak_sync("Вариант: " + pv_spoken, lang_code)
                        else:
                            tts.speak_sync("Вариант пустой.", lang_code)
                    except Exception:
                        tts.speak_sync("Вариант пустой.", lang_code)
                    awaiting_variant_choice = False
            elif it == "goto":
                if not (coach and coach.state.game):
                    tts.speak_sync("Партия не открыта.", lang_code); continue
                data = intent.get("data", {}) or {}
                move = int(data.get("move") or 1)
                side = data.get("side")
                # Переход просто к полуходу перед указанным ходом/стороной
                st0 = coach.current_status()
                st = coach.goto_ply(0)  # к началу, затем пройдём до нужного
                # Грубая навигация: сходим к ближайшей развилке после указанного хода
                start_mv = max(1, move)
                info = coach.autoplay_to_first_branch(min_fullmove=start_mv, max_fullmove=999)
                last_branch_info = info if "error" not in info else None
                tts.speak_sync("Готово.", lang_code)

            elif it == "step":
                if not (coach and coach.state.game):
                    tts.speak_sync("Партия не открыта.", lang_code); continue
                data = intent.get("data", {}) or {}
                delta = int(data.get("delta") or 0)
                st = coach.goto_ply(coach.state.ply_idx + delta)
                if "error" in st:
                    tts.speak_sync("Не удалось сделать шаг.", lang_code); continue
                tts.speak_sync("Готово.", lang_code)

            elif it == "start":
                if not (coach and coach.state.game):
                    tts.speak_sync("Партия не открыта.", lang_code); continue
                coach.goto_ply(0)
                tts.speak_sync("Готово.", lang_code)

            elif it == "end":
                if not (coach and coach.state.game):
                    tts.speak_sync("Партия не открыта.", lang_code); continue
                coach.goto_ply(len(coach.state.nodes_mainline))
                tts.speak_sync("Готово.", lang_code)

            elif it == "ask":
                if not (coach and coach.state.game):
                    tts.speak_sync("Сначала откроем партию.", lang_code); continue
                q = (intent.get("data", {}) or {}).get("question") or text

                # 1) Если ждали ответа на ошибку и пользователь спросил «как было правильно»
                if awaiting_mistake_query and is_ask_how_correct(q):
                    awaiting_mistake_query = False
                    best_san = (last_mistake_hint or {}).get("best_san") or ""
                    if best_san:
                        tts.speak_sync(f"Правильно: {san_to_speech(best_san)}.", lang_code)
                    else:
                        # fallback — просто взять лучший ход сейчас
                        qe = coach.quick_eval(depth=18, multipv=1)
                        lines = qe.get("lines", [])
                        if lines and lines[0].get("pv_san"):
                            first = lines[0]["pv_san"].split()[0]
                            tts.speak_sync(f"Правильно: {san_to_speech(first)}.", lang_code)
                        else:
                            tts.speak_sync("Сейчас лучшего хода не вижу.", lang_code)
                    continue

                # 2) Если в фразе явно есть один SAN-ход — проверим его как «что если»
                san_guess = maybe_extract_san(q)
                if san_guess:
                    res = coach.what_if_san(san_guess, depth=20, multipv=2, show_progress=True)
                    if "error" in res:
                        tts.speak_sync("Не смогла разобрать этот ход.", lang_code); continue
                    lines = res.get("lines", [])
                    if not lines:
                        tts.speak_sync("Движок не дал варианта.", lang_code); continue
                    def fmt_cp(cp: int) -> str:
                        return "мат" if abs(cp) >= 9000 else f"{cp/100.0:+.2f}"
                    # кратко: покажем оценку первой линии
                    tts.speak_sync(f"После {san_to_speech(san_guess)}: {fmt_cp(lines[0]['cp'])}.", lang_code)
                    continue

                # 3) Иначе — обычный короткий ответ-план
                reply = coach.coach_reply(q, depth=18)
                tts.speak_sync(reply, lang_code)
                last_reply = reply

    except Exception as e:
        log_fn(f"Fatal error: {e}")
    finally:
        try:
            if coach:
                coach.close()
        except Exception:
            pass
        on_done()

# ======================= GUI =======================
class AnalyzeGameDialog(tk.Toplevel):
    def __init__(self, master, initial_depth=20):
        super().__init__(master)
        self.title("Analyze PGN game")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.result: Optional[dict] = None

        self.selected_path = tk.StringVar()
        self.file_label_var = tk.StringVar(value="—")
        self.depth_var = tk.IntVar(value=max(15, min(30, int(initial_depth or 20))))
        self.eta_var = tk.StringVar(value="—")

        # Row 1: PGN file (no text entry, only button + filename label)
        row = ttk.Frame(self); row.pack(fill="x", padx=10, pady=6)
        ttk.Label(row, text="PGN file:").pack(side="left")
        ttk.Button(row, text="Browse…", command=self._choose_file).pack(side="left", padx=(6, 6))
        ttk.Label(row, textvariable=self.file_label_var, width=42, anchor="w").pack(side="left")

        # Row 2: Engine depth 15..30
        row2 = ttk.Frame(self); row2.pack(fill="x", padx=10, pady=6)
        ttk.Label(row2, text="Engine depth:").pack(side="left")
        cb = ttk.Combobox(row2, state="readonly", width=6,
                          values=[str(d) for d in range(15, 31)],
                          textvariable=self.depth_var)
        cb.pack(side="left", padx=(6, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._update_eta())

        # Row 3: MultiPV + PV length on one line
        row4 = ttk.Frame(self); row4.pack(fill="x", padx=10, pady=6)
        # MultiPV
        ttk.Label(row4, text="MultiPV:").pack(side="left")
        self.var_multipv = tk.IntVar(value=3)
        self.cb_multipv = ttk.Combobox(row4, values=[str(n) for n in range(1, 6)],
                                       state="readonly", width=5)
        self.cb_multipv.set(str(self.var_multipv.get()))
        self.cb_multipv.pack(side="left", padx=(6, 12))
        self.cb_multipv.bind("<<ComboboxSelected>>", lambda e: self._on_mpv_select())
        # PV length (full moves)
        ttk.Label(row4, text="PV length (full moves):").pack(side="left")
        self.var_pvlen = tk.IntVar(value=6)  # default: 6 full moves
        self.cb_pvlen = ttk.Combobox(row4, values=[str(n) for n in range(3, 13)],
                                     state="readonly", width=5)
        self.cb_pvlen.set(str(self.var_pvlen.get()))
        self.cb_pvlen.pack(side="left", padx=6)
        self.cb_pvlen.bind("<<ComboboxSelected>>", lambda e: self._on_pvlen_select())

        # Row 4: ETA
        row3 = ttk.Frame(self); row3.pack(fill="x", padx=10, pady=6)
        ttk.Label(row3, text="Estimated time:").pack(side="left")
        ttk.Label(row3, textvariable=self.eta_var, width=20).pack(side="left", padx=(6, 0))

        # Row 5: Buttons
        btns = ttk.Frame(self); btns.pack(fill="x", padx=10, pady=6)
        ttk.Button(btns, text="Cancel", command=self._on_cancel).pack(side="right")
        ttk.Button(btns, text="Analyze", command=self._on_ok).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self._on_cancel())

        self._update_eta()

    def _on_mpv_select(self):
        try:
            self.var_multipv.set(int(self.cb_multipv.get()))
        except Exception:
            pass
        self._update_eta()

    def _on_pvlen_select(self):
        try:
            self.var_pvlen.set(int(self.cb_pvlen.get()))
        except Exception:
            pass
        self._update_eta()

    def _choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose a PGN file",
            filetypes=[("PGN files", "*.pgn"), ("All files", "*.*")],
        )
        if path:
            self.selected_path.set(path)
            try:
                import os
                self.file_label_var.set(os.path.basename(path))
            except Exception:
                self.file_label_var.set(path)
            self._update_eta()

    def _estimate_seconds(self):
        path = self.selected_path.get()
        if not path:
            self.eta_var.set("—")
            return
        plies = count_plies_in_pgn(path)
        try:
            depth = int(self.depth_var.get() or 20)
        except Exception:
            depth = 20
        try:
            multipv = int(self.var_multipv.get() or 3)
        except Exception:
            multipv = 3
        try:
            pvlen = int(self.var_pvlen.get() or 6)
        except Exception:
            pvlen = 6

        seconds = eta_predict_seconds(plies, depth, multipv, pvlen)
        if seconds < 60:
            self.eta_var.set(f"{seconds} sec")
        else:
            m, s = divmod(int(seconds), 60)
            self.eta_var.set(f"{m} min {s} sec")


    def _update_eta(self):
        self._estimate_seconds()


    def _on_ok(self):
        if not self.selected_path.get():
            messagebox.showwarning("No file", "Please choose a PGN file.")
            return
        self.result = {
            "pgn_path": self.selected_path.get(),
            "depth": int(self.depth_var.get()),
            "multipv": int(self.var_multipv.get() or 3),
            "pv_moves": int(self.var_pvlen.get() or 6),  # full moves
        }
        self.grab_release(); self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release(); self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("AI Chess Voice – LLM intents")
        self.geometry("900x520")

        top = ttk.Frame(self); top.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(top, text="Language:").pack(side=tk.LEFT)
        self.lang_var = tk.StringVar(value="ru")
        self.lang_combo = ttk.Combobox(top, textvariable=self.lang_var, state="readonly", width=18,
                                       values=("ru – Русский", "en – English", "no – Norsk (Bokmål)"))
        self.lang_combo.pack(side=tk.LEFT, padx=8)

        ttk.Label(top, text="Voice:").pack(side=tk.LEFT)
        self.tts_var = tk.StringVar(value="edge")
        self.tts_combo = ttk.Combobox(top, textvariable=self.tts_var, state="readonly", width=24,
                                      values=("edge – Neural (online)", "sapi – Offline (Windows)"))
        self.tts_combo.pack(side=tk.LEFT, padx=8)

        self.start_btn = ttk.Button(top, text="Start", command=self.start_loop); self.start_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn  = ttk.Button(top, text="Stop",  command=self.stop_loop, state=tk.DISABLED); self.stop_btn.pack(side=tk.LEFT, padx=4)

        # Внутри App.__init__ после создания основного фрейма/виджетов:
        self.analyze_btn = ttk.Button(top, text="Analyze PGN game",
                              command=self.on_click_analyze_game)
        self.analyze_btn.pack(side=tk.RIGHT, padx=8)

        self.console = ScrolledText(self, height=24, wrap=tk.WORD)
        self.console.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0,8))
        self.console.configure(state=tk.DISABLED)

        self.tts: Optional[TTSManager] = None
        self.worker = None
        self.stop_event = threading.Event()

        self.log("Ready. Choose 'edge – Neural (online)' for best voices. 'Heard:' lines only.")
        self.status_var = tk.StringVar(value="Ready.")
        self.status_label = tk.Label(self, textvariable=self.status_var, anchor="w")
        self.status_label.pack(fill="x", padx=8, pady=(4, 8))
        # Progress widgets (hidden by default)
        self.progress = ttk.Progressbar(self, orient="horizontal", mode="determinate", maximum=100)
        self.progress_label = tk.StringVar(value="")
        self.progress_text = ttk.Label(self, textvariable=self.progress_label, anchor="w")


    def _set_status(self, text: str):
        try:
            if hasattr(self, "status_var"):
                self.status_var.set(text)
            else:
                print(text)
        except Exception:
            print(text)

    def on_click_analyze_game(self):
        dlg = AnalyzeGameDialog(self, initial_depth=20)
        self.wait_window(dlg)
        if not dlg.result:
            return

        res = dlg.result  # теперь IDE знает, что это dict
        p = Path(res["pgn_path"])
        depth = int(res["depth"])
        multipv = int(res.get("multipv", 3))
        pv_moves = int(res.get("pv_moves", 6))

        plies = count_plies_in_pgn(p)
        eta_hint = eta_predict_seconds(plies, depth, multipv, pv_moves)

        self._set_status(f"Analyzing: {p.name}, depth {depth}, MultiPV {multipv}, PV {pv_moves}…")
        self._start_game_analysis(p, depth, multipv, pv_moves, precomputed_plies=plies, eta_hint=eta_hint)

    def _start_game_analysis(self, pgn_path: Path, depth: int, multipv: int = 3, pv_moves: int = 12,
                         precomputed_plies: int | None = None, eta_hint: int | None = None):
        """Run analysis in a worker thread with UI progress + ETA."""
        try:
            self.status_var.set(f"Analyzing: {pgn_path.name} (depth {depth}, MultiPV {multipv}, PV {pv_moves})…")
        except Exception:
            pass

        from game_analyzer import analyze_game

        # подготовим прогресс-виджеты
        if not hasattr(self, "progress"):
            # на всякий случай, если не создали в __init__
            self.progress = ttk.Progressbar(self, orient="horizontal", mode="determinate", maximum=100)
            self.progress_label = tk.StringVar(value="")
            self.progress_text = ttk.Label(self, textvariable=self.progress_label, anchor="w")

        # показать прогресс-бар
        self.progress["value"] = 0
        self.progress.pack(fill="x", padx=8, pady=(0, 2))
        self.progress_text.pack(fill="x", padx=8, pady=(0, 8))
        self.progress_label.set("0% — estimating…")

        def _fmt_eta_text(pct: int, seconds: int) -> str:
            if seconds < 60:
                return f"{pct}% — {seconds} sec left"
            m, s = divmod(int(seconds), 60)
            return f"{pct}% — {m} min {s} sec left"
        
        plies = precomputed_plies if precomputed_plies is not None else count_plies_in_pgn(pgn_path)
        if eta_hint is None:
            eta_hint = eta_predict_seconds(plies, depth, multipv, pv_moves)

        # Initialize ETA label once and DO NOT reset start_ts again
        start_ts = time.time()
        self.progress_label.set(_fmt_eta_text(0, int(eta_hint)))

        # Smoothing state for ETA
        eta_prev = float(eta_hint)    # last displayed ETA (sec)
        eta_smooth = float(eta_hint)  # EMA buffer

        def progress_cb(done: int, total: int):
            nonlocal eta_smooth, eta_prev

            frac = (done / total) if total else 0.0
            elapsed = time.time() - start_ts

            # Two sources:
            # 1) model-based ETA: predicted - elapsed (robust early on)
            eta_model = max(0.0, float(eta_hint) - elapsed)

            # 2) live ETA from progress (noisy at low progress)
            if frac > 0.0:
                eta_live = elapsed * (1.0 - frac) / max(frac, 1e-6)
            else:
                eta_live = float(eta_hint)

            # Blend: trust model first, switch to live by 50%.
            # alpha = 0 at 15% → 1 at 50%
            alpha = 0.0
            if frac > 0.15:
                alpha = min(1.0, (frac - 0.15) / 0.35)

            eta_raw = (1.0 - alpha) * eta_model + alpha * eta_live

            # Exponential smoothing (EMA)
            eta_smooth = 0.7 * eta_smooth + 0.3 * eta_raw

            # Monotonic non-increasing display (avoid upward jumps)
            eta_smooth = min(eta_smooth, eta_prev)
            eta_prev = eta_smooth

            pct = int(frac * 100)
            ui_eta = int(max(0.0, eta_smooth))

            def ui():
                self.progress["value"] = pct
                self.progress_label.set(_fmt_eta_text(pct, ui_eta))
            self.after(0, ui)


        def _run():
            try:
                analyze_game(
                    pgn_path,
                    depth=depth,
                    multipv=multipv,
                    pv_moves_limit=pv_moves,
                    progress_cb=progress_cb, 
                )
                # финальное обновление калибровки
                elapsed = time.time() - start_ts
                eta_update_calibration_after_run(plies, depth, multipv, elapsed)

                def ui_ok():
                    self.status_var.set("Ready. Analysis finished.")
                    self.progress.pack_forget()
                    self.progress_text.pack_forget()
                self.after(0, ui_ok)

            except Exception as e:
                def ui_err():
                    self.status_var.set("Analysis error.")
                    self.progress.pack_forget()
                    self.progress_text.pack_forget()
                    messagebox.showerror("Analysis error", str(e))
                self.after(0, ui_err)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def selected_lang_code(self) -> str:
        v = self.lang_var.get()
        if v.startswith("ru"): return "ru"
        if v.startswith("en"): return "en"
        if v.startswith("no"): return "no"
        return "en"

    def selected_backend(self) -> str:
        return "edge" if self.tts_var.get().startswith("edge") else "sapi"

    def log(self, msg: str):
        def append():
            self.console.configure(state=tk.NORMAL)
            self.console.insert(tk.END, msg + "\n")
            self.console.see(tk.END)
            self.console.configure(state=tk.DISABLED)
        self.console.after(0, append)

    def start_loop(self):
        if self.worker and self.worker.is_alive():
            return
        lang = self.selected_lang_code()
        backend = self.selected_backend()
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.stop_event.clear()

        if self.tts:
            try: self.tts.stop()
            except Exception: pass
        self.tts = TTSManager(self.log, backend)
        self.tts.start()

        def on_done():
            def ui():
                self.start_btn.configure(state=tk.NORMAL)
                self.stop_btn.configure(state=tk.DISABLED)
            self.after(0, ui)

        self.worker = threading.Thread(
            target=run_assistant,
            args=(lang, self.log, self.stop_event, on_done, self.tts),
            daemon=True
        )
        self.worker.start()

    def stop_loop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()

    def destroy(self):
        try:
            if self.tts: self.tts.stop()
        finally:
            super().destroy()

def main():
    import argparse
    from pathlib import Path
    import sys
    import traceback

    parser = argparse.ArgumentParser(description="AI Chess Assistant — анализ PGN и режим 'what-if'")
    parser.add_argument("--analyze-pgn", type=str, help="Путь к PGN-файлу для анализа (CLI-режим)")
    parser.add_argument("--depth", type=int, default=20, help="Глубина движка (по умолчанию 20)")
    parser.add_argument("--multipv", type=int, default=3, help="Сколько вариантов (MultiPV), по умолчанию 3")
    parser.add_argument("--pv-moves", type=int, default=5, help="Ограничение длины PV в ходах (по умолчанию 5)")
    parser.add_argument("--whatif", action="store_true", help="Режим 'what-if' по jsonl (CLI-режим)")
    parser.add_argument("--jsonl", type=str, help="Путь к jsonl-файлу (для --whatif)")
    parser.add_argument("--ply", type=int, help="Номер полухода (для --whatif)")
    parser.add_argument("--san", type=str, help="Ход в SAN (для --whatif)")
    args, _ = parser.parse_known_args()

    # ---------- CLI-режим ----------
    if args.analyze_pgn or args.whatif:
        try:
            if args.analyze_pgn:
                from game_analyzer import analyze_game
                out_dir = Path("analysis_out")
                result_paths = analyze_game(
                    pgn_path=Path(args.analyze_pgn),
                    depth=args.depth,
                    multipv=args.multipv,
                    pv_moves_limit=args.pv_moves,
                    out_dir=out_dir,
                )
                print("Готово. Сгенерированные файлы:", result_paths)

        except SystemExit:
            raise
        except Exception as e:
            traceback.print_exc()
            sys.exit(1)

        return  # завершаем после CLI

    # ---------- GUI-режим ----------
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
