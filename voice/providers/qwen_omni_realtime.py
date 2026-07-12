from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4

import websockets
from websockets import ClientConnection

from config import (
    QWEN_REALTIME_API_KEY,
    QWEN_REALTIME_AUDIO_FORMAT,
    QWEN_REALTIME_BASE_URL,
    QWEN_REALTIME_INPUT_SAMPLE_RATE,
    QWEN_REALTIME_MAX_SESSION_MINUTES,
    QWEN_REALTIME_MODEL,
    QWEN_REALTIME_OUTPUT_SAMPLE_RATE,
    QWEN_REALTIME_REGION,
    QWEN_REALTIME_VAD_MODE,
    QWEN_REALTIME_VOICE,
    QWEN_REALTIME_WORKSPACE_ID,
)
from voice.session_state import structured_voice_log


LOG_RESERVED_FIELDS = {"event", "room_id", "room_name", "user_identity", "session_id"}


QWEN_INTERNAL_TOPICS = {
    "input_partial": "qwen.input_transcript.partial",
    "input_final": "qwen.input_transcript.final",
    "output_delta": "qwen.output_text.delta",
    "output_done": "qwen.output_text.done",
    "response_started": "qwen.response.started",
    "response_done": "qwen.response.done",
    "response_cancelled": "qwen.response.cancelled",
    "session_error": "qwen.session.error",
}


