from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import numpy as np
from livekit import rtc

ROOT_DIR = Path(__file__).resolve().parent
if ROOT_DIR.name == "scripts":
    ROOT_DIR = ROOT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from voice.audio_buffer import LiveKitAudioNormalizer
from voice.vad_controller import SileroVADController, resolve_default_silero_model_path


def build_test_frame(sample_rate: int = 48000, seconds: float = 0.1) -> rtc.AudioFrame:
    samples = int(sample_rate * seconds)
    waveform = (
        0.2 * np.sin(2 * math.pi * 440 * np.arange(samples) / sample_rate) * np.iinfo(np.int16).max
    ).astype(np.int16)
    return rtc.AudioFrame(
        waveform.tobytes(),
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=samples,
    )


async def main() -> None:
    model_path = resolve_default_silero_model_path()
    print(f"Silero model: {model_path}")

    normalizer = LiveKitAudioNormalizer(target_sample_rate=16000, target_channels=1)
    frame = build_test_frame()
    normalized_chunks = normalizer.transform(frame)
    if not normalized_chunks:
        normalized_chunks = normalizer.flush()
    print(f"Normalized chunks: {len(normalized_chunks)}")
    print(f"First chunk bytes: {len(normalized_chunks[0]) if normalized_chunks else 0}")

    vad = SileroVADController(sample_rate=16000, model_path=Path(model_path))
    silence = (np.zeros(16000 // 2, dtype=np.int16)).tobytes()
    actions = vad.feed(silence)
    print(f"Silence actions: {[action.kind for action in actions]}")
    print("Dry-run OK")


if __name__ == "__main__":
    asyncio.run(main())
