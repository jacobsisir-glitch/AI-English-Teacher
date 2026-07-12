from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    DOUBAO_AUTH_MODE,
    DOUBAO_TTS_RESOURCE_ID,
    DOUBAO_TTS_V3_ENDPOINT,
    DOUBAO_TTS_VOICE_TYPE,
)
from speech_providers.doubao_tts import DOUBAO_AUDIO_ENCODING, DoubaoTTSProvider  # noqa: E402
from speech_providers.errors import SpeechProviderError  # noqa: E402


TEST_TEXT = "你好，欢迎来到 AI Teacher。今天我们测试豆包语音。"
OUTPUT_PATH = PROJECT_ROOT / f"doubao_v3_test.{DOUBAO_AUDIO_ENCODING}"


async def main() -> int:
    provider = DoubaoTTSProvider()
    print(
        json.dumps(
            {
                "endpoint": DOUBAO_TTS_V3_ENDPOINT,
                "auth_mode": DOUBAO_AUTH_MODE,
                "resource_id": DOUBAO_TTS_RESOURCE_ID,
                "voice_type": DOUBAO_TTS_VOICE_TYPE,
                "text_length": len(TEST_TEXT),
                "output_path": str(OUTPUT_PATH),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    chunks: list[bytes] = []
    try:
        async for chunk in provider.stream_speech(
            text=TEST_TEXT,
            voice="doubao_default",
            lang="zh",
            speed=1.0,
        ):
            chunks.append(chunk)
            print(f"audio chunk: {len(chunk)} bytes")
    except SpeechProviderError as exc:
        print("Doubao V3 verification failed.")
        print(f"error: {exc}")
        print(f"status_code: {exc.status_code}")
        print(f"response_text: {exc.response_text}")
        print(f"exception_repr: {exc.exception_repr}")
        return 1
    except Exception as exc:
        print("Doubao V3 verification failed.")
        print(f"error: {exc}")
        print(f"exception_repr: {repr(exc)}")
        return 1

    audio = b"".join(chunks)
    if not audio:
        print("Doubao V3 verification failed: no audio bytes returned.")
        return 1

    OUTPUT_PATH.write_bytes(audio)
    print(f"saved: {OUTPUT_PATH}")
    print(f"total_bytes: {len(audio)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
