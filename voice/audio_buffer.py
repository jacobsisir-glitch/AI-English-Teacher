from __future__ import annotations

from collections import deque

import numpy as np
from livekit import rtc


class PCMWindowBuffer:
    def __init__(self, *, window_samples: int = 512, bytes_per_sample: int = 2) -> None:
        self.window_bytes = window_samples * bytes_per_sample
        self._buffer = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        if pcm:
            self._buffer.extend(pcm)

        windows: list[bytes] = []
        while len(self._buffer) >= self.window_bytes:
            windows.append(bytes(self._buffer[: self.window_bytes]))
            del self._buffer[: self.window_bytes]
        return windows

    def flush(self) -> bytes:
        remaining = bytes(self._buffer)
        self._buffer.clear()
        return remaining


class PreSpeechBuffer:
    def __init__(self, *, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._segments: deque[bytes] = deque()
        self._size = 0

    def append(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._segments.append(bytes(pcm))
        self._size += len(pcm)
        while self._size > self.max_bytes and self._segments:
            removed = self._segments.popleft()
            self._size -= len(removed)

    def consume(self) -> bytes:
        joined = b"".join(self._segments)
        self.clear()
        return joined

    def clear(self) -> None:
        self._segments.clear()
        self._size = 0


class LiveKitAudioNormalizer:
    def __init__(self, *, target_sample_rate: int = 16000, target_channels: int = 1) -> None:
        self.target_sample_rate = target_sample_rate
        self.target_channels = target_channels
        self._resamplers: dict[tuple[int, int], rtc.AudioResampler] = {}

    def transform(self, frame: rtc.AudioFrame) -> list[bytes]:
        working_frame = self._ensure_mono(frame)
        if working_frame.sample_rate == self.target_sample_rate:
            return [self._frame_to_bytes(working_frame)]

        resampler = self._get_resampler(working_frame.sample_rate, working_frame.num_channels)
        resampled_frames = resampler.push(working_frame)
        return [self._frame_to_bytes(item) for item in resampled_frames if item.samples_per_channel > 0]

    def flush(self) -> list[bytes]:
        flushed: list[bytes] = []
        for resampler in self._resamplers.values():
            flushed.extend(self._frame_to_bytes(item) for item in resampler.flush() if item.samples_per_channel > 0)
        return flushed

    def _get_resampler(self, input_rate: int, input_channels: int) -> rtc.AudioResampler:
        key = (input_rate, input_channels)
        if key not in self._resamplers:
            self._resamplers[key] = rtc.AudioResampler(
                input_rate=input_rate,
                output_rate=self.target_sample_rate,
                num_channels=input_channels,
            )
        return self._resamplers[key]

    def _ensure_mono(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if frame.num_channels == self.target_channels:
            return frame

        pcm = np.frombuffer(frame.data, dtype=np.int16)
        reshaped = pcm.reshape(-1, frame.num_channels)
        mono = reshaped.mean(axis=1).astype(np.int16)
        return rtc.AudioFrame(
            mono.tobytes(),
            sample_rate=frame.sample_rate,
            num_channels=1,
            samples_per_channel=mono.shape[0],
        )

    @staticmethod
    def _frame_to_bytes(frame: rtc.AudioFrame) -> bytes:
        return np.frombuffer(frame.data, dtype=np.int16).astype(np.int16, copy=False).tobytes()
