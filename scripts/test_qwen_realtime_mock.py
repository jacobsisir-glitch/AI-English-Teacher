from __future__ import annotations

import asyncio
import base64
import json
import sys
from contextlib import contextmanager
from collections import deque
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from voice.audio_buffer import PCMWindowBuffer
from voice.providers.qwen_omni_realtime import (
    LOG_RESERVED_FIELDS,
    QwenOmniRealtimeSession,
    QwenRealtimeConfig,
    QwenRealtimeConfigError,
    build_lumina_realtime_instructions,
)
import voice.providers.qwen_omni_realtime as qwen_module
import voice.qwen_room_processor as qwen_room_module


class MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(json.dumps({"type": "response.done", "response_id": "closed"}))

    async def emit(self, payload: dict[str, Any]) -> None:
        await self.incoming.put(json.dumps(payload))


class FakeAudioSource:
    def __init__(self) -> None:
        self.frames: list[Any] = []
        self.clear_count = 0

    def capture_frame(self, frame: Any) -> None:
        self.frames.append(frame)

    def clear_queue(self) -> None:
        self.clear_count += 1


def make_audio_processor() -> Any:
    processor = qwen_room_module.QwenParticipantVoiceProcessor.__new__(qwen_room_module.QwenParticipantVoiceProcessor)
    processor._stopped = False
    processor._audio_source = FakeAudioSource()
    processor._audio_buffer = PCMWindowBuffer(window_samples=480)
    processor._audio_queue = asyncio.Queue(maxsize=256)
    processor._audio_publish_task = None
    processor._current_audio_response_id = ""
    processor._published_track_sid = "TR_QWEN_TEST"
    processor._frame_count_by_response = {}
    processor._first_audio_enqueued_at = {}
    processor._first_audio_frame_published_at = {}
    processor._cancelled_audio_response_ids = set()
    processor.room_id_getter = lambda: "room-id"
    processor.room_name = "room-name"
    processor.user_identity = "student-1"
    return processor


@contextmanager
def patched_logger(replacement):
    original = qwen_module.structured_voice_log
    qwen_module.structured_voice_log = replacement
    try:
        yield
    finally:
        qwen_module.structured_voice_log = original


