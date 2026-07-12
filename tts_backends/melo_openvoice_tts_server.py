import importlib.machinery
import json
import logging
import re
import sys
import time
import types
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import soundfile as sf
import torch
import torchaudio
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


HOST = "127.0.0.1"
PORT = 8012
SAMPLE_RATE = 24000

BASE_DIR = Path(__file__).resolve().parent.parent
EXTERNAL_BASE = Path(r"D:\AI_English_teacher_tools\Melo_OpenVoice_TTS")
MELO_SOURCE_DIR = EXTERNAL_BASE / "MeloTTS"
OPENVOICE_SOURCE_DIR = EXTERNAL_BASE / "OpenVoice"
MELO_MODELS_DIR = EXTERNAL_BASE / "models"
OPENVOICE_CHECKPOINTS_DIR = OPENVOICE_SOURCE_DIR / "checkpoints_v2"
OUTPUT_DIR = BASE_DIR / "temp_audio_melo"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
TEACHER_TARGET_EMBEDDING = OPENVOICE_CHECKPOINTS_DIR / "base_speakers" / "ses" / "en-default.pth"
ZH_TARGET_EMBEDDING = OPENVOICE_CHECKPOINTS_DIR / "base_speakers" / "ses" / "zh.pth"
APPLY_OPENVOICE_FOR_ZH = False


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("melo_openvoice_tts")


def log_event(event: str, **payload: Any) -> None:
    logger.info(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str))


def install_runtime_compatibility_shims() -> None:
    import unidic_lite

    fake_unidic = types.ModuleType("unidic")
    fake_unidic.DICDIR = unidic_lite.DICDIR
    sys.modules["unidic"] = fake_unidic

    fake_wavmark = types.ModuleType("wavmark")

    class DummyWavmarkModel:
        def to(self, *_args: Any, **_kwargs: Any):
            return self

        def encode(self, signal, _message_tensor):
            return signal

        def decode(self, signal):
            shape = (signal.shape[0], 32)
            return torch.zeros(shape, dtype=torch.float32, device=signal.device)

    def _load_model():
        return DummyWavmarkModel()

    fake_wavmark.load_model = _load_model
    sys.modules["wavmark"] = fake_wavmark

    fake_librosa = types.ModuleType("librosa")
    fake_librosa.__spec__ = importlib.machinery.ModuleSpec("librosa", loader=None)
    fake_librosa_filters = types.ModuleType("librosa.filters")
    fake_librosa_filters.__spec__ = importlib.machinery.ModuleSpec("librosa.filters", loader=None)
    fake_librosa_util = types.ModuleType("librosa.util")
    fake_librosa_util.__spec__ = importlib.machinery.ModuleSpec("librosa.util", loader=None)

    def _load(path: str, sr: int | None = None, mono: bool = True):
        audio, sample_rate = sf.read(path, always_2d=False)
        if isinstance(audio, np.ndarray) and audio.ndim > 1 and mono:
            audio = audio.mean(axis=1)
        if sr is not None and sample_rate != sr:
            tensor = torch.as_tensor(audio, dtype=torch.float32)
            if tensor.ndim == 1:
                tensor = tensor.unsqueeze(0)
            else:
                tensor = tensor.transpose(0, 1)
            resampled = torchaudio.functional.resample(tensor, sample_rate, sr)
            audio = (
                resampled.squeeze(0).cpu().numpy()
                if resampled.shape[0] == 1
                else resampled.transpose(0, 1).cpu().numpy()
            )
            sample_rate = sr
        return np.asarray(audio, dtype=np.float32), sample_rate

    def _pad_center(data: np.ndarray, size: int):
        data = np.asarray(data)
        if data.shape[-1] >= size:
            start = (data.shape[-1] - size) // 2
            return data[..., start : start + size]
        pad_total = size - data.shape[-1]
        pad_left = pad_total // 2
        pad_right = pad_total - pad_left
        return np.pad(data, (pad_left, pad_right), mode="constant")

    def _mel(sr: int, n_fft: int, n_mels: int = 128, fmin: float = 0.0, fmax: float | None = None, **_: Any):
        fb = torchaudio.functional.melscale_fbanks(
            n_freqs=n_fft // 2 + 1,
            f_min=float(fmin),
            f_max=float(sr / 2 if fmax is None else fmax),
            n_mels=int(n_mels),
            sample_rate=int(sr),
            norm="slaney",
            mel_scale="slaney",
        )
        return fb.transpose(0, 1).cpu().numpy().astype(np.float32)

    fake_librosa.load = _load
    fake_librosa.filters = fake_librosa_filters
    fake_librosa.util = fake_librosa_util
    fake_librosa_filters.mel = _mel
    fake_librosa_util.pad_center = _pad_center

    sys.modules["librosa"] = fake_librosa
    sys.modules["librosa.filters"] = fake_librosa_filters
    sys.modules["librosa.util"] = fake_librosa_util

    import transformers

    real_from_pretrained = transformers.AutoTokenizer.from_pretrained

    class FallbackTokenizer:
        def tokenize(self, text: str):
            return re.findall(r"[A-Za-z]+|[^A-Za-z\s]", text)

    def safe_from_pretrained(*args: Any, **kwargs: Any):
        kwargs.setdefault("local_files_only", True)
        try:
            return real_from_pretrained(*args, **kwargs)
        except Exception as exc:
            log_event(
                "melo.tokenizer.fallback",
                model_id=args[0] if args else None,
                exception_repr=repr(exc),
            )
            return FallbackTokenizer()

    transformers.AutoTokenizer.from_pretrained = safe_from_pretrained


