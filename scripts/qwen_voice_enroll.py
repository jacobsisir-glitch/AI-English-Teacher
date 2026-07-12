from __future__ import annotations

import argparse
import json
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import QWEN_REALTIME_API_KEY, QWEN_REALTIME_MODEL, QWEN_REALTIME_REGION


OFFICIAL_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"
ENROLLMENT_MODEL = "qwen-voice-enrollment"
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_DURATION_SECONDS = 30.0
RECOMMENDED_MIN_SECONDS = 10.0
RECOMMENDED_MAX_SECONDS = 20.0
SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a"}


@dataclass
class AudioProbe:
    path: Path
    suffix: str
    size_bytes: int
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    sample_width_bits: int | None = None
    warnings: list[str] | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "file": str(self.path),
            "format": self.suffix.lstrip("."),
            "size_bytes": self.size_bytes,
            "duration_seconds": round(self.duration_seconds, 3) if self.duration_seconds is not None else None,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width_bits": self.sample_width_bits,
            "warnings": self.warnings or [],
        }


class ValidationError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Qwen Omni custom voice enrollment request.")
    parser.add_argument("--audio", required=True, help="Path to WAV 16-bit, MP3, or M4A source audio.")
    parser.add_argument("--preferred-name", required=True, help="Preferred custom voice name, e.g. lumina.")
    parser.add_argument("--target-model", default=QWEN_REALTIME_MODEL, help="Target realtime model. Must match later usage.")
    parser.add_argument("--confirm-rights", action="store_true", help="Confirm you own or have rights to this voice.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Reserved for a future real enrollment call. This script currently refuses online creation.",
    )
    return parser.parse_args()


def probe_audio(path: Path) -> AudioProbe:
    if not path.exists() or not path.is_file():
        raise ValidationError(f"Audio file does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValidationError("Audio must be WAV, MP3, or M4A.")
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise ValidationError("Audio file is empty.")
    if size_bytes >= MAX_AUDIO_BYTES:
        raise ValidationError("Audio file must be smaller than 10MB.")

    if suffix == ".wav":
        probe = _probe_wav(path, size_bytes)
    else:
        probe = _probe_with_ffprobe(path, suffix, size_bytes)
    validate_audio_probe(probe)
    return probe


def _probe_wav(path: Path, size_bytes: int) -> AudioProbe:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        sample_width_bits = wav.getsampwidth() * 8
        frames = wav.getnframes()
        duration_seconds = frames / float(sample_rate) if sample_rate else 0.0
    return AudioProbe(
        path=path,
        suffix=".wav",
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bits=sample_width_bits,
        warnings=[],
    )


def _probe_with_ffprobe(path: Path, suffix: str, size_bytes: int) -> AudioProbe:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
    except FileNotFoundError as exc:
        raise ValidationError("MP3/M4A validation requires ffprobe on PATH.") from exc
    except subprocess.SubprocessError as exc:
        raise ValidationError(f"ffprobe failed to inspect audio: {type(exc).__name__}") from exc
    data = json.loads(result.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    duration = data.get("format", {}).get("duration")
    return AudioProbe(
        path=path,
        suffix=suffix,
        size_bytes=size_bytes,
        duration_seconds=float(duration) if duration else None,
        sample_rate=int(stream["sample_rate"]) if str(stream.get("sample_rate") or "").isdigit() else None,
        channels=int(stream["channels"]) if str(stream.get("channels") or "").isdigit() else None,
        warnings=[],
    )


def validate_audio_probe(probe: AudioProbe) -> None:
    warnings = probe.warnings if probe.warnings is not None else []
    if probe.duration_seconds is None:
        raise ValidationError("Could not determine audio duration.")
    if probe.duration_seconds <= 0:
        raise ValidationError("Audio duration must be greater than 0 seconds.")
    if probe.duration_seconds > MAX_DURATION_SECONDS:
        raise ValidationError("Audio duration must be 30 seconds or shorter.")
    if not (RECOMMENDED_MIN_SECONDS <= probe.duration_seconds <= RECOMMENDED_MAX_SECONDS):
        warnings.append("Recommended enrollment duration is 10-20 seconds.")
    if probe.sample_rate is None or probe.sample_rate < 24000:
        raise ValidationError("Audio sample rate should be at least 24kHz.")
    if probe.channels != 1:
        raise ValidationError("Audio should be mono.")
    if probe.suffix == ".wav" and probe.sample_width_bits != 16:
        raise ValidationError("WAV audio must be 16-bit.")
    warnings.append("Use clean speech only: no background music, noise, or other voices.")
    probe.warnings = warnings


def build_safe_request_preview(args: argparse.Namespace, probe: AudioProbe) -> dict[str, Any]:
    return {
        "endpoint": OFFICIAL_ENDPOINT,
        "model": ENROLLMENT_MODEL,
        "input": {
            "action": "create_voice",
            "target_model": args.target_model,
            "preferred_name": args.preferred_name,
            "audio_file": str(probe.path),
        },
        "parameters": {
            "region": QWEN_REALTIME_REGION,
        },
    }


def mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "<missing>"
    return f"{text[:2]}***{text[-2:]}" if len(text) > 4 else "***"


def main() -> int:
    args = parse_args()
    try:
        if QWEN_REALTIME_REGION != "beijing":
            raise ValidationError("QWEN_REALTIME_REGION must be beijing for the current project wiring.")
        if args.execute and not args.confirm_rights:
            raise ValidationError("Refusing real enrollment without --confirm-rights.")
        if args.execute:
            raise ValidationError("Real Qwen voice enrollment is intentionally disabled in this dry-run script.")
        probe = probe_audio(Path(args.audio).expanduser().resolve())
    except ValidationError as exc:
        print(f"validation_failed: {exc}", file=sys.stderr)
        return 2

    print("Qwen voice enrollment dry-run OK")
    print("Rights: only enroll a voice you own or have explicit permission to clone.")
    print("Never clone Doubao or other commercial preset voices.")
    print(f"API key: {mask_secret(QWEN_REALTIME_API_KEY)}")
    print(json.dumps({"audio": probe.to_safe_dict(), "request_preview": build_safe_request_preview(args, probe)}, ensure_ascii=False, indent=2))
    print("No request was sent. To use an enrolled voice later, set QWEN_REALTIME_VOICE=<voice ID> in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
