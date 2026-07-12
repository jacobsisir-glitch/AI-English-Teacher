from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import qwen_voice_enroll


def write_wav(path: Path, *, seconds: float, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> None:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames * channels)


def expect_validation_error(fn, expected: str) -> None:
    try:
        fn()
    except qwen_voice_enroll.ValidationError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected ValidationError containing {expected!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        valid = tmpdir / "lumina.wav"
        write_wav(valid, seconds=12)
        probe = qwen_voice_enroll.probe_audio(valid)
        assert probe.duration_seconds and 11.9 < probe.duration_seconds < 12.1
        assert probe.sample_rate == 24000
        assert probe.channels == 1
        assert probe.sample_width_bits == 16

        stereo = tmpdir / "stereo.wav"
        write_wav(stereo, seconds=12, channels=2)
        expect_validation_error(lambda: qwen_voice_enroll.probe_audio(stereo), "mono")

        low_rate = tmpdir / "low_rate.wav"
        write_wav(low_rate, seconds=12, sample_rate=16000)
        expect_validation_error(lambda: qwen_voice_enroll.probe_audio(low_rate), "24kHz")

        too_long = tmpdir / "too_long.wav"
        write_wav(too_long, seconds=31)
        expect_validation_error(lambda: qwen_voice_enroll.probe_audio(too_long), "30 seconds")

        not_audio = tmpdir / "voice.txt"
        not_audio.write_text("not audio", encoding="utf-8")
        expect_validation_error(lambda: qwen_voice_enroll.probe_audio(not_audio), "WAV, MP3, or M4A")

        old_argv = sys.argv[:]
        sys.argv = [
            "qwen_voice_enroll.py",
            "--audio",
            str(valid),
            "--preferred-name",
            "lumina",
            "--target-model",
            "qwen3.5-omni-flash-realtime",
            "--execute",
        ]
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                exit_code = qwen_voice_enroll.main()
        finally:
            sys.argv = old_argv
        assert exit_code == 2
        assert "--confirm-rights" in stderr.getvalue()

        old_argv = sys.argv[:]
        sys.argv = [
            "qwen_voice_enroll.py",
            "--audio",
            str(valid),
            "--preferred-name",
            "lumina",
            "--confirm-rights",
        ]
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                exit_code = qwen_voice_enroll.main()
        finally:
            sys.argv = old_argv
        output = stdout.getvalue()
        assert exit_code == 0
        assert "dry-run OK" in output
        assert "base64" not in output.lower()
        assert "No request was sent" in output

    print("Qwen voice enrollment offline tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
