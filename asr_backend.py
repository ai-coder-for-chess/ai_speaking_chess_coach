# asr_backend.py
from typing import Iterable, Optional, Tuple
from faster_whisper import WhisperModel
import os
import uuid

_MODEL: Optional[WhisperModel] = None

def init_asr(model_size: str = "small", device: str = "cpu", compute_type: str = "int8") -> WhisperModel:
    """
    Инициализация модели один раз на всё приложение.
    model_size: "tiny"|"base"|"small"|"medium"|"large-v3"
    device: "cpu" или "cuda"
    compute_type: для CPU обычно "int8"
    """
    global _MODEL
    if _MODEL is None:
        # Можно указать свою папку для скачивания моделей:
        # download_root = os.path.join(os.getcwd(), "models_whisper")
        _MODEL = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _MODEL

def transcribe_wav(path: str, langs: Iterable[Optional[str]] = ("ru", "en", "no", None)) -> str:
    """
    Распознаёт речь из WAV-файла. langs: перебор языков, None = автодетект.
    """
    assert _MODEL is not None, "ASR model is not initialized. Call init_asr() first."
    for lang in langs:
        segments, _ = _MODEL.transcribe(
            path,
            language=lang,      # None = autodetect
            vad_filter=True     # отсекает тишину/шумы
        )
        txt = " ".join(s.text for s in segments).strip()
        if txt:
            return txt.lower()
    return ""

def transcribe_bytes(wav_bytes: bytes, langs: Iterable[Optional[str]] = ("ru", "en", "no", None)) -> str:
    """
    Если у тебя поток байтов из микрофона/файла — сохраняем временно и распознаём.
    """
    tmp_name = f"tmp_cmd_{uuid.uuid4().hex}.wav"
    with open(tmp_name, "wb") as f:
        f.write(wav_bytes)
    try:
        return transcribe_wav(tmp_name, langs)
    finally:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
