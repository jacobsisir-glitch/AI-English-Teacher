from __future__ import annotations

import time
import importlib.util
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime

from voice.audio_buffer import PCMWindowBuffer, PreSpeechBuffer


@dataclass
class VADAction:
    kind: str
    pcm: bytes = b""
    event_ts: float | None = None


class SileroOnnxModel:
    def __init__(self, model_path: Path, *, sample_rate: int = 16000) -> None:
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        providers = ["CPUExecutionProvider"] if "CPUExecutionProvider" in onnxruntime.get_available_providers() else None
        self.session = onnxruntime.InferenceSession(model_path.as_posix(), providers=providers, sess_options=options)
        self.sample_rate = sample_rate
        self.window_size = 512 if sample_rate == 16000 else 256
        self.context_size = 64 if sample_rate == 16000 else 32
        self.reset_states()

    def reset_states(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.context_size), dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        if window.ndim == 1:
            window = window[np.newaxis, :]
        if window.shape[-1] != self.window_size:
            raise ValueError(
                f"Silero VAD expects {self.window_size} samples per window, got {window.shape[-1]}."
            )

        x = np.concatenate([self._context, window.astype(np.float32)], axis=1)
        out, state = self.session.run(
            None,
            {
                "input": x,
                "state": self._state,
                "sr": np.array(self.sample_rate, dtype=np.int64),
            },
        )
        self._state = state.astype(np.float32)
        self._context = x[:, -self.context_size :].astype(np.float32)
        return float(out.squeeze())


def resolve_default_silero_model_path() -> Path:
    spec = importlib.util.find_spec("silero_vad")
    if spec is None or not spec.origin:
        raise FileNotFoundError("silero_vad package is not installed, cannot locate ONNX model.")
    package_dir = Path(spec.origin).resolve().parent
    candidate = package_dir / "data" / "silero_vad_16k_op15.onnx"
    if not candidate.exists():
        raise FileNotFoundError(f"Silero VAD model not found at {candidate}")
    return candidate


class SileroVADController:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence_duration_ms: int = 650,
        min_speech_duration_ms: int = 180,
        pre_speech_ms: int = 250,
        model_path: Path | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.neg_threshold = threshold - 0.15
        self.model = SileroOnnxModel(model_path or resolve_default_silero_model_path(), sample_rate=sample_rate)
        self.window_size = self.model.window_size
        self.window_buffer = PCMWindowBuffer(window_samples=self.window_size)
        self.prebuffer = PreSpeechBuffer(max_bytes=int((pre_speech_ms / 1000) * sample_rate * 2))
        self.min_silence_samples = int(sample_rate * min_silence_duration_ms / 1000)
        self.min_speech_samples = int(sample_rate * min_speech_duration_ms / 1000)
        self.pending_silence_samples = 0
        self.current_speech_samples = 0
        self.active = False

    def reset(self) -> None:
        self.model.reset_states()
        self.window_buffer.flush()
        self.prebuffer.clear()
        self.pending_silence_samples = 0
        self.current_speech_samples = 0
        self.active = False

    def feed(self, pcm: bytes) -> list[VADAction]:
        actions: list[VADAction] = []
        for window_bytes in self.window_buffer.push(pcm):
            window = np.frombuffer(window_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            speech_prob = self.model(window)
            now = time.time()

            if not self.active:
                if speech_prob >= self.threshold:
                    self.active = True
                    self.pending_silence_samples = 0
                    self.current_speech_samples = len(window_bytes) // 2
                    initial_pcm = self.prebuffer.consume() + window_bytes
                    actions.append(VADAction(kind="speech_start", pcm=initial_pcm, event_ts=now))
                else:
                    self.prebuffer.append(window_bytes)
                continue

            actions.append(VADAction(kind="speech_chunk", pcm=window_bytes, event_ts=now))
            self.current_speech_samples += self.window_size
            if speech_prob < self.neg_threshold:
                self.pending_silence_samples += self.window_size
                if (
                    self.pending_silence_samples >= self.min_silence_samples
                    and self.current_speech_samples >= self.min_speech_samples
                ):
                    self.active = False
                    self.pending_silence_samples = 0
                    self.current_speech_samples = 0
                    self.prebuffer.clear()
                    actions.append(VADAction(kind="speech_end", event_ts=now))
            else:
                self.pending_silence_samples = 0
        return actions

    def flush(self) -> list[VADAction]:
        flushed: list[VADAction] = []
        remaining = self.window_buffer.flush()
        if remaining and self.active:
            flushed.append(VADAction(kind="speech_chunk", pcm=remaining, event_ts=time.time()))
        if self.active:
            self.active = False
            self.pending_silence_samples = 0
            self.current_speech_samples = 0
            flushed.append(VADAction(kind="speech_end", event_ts=time.time()))
        self.prebuffer.clear()
        self.model.reset_states()
        return flushed
