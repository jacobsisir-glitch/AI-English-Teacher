from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass(frozen=True)
class SynthesisResult:
    audio: bytes
    media_type: str


class SpeechProvider(Protocol):
    name: str

    async def list_voices(self) -> dict:
        """Return provider voices in the shape consumed by /api/tts/voices."""

    async def synthesize_speech(
        self,
        *,
        text: str,
        voice: str | None,
        lang: str | None,
        speed: float | None,
    ) -> SynthesisResult:
        """Return a complete synthesized audio file."""

    async def stream_speech(
        self,
        *,
        text: str,
        voice: str | None,
        lang: str | None,
        speed: float | None,
    ) -> AsyncIterator[bytes]:
        """Yield encoded audio chunks for streaming playback."""