class RealtimeWebSocket(Protocol):
    async def send(self, data: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


WebSocketFactory = Callable[[str, dict[str, str]], Awaitable[RealtimeWebSocket]]
JsonCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
AudioCallback = Callable[[bytes, str], Awaitable[None] | None]


class QwenRealtimeConfigError(RuntimeError):
    pass


@dataclass
class QwenRealtimeConfig:
    api_key: str = QWEN_REALTIME_API_KEY
    workspace_id: str = QWEN_REALTIME_WORKSPACE_ID
    model: str = QWEN_REALTIME_MODEL
    region: str = QWEN_REALTIME_REGION
    base_url: str = QWEN_REALTIME_BASE_URL
    voice: str = QWEN_REALTIME_VOICE
    vad_mode: str = QWEN_REALTIME_VAD_MODE
    max_session_minutes: int = QWEN_REALTIME_MAX_SESSION_MINUTES
    input_sample_rate: int = QWEN_REALTIME_INPUT_SAMPLE_RATE
    output_sample_rate: int = QWEN_REALTIME_OUTPUT_SAMPLE_RATE
    audio_format: str = QWEN_REALTIME_AUDIO_FORMAT
    instructions: str = ""

    def endpoint(self) -> str:
        if self.base_url:
            base = self.base_url.rstrip("/")
            if "model=" in base:
                return base
            separator = "&" if "?" in base else "?"
            return f"{base}{separator}model={self.model}"
        if not self.workspace_id:
            raise QwenRealtimeConfigError("QWEN_REALTIME_WORKSPACE_ID is required for qwen_omni_realtime.")
        if self.region != "beijing":
            raise QwenRealtimeConfigError("Only QWEN_REALTIME_REGION=beijing is currently wired.")
        return (
            f"wss://{self.workspace_id}.cn-beijing.maas.aliyuncs.com"
            f"/api-ws/v1/realtime?model={self.model}"
        )

    def validate(self) -> None:
        if not self.api_key:
            raise QwenRealtimeConfigError("QWEN_REALTIME_API_KEY is required for qwen_omni_realtime.")
        if not str(self.voice or "").strip():
            raise QwenRealtimeConfigError("QWEN_REALTIME_VOICE is required for qwen_omni_realtime.")
        if self.vad_mode not in {"server_vad", "semantic_vad", "manual"}:
            raise QwenRealtimeConfigError(
                "QWEN_REALTIME_VAD_MODE must be one of: server_vad, semantic_vad, manual."
            )
        if self.input_sample_rate != 16000:
            raise QwenRealtimeConfigError("Qwen realtime input audio must be 16kHz PCM.")
        if self.output_sample_rate != 24000:
            raise QwenRealtimeConfigError("Qwen realtime output audio must be 24kHz PCM.")

    def turn_detection(self) -> dict[str, Any] | None:
        if self.vad_mode == "manual":
            return None
        return {"type": self.vad_mode}


@dataclass
class QwenRealtimeMetrics:
    connect_started: float = 0.0
    connected: float = 0.0
    first_audio_input: float = 0.0
    speech_started: float = 0.0
    speech_stopped: float = 0.0
    first_text: float = 0.0
    first_audio: float = 0.0
    output_audio_bytes: int = 0
    session_started: float = field(default_factory=time.monotonic)


class QwenOmniRealtimeSession:
    def __init__(
        self,
        *,
        room_id: str,
        room_name: str,
        participant_id: str,
        instructions: str,
        on_topic: JsonCallback,
        on_audio: AudioCallback,
        websocket_factory: WebSocketFactory | None = None,
        config: QwenRealtimeConfig | None = None,
    ) -> None:
        self.room_id = room_id
        self.room_name = room_name
        self.participant_id = participant_id
        self.config = config or QwenRealtimeConfig(instructions=instructions)
        self.config.instructions = instructions
        self.on_topic = on_topic
        self.on_audio = on_audio
        self.websocket_factory = websocket_factory or self._default_websocket_factory
        self.session_id = f"qwen-session-{uuid4().hex}"
        self.utterance_id = ""
        self.active_response_id = ""
        self.output_text_by_response: dict[str, list[str]] = {}
        self.audio_bytes_by_response: dict[str, int] = {}
        self.response_created_at: dict[str, float] = {}
        self.first_audio_delta_at: dict[str, float] = {}
        self.first_transcript_delta_at: dict[str, float] = {}
        self.cancelled_response_ids: set[str] = set()
        self._ws: RealtimeWebSocket | None = None
        self._recv_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._closed = False
        self._manual_has_audio = False
        self.metrics = QwenRealtimeMetrics()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._recv_task is not None and not self._recv_task.done()

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._closed:
                raise RuntimeError("Qwen realtime session is closed.")
            if self.is_connected:
                return
            if self._recv_task is not None and not self._recv_task.done():
                return
            if self._recv_task is not None and self._recv_task.done():
                self._consume_recv_task_result(self._recv_task)
                self._recv_task = None
            if self._ws is not None:
                await self._close_ws_only()
            self.config.validate()
            endpoint = self.config.endpoint()
            headers = {"Authorization": f"Bearer {self.config.api_key}"}
            self.metrics.connect_started = time.monotonic()
            self._log("qwen.session.connect_start", endpoint=_safe_endpoint(endpoint))
            self._ws = await self.websocket_factory(endpoint, headers)
            self.metrics.connected = time.monotonic()
            self._recv_task = asyncio.create_task(self._recv_loop(), name=f"qwen-recv-{self.participant_id}")
            self._recv_task.add_done_callback(self._on_recv_task_done)
            await self._send_session_update()
            self._log("qwen.session.connected", connect_ms=self._elapsed_ms(self.metrics.connect_started))

    async def append_audio(self, pcm16_16k: bytes) -> None:
        if not pcm16_16k:
            return
        await self.connect()
        if not self.metrics.first_audio_input:
            self.metrics.first_audio_input = time.monotonic()
            self._log("qwen.input.first_audio", bytes=len(pcm16_16k))
        self._manual_has_audio = True
        await self._send_event(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm16_16k).decode("ascii"),
            }
        )

    async def manual_commit(self) -> None:
        if self.config.vad_mode != "manual" or not self._manual_has_audio:
            return
        await self._send_event({"type": "input_audio_buffer.commit"})
        await self._send_event({"type": "response.create", "response": {"modalities": ["text", "audio"]}})
        self._manual_has_audio = False

    async def cancel_response(self, *, reason: str = "student_interrupt") -> None:
        response_id = self.active_response_id
        if response_id:
            self.cancelled_response_ids.add(response_id)
        if not response_id:
            self._log("qwen.response.cancel_skipped", reason=reason, response_id="")
            return
        if self._ws is not None:
            await self._send_event({"type": "response.cancel"})
        await self._emit_topic(
            QWEN_INTERNAL_TOPICS["response_cancelled"],
            {"responseId": response_id, "reason": reason},
        )
        self._log("qwen.response.cancelled", response_id=response_id, reason=reason)

    async def close(self) -> None:
        self._closed = True
        try:
            await self._close_ws_only()
            if self._recv_task is not None and not self._recv_task.done():
                self._recv_task.cancel()
            if self._recv_task is not None:
                await asyncio.gather(self._recv_task, return_exceptions=True)
                self._consume_recv_task_result(self._recv_task)
        finally:
            duration_ms = self._elapsed_ms(self.metrics.session_started)
            self._log("qwen.session.closed", duration_ms=duration_ms)
            self._ws = None
            self._recv_task = None

    async def _close_ws_only(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.close()
        finally:
            self._ws = None

    def expired(self) -> bool:
        return (time.monotonic() - self.metrics.session_started) >= max(60, self.config.max_session_minutes * 60)

    async def _send_session_update(self) -> None:
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "instructions": self.config.instructions,
            "voice": self.config.voice,
            "input_audio_format": self.config.audio_format,
            "output_audio_format": self.config.audio_format,
        }
        turn_detection = self.config.turn_detection()
        if turn_detection is not None:
            session["turn_detection"] = turn_detection
        await self._send_event({"type": "session.update", "session": session})
        self._log(
            "qwen.session.instructions",
            instructions_length=len(self.config.instructions or ""),
            instructions_hash=_hash_text(self.config.instructions or ""),
            voice=_mask_identifier(self.config.voice),
        )

    async def _send_event(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            return
        payload = {"event_id": f"evt_{uuid4().hex}", **payload}
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        while not self._closed:
            try:
                raw = await self._ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closed:
                    await self._emit_error("websocket_recv_failed", str(exc))
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await self._emit_error("invalid_json", raw[:160])
                continue
            await self._handle_server_event(payload)

    def _on_recv_task_done(self, task: asyncio.Task) -> None:
        self._consume_recv_task_result(task)

    def _consume_recv_task_result(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        self._log(
            "qwen.recv_task.failed",
            error_type=type(exc).__name__,
        )

    async def _handle_server_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type") or "")
        response_id = str(payload.get("response_id") or payload.get("response", {}).get("id") or self.active_response_id)
        self._log(
            "qwen.server.event",
            event_type=event_type,
            response_id=response_id,
            audio_bytes=0,
            text_length=0,
        )
        if event_type == "error":
            error = payload.get("error") or payload
            await self._emit_error(str(error.get("code") if isinstance(error, dict) else "server_error"), str(error))
            return

        if event_type == "session.updated":
            session_payload = payload.get("session") if isinstance(payload.get("session"), dict) else {}
            self._log(
                "qwen.session.updated",
                session_id=str(session_payload.get("id") or ""),
                instructions_length=len(self.config.instructions or ""),
                instructions_hash=_hash_text(self.config.instructions or ""),
                voice=_mask_identifier(str(session_payload.get("voice") or self.config.voice or "")),
            )
            return

        if event_type == "input_audio_buffer.speech_started":
            self.utterance_id = str(payload.get("item_id") or payload.get("utterance_id") or f"utt_{uuid4().hex}")
            self.metrics.speech_started = time.monotonic()
            await self.cancel_response(reason="input_speech_started")
            self._log("qwen.input.speech_started", utterance_id=self.utterance_id)
            return

        if event_type == "input_audio_buffer.speech_stopped":
            self.metrics.speech_stopped = time.monotonic()
            self._log("qwen.input.speech_stopped", utterance_id=self.utterance_id)
            return

        if event_type in {"conversation.item.input_audio_transcription.delta", "input_audio_transcription.delta"}:
            text = _extract_input_transcript_delta(payload)
            if not text:
                self._log_field_missing(event_type, payload, response_id=response_id, expected="text/stash")
                return
            await self._emit_topic(
                QWEN_INTERNAL_TOPICS["input_partial"],
                {"utteranceId": self.utterance_id, "text": text},
            )
            return

        if event_type in {"conversation.item.input_audio_transcription.completed", "input_audio_transcription.completed"}:
            text = _extract_required_string(payload, "transcript")
            if not text:
                self._log_field_missing(event_type, payload, response_id=response_id, expected="transcript")
                return
            await self._emit_topic(
                QWEN_INTERNAL_TOPICS["input_final"],
                {"utteranceId": self.utterance_id, "text": text},
            )
            self._log("qwen.input.transcript_final", utterance_id=self.utterance_id, text_length=len(text))
            return

        if event_type == "response.created":
            self.active_response_id = response_id or str(payload.get("response", {}).get("id") or f"resp_{uuid4().hex}")
            self.output_text_by_response[self.active_response_id] = []
            self.audio_bytes_by_response[self.active_response_id] = 0
            self.response_created_at[self.active_response_id] = time.monotonic()
            await self._emit_topic(
                QWEN_INTERNAL_TOPICS["response_started"],
                {"responseId": self.active_response_id, "utteranceId": self.utterance_id},
            )
            self._log("qwen.response.started", response_id=self.active_response_id, utterance_id=self.utterance_id)
            return

        if event_type in {"response.audio_transcript.delta", "response.text.delta"}:
            if self._is_late_response(response_id):
                return
            delta = _extract_required_string(payload, "delta")
            if not delta:
                self._log_field_missing(event_type, payload, response_id=response_id, expected="delta")
                return
            if not self.metrics.first_text:
                self.metrics.first_text = time.monotonic()
                self._log("qwen.response.first_text", latency_ms=self._speech_end_latency_ms(self.metrics.first_text))
            if response_id not in self.first_transcript_delta_at:
                self.first_transcript_delta_at[response_id] = time.monotonic()
                created_at = self.response_created_at.get(response_id, 0.0)
                first_audio_at = self.first_audio_delta_at.get(response_id, 0.0)
                self._log(
                    "qwen.sync.first_transcript_delta",
                    response_id=response_id,
                    text_length=len(delta),
                    response_created_delta_ms=self._delta_ms(created_at, self.first_transcript_delta_at[response_id]),
                )
                if first_audio_at:
                    self._log(
                        "qwen.sync.measured_lag",
                        response_id=response_id,
                        transcript_vs_audio_delta_ms=self._signed_delta_ms(
                            first_audio_at,
                            self.first_transcript_delta_at[response_id],
                        ),
                    )
            self.output_text_by_response.setdefault(response_id, []).append(delta)
            self._log(
                "qwen.response.text_delta",
                response_id=response_id,
                text_length=len(delta),
            )
            await self._emit_topic(
                QWEN_INTERNAL_TOPICS["output_delta"],
                {"responseId": response_id, "utteranceId": self.utterance_id, "delta": delta},
            )
            return

        if event_type in {"response.audio_transcript.done", "response.text.done"}:
            if self._is_late_response(response_id):
                return
            text = _extract_required_string(payload, "transcript")
            if not text:
                text = "".join(self.output_text_by_response.get(response_id, []))
            if not text:
                self._log_field_missing(event_type, payload, response_id=response_id, expected="transcript")
                return
            await self._emit_topic(
                QWEN_INTERNAL_TOPICS["output_done"],
                {"responseId": response_id, "utteranceId": self.utterance_id, "text": text},
            )
            return

        if event_type == "response.audio.delta":
            if self._is_late_response(response_id):
                return
            encoded = _extract_required_string(payload, "delta")
            if not encoded:
                self._log_field_missing(event_type, payload, response_id=response_id, expected="delta")
                return
            audio = _decode_audio_delta(encoded)
            if not audio:
                self._log("qwen.response.audio_delta_decode_empty", response_id=response_id, encoded_length=len(encoded))
                return
            if not self.metrics.first_audio:
                self.metrics.first_audio = time.monotonic()
                self._log("qwen.response.first_audio", latency_ms=self._speech_end_latency_ms(self.metrics.first_audio))
            if response_id not in self.first_audio_delta_at:
                self.first_audio_delta_at[response_id] = time.monotonic()
                created_at = self.response_created_at.get(response_id, 0.0)
                first_text_at = self.first_transcript_delta_at.get(response_id, 0.0)
                self._log(
                    "qwen.sync.first_audio_delta",
                    response_id=response_id,
                    decoded_bytes=len(audio),
                    response_created_delta_ms=self._delta_ms(created_at, self.first_audio_delta_at[response_id]),
                )
                if first_text_at:
                    self._log(
                        "qwen.sync.measured_lag",
                        response_id=response_id,
                        transcript_vs_audio_delta_ms=self._signed_delta_ms(
                            self.first_audio_delta_at[response_id],
                            first_text_at,
                        ),
                    )
            self.metrics.output_audio_bytes += len(audio)
            self.audio_bytes_by_response[response_id] = self.audio_bytes_by_response.get(response_id, 0) + len(audio)
            self._log(
                "qwen.response.audio_delta",
                response_id=response_id,
                encoded_length=len(encoded),
                decoded_bytes=len(audio),
                total_audio_bytes=self.audio_bytes_by_response[response_id],
            )
            await self._maybe_await(self.on_audio(audio, response_id))
            return

        if event_type == "response.audio.done":
            if not self._is_late_response(response_id):
                await self._maybe_await(self.on_audio(b"", response_id))
                self._log(
                    "qwen.response.audio_done",
                    response_id=response_id,
                    audio_bytes=self.audio_bytes_by_response.get(response_id, 0),
                )
            return

        if event_type in {"response.done", "response.cancelled"}:
            if event_type == "response.cancelled":
                self.cancelled_response_ids.add(response_id)
                await self._emit_topic(
                    QWEN_INTERNAL_TOPICS["response_cancelled"],
                    {"responseId": response_id, "utteranceId": self.utterance_id},
                )
                self._log("qwen.response.cancelled", response_id=response_id, source="server")
                return
            if not self._is_late_response(response_id):
                await self._maybe_await(self.on_audio(b"", response_id))
                await self._emit_topic(
                    QWEN_INTERNAL_TOPICS["response_done"],
                    {
                        "responseId": response_id,
                        "utteranceId": self.utterance_id,
                        "audioBytes": self.metrics.output_audio_bytes,
                    },
                )
                self._log(
                    "qwen.response.done",
                    response_id=response_id,
                    audio_bytes=self.audio_bytes_by_response.get(response_id, self.metrics.output_audio_bytes),
                    text_length=len("".join(self.output_text_by_response.get(response_id, []))),
                )
            return

    def _is_late_response(self, response_id: str) -> bool:
        return bool(response_id and response_id in self.cancelled_response_ids)

    async def _emit_topic(self, topic: str, payload: dict[str, Any]) -> None:
        payload.setdefault("roomId", self.room_id)
        payload.setdefault("roomName", self.room_name)
        payload.setdefault("participantId", self.participant_id)
        payload.setdefault("sessionId", self.session_id)
        await self._maybe_await(self.on_topic(topic, payload))

    async def _emit_error(self, code: str, message: str) -> None:
        self._log("qwen.session.error", code=code, message=message[:300])
        await self._emit_topic(QWEN_INTERNAL_TOPICS["session_error"], {"code": code, "message": message})

    def _log(self, event: str, **payload: Any) -> None:
        record = {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "user_identity": self.participant_id,
            "session_id": self.session_id,
        }
        for key, value in payload.items():
            if key == "event":
                record["payload_event"] = value
            elif key == "session_id":
                record["server_session_id"] = value
            elif key in {"room_id", "room_name", "user_identity"}:
                record[f"payload_{key}"] = value
            else:
                record[key] = value
        try:
            structured_voice_log(event, **record)
        except Exception as exc:
            try:
                structured_voice_log(
                    "qwen.log.failed",
                    room_id=self.room_id,
                    room_name=self.room_name,
                    user_identity=self.participant_id,
                    session_id=self.session_id,
                    original_event=event,
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass

    def _log_field_missing(self, event_type: str, payload: dict[str, Any], *, response_id: str, expected: str) -> None:
        self._log(
            "qwen.event.field_missing",
            event_type=event_type,
            response_id=response_id,
            expected=expected,
            keys=sorted(str(key) for key in payload.keys())[:20],
        )

    @staticmethod
    async def _default_websocket_factory(url: str, headers: dict[str, str]) -> ClientConnection:
        return await websockets.connect(
            url,
            additional_headers=headers,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )

    @staticmethod
    async def _maybe_await(result: Awaitable[None] | None) -> None:
        if result is not None:
            await result

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        if not started:
            return 0
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _delta_ms(started: float, ended: float) -> int:
        if not started or not ended:
            return 0
        return int((ended - started) * 1000)

    @staticmethod
    def _signed_delta_ms(audio_at: float, transcript_at: float) -> int:
        if not audio_at or not transcript_at:
            return 0
        return int((transcript_at - audio_at) * 1000)

    def _speech_end_latency_ms(self, at_time: float) -> int:
        if not self.metrics.speech_stopped:
            return 0
        return int((at_time - self.metrics.speech_stopped) * 1000)


def build_lumina_realtime_instructions(*, class_mode: bool = False) -> str:
    from llm_wrapper import build_teacher_system_prompt

    return build_teacher_system_prompt(
        "class_mode" if class_mode else "normal_chat",
        {"class_mode": class_mode},
    )


def _extract_required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    return value if isinstance(value, str) else ""


def _extract_input_transcript_delta(payload: dict[str, Any]) -> str:
    text = _extract_required_string(payload, "text")
    stash = _extract_required_string(payload, "stash")
    return text or stash


def _decode_audio_delta(raw: str) -> bytes:
    if not raw:
        return b""
    try:
        return base64.b64decode(raw)
    except Exception:
        return b""


def _safe_endpoint(url: str) -> str:
    return url.split("?")[0] + "?model=<redacted>" if "?" in url else url


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _mask_identifier(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "<empty>"
    if len(text) <= 4:
        return text[0] + "***"
    return f"{text[:2]}***{text[-2:]}"