install_runtime_compatibility_shims()
sys.path.insert(0, str(MELO_SOURCE_DIR))
sys.path.insert(0, str(OPENVOICE_SOURCE_DIR))

from melo.api import TTS as MeloTTS  # noqa: E402
from openvoice.api import ToneColorConverter  # noqa: E402


VOICE_SPECS = {
    "melo_teacher": {
        "id": "melo_teacher",
        "label": "Melo/OpenVoice Teacher",
        "supported_langs": ["zh", "en", "zh_mix_en"],
        "backend_name": "melo_openvoice",
    }
}


class SpeakRequest(BaseModel):
    text: str = Field(..., description="Text to synthesize")
    voice: str = Field(default="melo_teacher")
    lang: str = Field(default="zh")
    speed: float = Field(default=1.0, gt=0.0)


class BackendState:
    def __init__(self) -> None:
        self.zh_tts = None
        self.en_tts = None
        self.converter = None
        self.target_se = None
        self.source_embeddings: dict[str, torch.Tensor] = {}
        self.mode = "fallback"


backend_state = BackendState()
app = FastAPI(title="Melo/OpenVoice TTS Service")


def build_melo_tts(language: str, model_dir: Path):
    log_event(
        "tts.backend.select",
        backend_name="melo",
        lang=language,
        model_dir=str(model_dir),
        device=DEVICE,
    )
    tts = MeloTTS(
        language=language,
        device=DEVICE,
        config_path=str(model_dir / "config.json"),
        ckpt_path=str(model_dir / "checkpoint.pth"),
    )
    if hasattr(tts.hps, "data"):
        setattr(tts.hps.data, "disable_bert", True)
    return tts


def init_openvoice_converter() -> tuple[Any, dict[str, torch.Tensor], torch.Tensor]:
    converter = ToneColorConverter(
        str(OPENVOICE_CHECKPOINTS_DIR / "converter" / "config.json"),
        device=DEVICE,
    )
    converter.load_ckpt(str(OPENVOICE_CHECKPOINTS_DIR / "converter" / "checkpoint.pth"))
    converter.watermark_model = None

    ses_dir = OPENVOICE_CHECKPOINTS_DIR / "base_speakers" / "ses"
    source_embeddings = {
        "zh": torch.load(str(ses_dir / "zh.pth"), map_location=DEVICE).to(DEVICE),
        "en": torch.load(str(ses_dir / "en-us.pth"), map_location=DEVICE).to(DEVICE),
    }
    target_se = torch.load(str(TEACHER_TARGET_EMBEDDING), map_location=DEVICE).to(DEVICE)
    return converter, source_embeddings, target_se


def ensure_backend_ready() -> None:
    if backend_state.zh_tts is not None and backend_state.en_tts is not None:
        return

    started = time.perf_counter()
    try:
        backend_state.zh_tts = build_melo_tts("ZH", MELO_MODELS_DIR / "MeloTTS-Chinese")
        backend_state.en_tts = build_melo_tts("EN", MELO_MODELS_DIR / "MeloTTS-English")
        backend_state.converter, backend_state.source_embeddings, backend_state.target_se = init_openvoice_converter()
        backend_state.mode = "partial_integration"
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log_event(
            "tts.backend.ready",
            backend_name="melo_openvoice",
            mode=backend_state.mode,
            elapsed_ms=elapsed_ms,
            target_embedding=str(TEACHER_TARGET_EMBEDDING),
        )
    except Exception as exc:
        backend_state.mode = "fallback"
        log_event(
            "tts.backend.error",
            backend_name="melo_openvoice",
            exception_repr=repr(exc),
        )
        raise