async def main() -> int:
    try:
        QwenRealtimeConfig(api_key="", workspace_id="workspace").validate()
    except QwenRealtimeConfigError as exc:
        assert "QWEN_REALTIME_API_KEY" in str(exc)
    else:
        raise AssertionError("missing API key must raise QwenRealtimeConfigError")

    try:
        QwenRealtimeConfig(api_key="test-key", workspace_id="", base_url="").endpoint()
    except QwenRealtimeConfigError as exc:
        assert "QWEN_REALTIME_WORKSPACE_ID" in str(exc)
    else:
        raise AssertionError("missing workspace/base URL must raise QwenRealtimeConfigError")

    try:
        QwenRealtimeConfig(api_key="test-key", workspace_id="workspace", voice="").validate()
    except QwenRealtimeConfigError as exc:
        assert "QWEN_REALTIME_VOICE" in str(exc)
    else:
        raise AssertionError("blank QWEN_REALTIME_VOICE must raise QwenRealtimeConfigError")

    ws = MockWebSocket()
    topics: deque[tuple[str, dict[str, Any]]] = deque()
    audio_chunks: list[tuple[bytes, str]] = []

    async def factory(url: str, headers: dict[str, str]) -> MockWebSocket:
        assert "qwen3.5-omni-flash-realtime" in url
        assert headers["Authorization"] == "Bearer test-key"
        return ws

    async def on_topic(topic: str, payload: dict[str, Any]) -> None:
        topics.append((topic, payload))

    async def on_audio(audio: bytes, response_id: str) -> None:
        audio_chunks.append((audio, response_id))

    session = QwenOmniRealtimeSession(
        room_id="room-id",
        room_name="room-name",
        participant_id="student-1",
        instructions="You are Lumina.",
        on_topic=on_topic,
        on_audio=on_audio,
        websocket_factory=factory,
        config=QwenRealtimeConfig(
            api_key="test-key",
            workspace_id="workspace",
            model="qwen3.5-omni-flash-realtime",
            voice="custom_lumina_voice_001",
            vad_mode="manual",
            input_sample_rate=16000,
            output_sample_rate=24000,
        ),
    )
    assert {"event", "room_id", "room_name", "user_identity", "session_id"} <= LOG_RESERVED_FIELDS

    captured_logs: list[tuple[str, dict[str, Any]]] = []

    def capture_log(event: str, **payload: Any) -> None:
        captured_logs.append((event, payload))

    with patched_logger(capture_log):
        session._log("qwen.test.no_session")
        session._log("qwen.test.same_session", session_id=session.session_id)
        session._log("qwen.test.other_session", session_id="server-session-123", room_id="payload-room")

    assert any(event == "qwen.test.no_session" and payload["session_id"] == session.session_id for event, payload in captured_logs)
    assert any(
        event == "qwen.test.same_session" and payload["session_id"] == session.session_id and payload["server_session_id"] == session.session_id
        for event, payload in captured_logs
    )
    assert any(
        event == "qwen.test.other_session" and payload["server_session_id"] == "server-session-123" and payload["payload_room_id"] == "payload-room"
        for event, payload in captured_logs
    )

    def broken_log(event: str, **payload: Any) -> None:
        raise TypeError("logger broke")

    with patched_logger(broken_log):
        session._log("qwen.test.logger_failure", session_id="server-session-456")

    await session.connect()
    assert ws.sent[0]["type"] == "session.update"
    assert ws.sent[0]["session"]["modalities"] == ["text", "audio"]
    assert ws.sent[0]["session"]["voice"] == "custom_lumina_voice_001"
    assert "turn_detection" not in ws.sent[0]["session"]
    instructions = build_lumina_realtime_instructions(class_mode=False)
    assert "Lumina" in instructions
    assert "65%" in instructions
    assert "grammar" in instructions.lower() or "语法" in instructions
    assert "Your spoken response must contain dialogue only." in instructions
    assert "每次回复至少包含一个动作标签" not in instructions
    assert "只允许使用以下标签" not in instructions

    await session.append_audio(b"\x01\x02" * 160)
    assert ws.sent[-1]["type"] == "input_audio_buffer.append"
    assert base64.b64decode(ws.sent[-1]["audio"]) == b"\x01\x02" * 160

    await session.manual_commit()
    assert [item["type"] for item in ws.sent[-2:]] == ["input_audio_buffer.commit", "response.create"]

    await session.cancel_response(reason="no_active_response")
    assert ws.sent[-1]["type"] == "response.create"
    assert not any(topic == "qwen.response.cancelled" for topic, _payload in topics)

    await ws.emit({"type": "response.created", "response": {"id": "resp-1"}})
    await ws.emit({"type": "session.updated", "session": {"id": "server-session-real"}, "session_id": "payload-session-real"})
    await ws.emit({"type": "response.audio_transcript.delta", "response_id": "resp-1", "delta": "Good."})
    await ws.emit({"type": "response.audio_transcript.delta", "response_id": "resp-1", "delta": " Try again."})
    await ws.emit({"type": "response.audio.delta", "response_id": "resp-1", "delta": base64.b64encode(b"audio1").decode()})
    await asyncio.sleep(0.05)
    assert any(topic == "qwen.response.started" for topic, _payload in topics)
    assert any(topic == "qwen.output_text.delta" and payload["delta"] == "Good." for topic, payload in topics)
    assert any(topic == "qwen.output_text.delta" and payload["delta"] == " Try again." for topic, payload in topics)
    assert audio_chunks == [(b"audio1", "resp-1")]

    await session.cancel_response(reason="test_cancel")
    assert ws.sent[-1]["type"] == "response.cancel"

    await ws.emit({"type": "response.audio.delta", "response_id": "resp-1", "delta": base64.b64encode(b"late").decode()})
    await asyncio.sleep(0.05)
    assert audio_chunks == [(b"audio1", "resp-1")]

    before_final_count = len([topic for topic, _payload in topics if topic == "qwen.input_transcript.final"])
    await ws.emit({"type": "conversation.item.input_audio_transcription.completed", "text": "wrong-field"})
    await asyncio.sleep(0.05)
    after_wrong_field_count = len([topic for topic, _payload in topics if topic == "qwen.input_transcript.final"])
    assert after_wrong_field_count == before_final_count

    await ws.emit({"type": "conversation.item.input_audio_transcription.delta", "text": "", "stash": "partial words"})
    await asyncio.sleep(0.05)
    assert any(
        topic == "qwen.input_transcript.partial" and payload["text"] == "partial words"
        for topic, payload in topics
    )

    await ws.emit({"type": "conversation.item.input_audio_transcription.completed", "transcript": "I goed home."})
    await asyncio.sleep(0.05)
    assert any(
        topic == "qwen.input_transcript.final" and payload["text"] == "I goed home."
        for topic, payload in topics
    )

    await ws.emit({"type": "session.created", "session": {"id": "server-session-created"}})
    await ws.emit({"type": "session.updated", "session": {"id": "server-session-updated"}})
    await ws.emit({"type": "input_audio_buffer.speech_started", "item_id": "utt-real"})
    await ws.emit({"type": "conversation.item.input_audio_transcription.completed", "transcript": "七彩。"})
    await ws.emit({"type": "response.created", "response": {"id": "resp-real"}})
    await ws.emit({"type": "response.audio_transcript.delta", "response_id": "resp-real", "delta": "Hello."})
    await ws.emit({"type": "response.audio.delta", "response_id": "resp-real", "delta": base64.b64encode(b"pcm-real").decode()})
    await ws.emit({"type": "response.audio.done", "response_id": "resp-real"})
    await ws.emit({"type": "response.done", "response_id": "resp-real"})
    await asyncio.sleep(0.05)
    assert any(topic == "qwen.input_transcript.final" and payload["text"] == "七彩。" for topic, payload in topics)
    assert (b"pcm-real", "resp-real") in audio_chunks
    assert session.audio_bytes_by_response["resp-real"] == len(b"pcm-real")

    await ws.emit({"type": "response.created", "response": {"id": "resp-2"}})
    await ws.emit({"type": "response.audio_transcript.delta", "response_id": "resp-2", "delta": "Hello"})
    await ws.emit({"type": "response.audio_transcript.delta", "response_id": "resp-2", "delta": ", Lumina."})
    await ws.emit({"type": "response.audio_transcript.done", "response_id": "resp-2", "transcript": ""})
    await asyncio.sleep(0.05)
    assert any(
        topic == "qwen.output_text.done" and payload["text"] == "Hello, Lumina."
        for topic, payload in topics
    )

    window = PCMWindowBuffer(window_samples=480)
    assert window.push(b"\x00" * 100) == []
    frames = window.push(b"\x01" * 900)
    assert len(frames) == 1
    assert len(frames[0]) == 960
    assert len(window.flush()) == 40

    await session.close()
    assert ws.closed

    ws2 = MockWebSocket()
    topics2: deque[tuple[str, dict[str, Any]]] = deque()
    audio2: list[tuple[bytes, str]] = []

    async def factory2(url: str, headers: dict[str, str]) -> MockWebSocket:
        return ws2

    async def on_topic2(topic: str, payload: dict[str, Any]) -> None:
        topics2.append((topic, payload))

    async def on_audio2(audio: bytes, response_id: str) -> None:
        audio2.append((audio, response_id))

    session2 = QwenOmniRealtimeSession(
        room_id="room-id",
        room_name="room-name",
        participant_id="student-2",
        instructions="You are Lumina.",
        on_topic=on_topic2,
        on_audio=on_audio2,
        websocket_factory=factory2,
        config=QwenRealtimeConfig(api_key="test-key", workspace_id="workspace", vad_mode="manual"),
    )
    await session2.connect()

    call_count = 0

    def flaky_log(event: str, **payload: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise TypeError("simulated logging failure")

    with patched_logger(flaky_log):
        await ws2.emit({"type": "response.created", "response": {"id": "resp-log"}})
        await ws2.emit({"type": "response.audio.delta", "response_id": "resp-log", "delta": base64.b64encode(b"pcm").decode()})
        await asyncio.sleep(0.05)

    assert audio2 == [(b"pcm", "resp-log")]
    assert session2._recv_task is not None and not session2._recv_task.done()
    await session2.close()

    processor = make_audio_processor()
    original_buffer_ms = qwen_room_module.QWEN_AUDIO_PLAYBACK_BUFFER_MS
    qwen_room_module.QWEN_AUDIO_PLAYBACK_BUFFER_MS = 0
    try:
        await processor._publish_qwen_audio(b"\x01\x02" * 480, "resp-audio")
        await asyncio.sleep(0.05)
        await processor._publish_qwen_audio(b"", "resp-audio")
        await asyncio.sleep(0.05)
        assert len(processor._audio_source.frames) >= 1
        assert processor._frame_count_by_response["resp-audio"] >= 1
        await processor._stop_audio_publisher()
    finally:
        qwen_room_module.QWEN_AUDIO_PLAYBACK_BUFFER_MS = original_buffer_ms

    processor = make_audio_processor()
    await processor._publish_qwen_audio(b"\x03\x04" * 480, "resp-buffered")
    await asyncio.sleep(0.05)
    assert len(processor._audio_source.frames) == 0
    await asyncio.sleep((qwen_room_module.QWEN_AUDIO_PLAYBACK_BUFFER_MS / 1000) + 0.1)
    assert len(processor._audio_source.frames) >= 1
    await processor._stop_audio_publisher()

    processor = make_audio_processor()
    processor._cancelled_audio_response_ids.add("resp-cancelled")
    await processor._publish_qwen_audio(b"\x05\x06" * 480, "resp-cancelled")
    await asyncio.sleep(0.05)
    assert len(processor._audio_source.frames) == 0
    assert processor._audio_queue.maxsize == 256
    await processor._stop_audio_publisher()

    frontend_html = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "subtitle-scroll" in frontend_html
    subtitle_block = frontend_html.split('<div ref="subtitleScrollRef"', 1)[1].split("</transition-group>", 1)[0]
    assert "truncate" not in subtitle_block
    assert "line-clamp" not in subtitle_block
    assert ".slice(-3)" in frontend_html

    print("Qwen realtime mock tests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