def pick_language(text: str, requested_lang: str) -> str:
    lowered = requested_lang.strip().lower()
    if lowered in {"zh", "cn", "z", "zh_mix_en"}:
        return "zh"
    if lowered in {"en", "a", "english"}:
        return "en"
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
    alpha_count = len(re.findall(r"[A-Za-z]", text))
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    if has_cjk and cjk_count >= max(3, alpha_count):
        return "zh"
    return "en"


def synthesize_with_melo(text: str, lang: str, speed: float, output_path: Path) -> tuple[bool, str, str]:
    ensure_backend_ready()
    tts = backend_state.zh_tts if lang == "zh" else backend_state.en_tts
    speaker_id = 0
    tts.tts_to_file(
        text=text,
        speaker_id=speaker_id,
        output_path=str(output_path),
        speed=speed,
        quiet=True,
    )
    if backend_state.converter is None:
        return False, "", "converter_unavailable"

    if lang == "zh" and not APPLY_OPENVOICE_FOR_ZH:
        return False, "", "skip_openvoice_for_zh"

    src_embedding = backend_state.source_embeddings["zh" if lang == "zh" else "en"]
    target_embedding = ZH_TARGET_EMBEDDING if lang == "zh" else TEACHER_TARGET_EMBEDDING
    converted_path = output_path.with_name(output_path.stem + "_ov.wav")
    backend_state.converter.convert(
        audio_src_path=str(output_path),
        src_se=src_embedding,
        tgt_se=torch.load(str(target_embedding), map_location=DEVICE).to(DEVICE),
        output_path=str(converted_path),
        message="",
    )
    converted_path.replace(output_path)
    return True, str(target_embedding), "openvoice_applied"


@app.get("/voices")
def list_voices() -> dict[str, Any]:
    ready = True
    error = None
    try:
        ensure_backend_ready()
    except Exception as exc:
        ready = False
        error = repr(exc)
    return {
        "backend_name": "melo_openvoice",
        "mode": backend_state.mode,
        "ready": ready,
        "error": error,
        "voices": list(VOICE_SPECS.values()),
    }


@app.post("/speak")
def speak(request: SpeakRequest):
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    if request.voice not in VOICE_SPECS:
        raise HTTPException(status_code=400, detail=f"unsupported voice: {request.voice}")

    lang = pick_language(text, request.lang)
    started = time.perf_counter()
    output_path = OUTPUT_DIR / f"melo_{lang}_{int(time.time() * 1000)}_{uuid4().hex[:8]}.wav"

    log_event(
        "tts.request.begin",
        backend_name="melo_openvoice",
        text_length=len(text),
        text_preview=text[:80],
        voice=request.voice,
        lang=lang,
        requested_lang=request.lang,
        speed=request.speed,
        output_path=str(output_path),
    )

    try:
        applied_openvoice, target_embedding, reason = synthesize_with_melo(text, lang, request.speed, output_path)
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log_event(
            "tts.request.error",
            backend_name="melo_openvoice",
            text_length=len(text),
            voice=request.voice,
            lang=lang,
            speed=request.speed,
            elapsed_ms=elapsed_ms,
            whether_openvoice_applied=False,
            fallback_reason="synthesis_failed",
            exception_repr=repr(exc),
        )
        raise HTTPException(status_code=500, detail=f"melo/openvoice synthesis failed: {exc}") from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log_event(
        "tts.request.success",
        backend_name="melo_openvoice",
        text_length=len(text),
        voice=request.voice,
        lang=lang,
        speed=request.speed,
        elapsed_ms=elapsed_ms,
        whether_openvoice_applied=applied_openvoice,
        target_embedding=target_embedding,
        reason_for_skip_or_apply=reason,
        output_path=str(output_path),
    )

    return FileResponse(
        path=str(output_path),
        media_type="audio/wav",
        filename=output_path.name,
    )


if __name__ == "__main__":
    print(f"Run with: uvicorn tts_backends.melo_openvoice_tts_server:app --host {HOST} --port {PORT} --reload")
