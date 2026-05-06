from __future__ import annotations

import asyncio
import base64
import gzip
import inspect
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

import httpx
import websockets

from config import (
    DOUBAO_ACCESS_TOKEN,
    DOUBAO_API_KEY,
    DOUBAO_APP_ID,
    DOUBAO_AUTH_MODE,
    DOUBAO_CLUSTER,
    DOUBAO_TTS_API_VERSION,
    DOUBAO_TTS_ENABLE_STREAM,
    DOUBAO_TTS_MODEL,
    DOUBAO_TTS_RESOURCE_ID,
    DOUBAO_TTS_RESOURCE_ID_HEADER,
    DOUBAO_TTS_SPEAKER_ID,
    DOUBAO_TTS_V3_ENDPOINT,
    DOUBAO_TTS_VOICE,
    DOUBAO_TTS_VOICE_TYPE,
    TTS_TIMEOUT_SECONDS,
)

from .base import SynthesisResult
from . import doubao_protocol as protocol
from .errors import SpeechProviderConfigError, SpeechProviderRuntimeError


logger = logging.getLogger("uvicorn.error")

DOUBAO_V1_HTTP_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"
DOUBAO_V1_WS_TTS_URL = "wss://openspeech.bytedance.com/api/v1/tts/ws_binary"
DOUBAO_AUDIO_ENCODING = "mp3"
DOUBAO_AUDIO_MEDIA_TYPE = "audio/mpeg"


def _log_event(event: str, **payload: Any) -> None:
    try:
        logger.info(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str))
    except Exception:
        logger.info("%s %s", event, payload)


def _truncate(value: str | None, limit: int = 600) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated>"


def _read_int32(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True)


def _read_uint32(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


class DoubaoTTSProvider:
    name = "doubao"

    def __init__(self) -> None:
        self.api_version = (DOUBAO_TTS_API_VERSION or "v3").strip().lower()
        self.auth_mode = (DOUBAO_AUTH_MODE or "api_key").strip().lower()
        self.api_key = DOUBAO_API_KEY.strip()
        self.app_id = DOUBAO_APP_ID.strip()
        self.access_token = DOUBAO_ACCESS_TOKEN.strip()
        self.cluster = DOUBAO_CLUSTER.strip()
        self.v3_endpoint = DOUBAO_TTS_V3_ENDPOINT.strip()
        self.resource_id = DOUBAO_TTS_RESOURCE_ID.strip()
        self.resource_id_header = DOUBAO_TTS_RESOURCE_ID_HEADER.strip() or "X-Api-Resource-Id"
        self.voice_type = DOUBAO_TTS_VOICE_TYPE.strip()
        self.speaker_id = DOUBAO_TTS_SPEAKER_ID.strip()
        self.default_voice = DOUBAO_TTS_VOICE.strip()
        self.model = DOUBAO_TTS_MODEL.strip()
        self.streaming_enabled = DOUBAO_TTS_ENABLE_STREAM

    def _ensure_configured(self) -> None:
        missing: list[str] = []
        if not self._effective_voice_type(None):
            missing.append("DOUBAO_TTS_VOICE_TYPE")
        if self.api_version == "v3":
            if not self.v3_endpoint:
                missing.append("DOUBAO_TTS_V3_ENDPOINT")
            if not self.resource_id:
                missing.append("DOUBAO_TTS_RESOURCE_ID")
            if self.auth_mode == "api_key":
                if not self.api_key:
                    missing.append("DOUBAO_API_KEY")
            elif self.auth_mode == "app_token":
                if not self.app_id:
                    missing.append("DOUBAO_APP_ID")
                if not self.access_token:
                    missing.append("DOUBAO_ACCESS_TOKEN")
            else:
                missing.append("DOUBAO_AUTH_MODE must be api_key or app_token")
        elif self.api_version == "v1":
            if not self.app_id:
                missing.append("DOUBAO_APP_ID")
            if not self.access_token:
                missing.append("DOUBAO_ACCESS_TOKEN")
            if not self.cluster:
                missing.append("DOUBAO_CLUSTER")
        else:
            missing.append("DOUBAO_TTS_API_VERSION must be v3 or v1")
        if missing:
            raise SpeechProviderConfigError(
                f"Missing Doubao TTS config: {', '.join(missing)}",
                status_code=500,
            )

    def _effective_voice_type(self, voice: str | None) -> str:
        requested = (voice or "").strip()
        if self.voice_type:
            return self.voice_type
        if self.speaker_id:
            return self.speaker_id
        if self.default_voice and self.default_voice != "doubao_default":
            return self.default_voice
        if requested and requested not in {"doubao_default", "melo_teacher", "af_heart", "zf_001"}:
            return requested
        return ""

    def _v3_headers(self, reqid: str) -> dict[str, str]:
        if self.auth_mode == "api_key":
            return {
                "X-Api-Key": self.api_key,
                self.resource_id_header: self.resource_id,
                "X-Api-Connect-Id": reqid,
            }
        return {
            "X-Api-App-Id": self.app_id,
            "X-Api-Access-Key": self.access_token,
            self.resource_id_header: self.resource_id,
            "X-Api-Connect-Id": reqid,
        }

    def _v1_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer;{self.access_token}",
            "Content-Type": "application/json",
        }
        if self.resource_id:
            headers[self.resource_id_header] = self.resource_id
        return headers

    def _v3_start_session_payload(
        self,
        *,
        voice: str | None,
        speed: float | None,
        reqid: str,
    ) -> dict[str, Any]:
        req_params: dict[str, Any] = {
            "reqid": reqid,
            "speaker": self._effective_voice_type(voice),
            "audio_params": {
                "format": DOUBAO_AUDIO_ENCODING,
                "sample_rate": 24000,
                "bit_rate": 128000,
            },
            "speed_ratio": float(speed or 1.0),
            "additions": json.dumps(
                {
                    "explicit_language": "zh-cn",
                    "disable_markdown_filter": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        if self.model:
            req_params["model"] = self.model
        return {
            "user": {
                "uid": "ai-teacher-local",
            },
            "event": protocol.EVENT_START_SESSION,
            "namespace": "BidirectionalTTS",
            "req_params": req_params,
        }

    def _v3_task_payload(self, *, text: str, reqid: str) -> dict[str, Any]:
        return {
            "user": {
                "uid": "ai-teacher-local",
            },
            "event": protocol.EVENT_TASK_REQUEST,
            "namespace": "BidirectionalTTS",
            "req_params": {
                "reqid": reqid,
                "text": text,
            },
        }

    def _v1_payload(
        self,
        *,
        text: str,
        voice: str | None,
        speed: float | None,
        operation: str,
    ) -> dict[str, Any]:
        return {
            "app": {
                "appid": self.app_id,
                "token": self.access_token,
                "cluster": self.cluster,
            },
            "user": {
                "uid": "ai-teacher-local",
            },
            "audio": {
                "voice_type": self._effective_voice_type(voice),
                "encoding": DOUBAO_AUDIO_ENCODING,
                "speed_ratio": float(speed or 1.0),
                "rate": 24000,
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": operation,
            },
        }

    async def list_voices(self) -> dict:
        voice_type = self._effective_voice_type(None)
        configured = bool(voice_type)
        if self.api_version == "v3":
            if self.auth_mode == "api_key":
                configured = configured and bool(self.api_key and self.v3_endpoint and self.resource_id)
            else:
                configured = configured and bool(self.app_id and self.access_token and self.v3_endpoint and self.resource_id)
        else:
            configured = configured and bool(self.app_id and self.access_token and self.cluster)
        return {
            "backend_name": self.name,
            "api_version": self.api_version,
            "mode": "streaming" if self.streaming_enabled else "http",
            "ready": configured,
            "auth_mode": self.auth_mode,
            "streaming_enabled": self.streaming_enabled,
            "endpoint": self.v3_endpoint if self.api_version == "v3" else DOUBAO_V1_HTTP_TTS_URL,
            "resource_id": self.resource_id if self.api_version == "v3" else None,
            "voice_type": voice_type,
            "voices": [
                {
                    "id": "doubao_default",
                    "voice_type": voice_type,
                    "label": "Doubao TTS V3 voice" if self.api_version == "v3" else "Doubao TTS V1 voice",
                    "supported_langs": ["zh", "en", "zh-en"],
                    "backend_name": self.name,
                }
            ],
            "legacy_fallback": "local_melo",
        }

    async def synthesize_speech(
        self,
        *,
        text: str,
        voice: str | None,
        lang: str | None,
        speed: float | None,
    ) -> SynthesisResult:
        if self.api_version == "v1":
            return await self._synthesize_speech_v1(text=text, voice=voice, lang=lang, speed=speed)

        chunks: list[bytes] = []
        async for chunk in self.stream_speech(text=text, voice=voice, lang=lang, speed=speed):
            chunks.append(chunk)
        return SynthesisResult(audio=b"".join(chunks), media_type=DOUBAO_AUDIO_MEDIA_TYPE)

    async def stream_speech(
        self,
        *,
        text: str,
        voice: str | None,
        lang: str | None,
        speed: float | None,
    ) -> AsyncIterator[bytes]:
        self._ensure_configured()
        if not self.streaming_enabled:
            raise SpeechProviderConfigError("Doubao TTS streaming is disabled")
        if self.api_version == "v1":
            async for chunk in self._stream_speech_v1(text=text, voice=voice, lang=lang, speed=speed):
                yield chunk
            return
        async for chunk in self._stream_speech_v3(text=text, voice=voice, lang=lang, speed=speed):
            yield chunk

    async def _stream_speech_v3(
        self,
        *,
        text: str,
        voice: str | None,
        lang: str | None,
        speed: float | None,
    ) -> AsyncIterator[bytes]:
        started = time.perf_counter()
        reqid = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        voice_type = self._effective_voice_type(voice)
        start_session_payload = self._v3_start_session_payload(
            voice=voice,
            speed=speed,
            reqid=reqid,
        )
        task_payload = self._v3_task_payload(text=text, reqid=reqid)
        chunk_count = 0
        total_bytes = 0

        _log_event(
            "doubao.tts.v3.bidirection.connect.begin",
            api_version="v3",
            auth_mode=self.auth_mode,
            endpoint=self.v3_endpoint,
            resource_id=self.resource_id,
            speaker=voice_type,
            text_length=len(text),
            encoding=DOUBAO_AUDIO_ENCODING,
            stream=True,
            reqid=reqid,
            session_id=session_id,
            lang=lang,
        )

        try:
            async with websockets.connect(
                self.v3_endpoint,
                **self._websocket_connect_kwargs(headers=self._v3_headers(reqid)),
            ) as websocket:
                _log_event(
                    "doubao.tts.v3.bidirection.connected",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    text_length=len(text),
                    encoding=DOUBAO_AUDIO_ENCODING,
                    stream=True,
                    reqid=reqid,
                    session_id=session_id,
                )
                await websocket.send(protocol.build_start_connection_request())
                _log_event(
                    "doubao.tts.v3.stream.request.sent",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    text_length=0,
                    encoding=DOUBAO_AUDIO_ENCODING,
                    stream=True,
                    reqid=reqid,
                    session_id=session_id,
                    protocol_event=protocol.EVENT_START_CONNECTION,
                    message_type=protocol.MSG_TYPE_FULL_CLIENT_REQUEST,
                )
                await self._wait_for_v3_event(
                    websocket,
                    expected_events={protocol.EVENT_CONNECTION_STARTED},
                    reqid=reqid,
                    voice_type=voice_type,
                    text=text,
                )
                _log_event(
                    "doubao.tts.v3.bidirection.connection_started",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    protocol_event=protocol.EVENT_CONNECTION_STARTED,
                    session_id=session_id,
                    reqid=reqid,
                )

                await websocket.send(protocol.build_start_session_request(session_id, start_session_payload))
                _log_event(
                    "doubao.tts.v3.stream.request.sent",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    text_length=0,
                    encoding=DOUBAO_AUDIO_ENCODING,
                    stream=True,
                    reqid=reqid,
                    session_id=session_id,
                    protocol_event=protocol.EVENT_START_SESSION,
                    message_type=protocol.MSG_TYPE_FULL_CLIENT_REQUEST,
                )
                await self._wait_for_v3_event(
                    websocket,
                    expected_events={protocol.EVENT_SESSION_STARTED},
                    reqid=reqid,
                    voice_type=voice_type,
                    text=text,
                )
                _log_event(
                    "doubao.tts.v3.bidirection.session_started",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    protocol_event=protocol.EVENT_SESSION_STARTED,
                    session_id=session_id,
                    reqid=reqid,
                )

                await websocket.send(protocol.build_task_request(session_id, task_payload))
                _log_event(
                    "doubao.tts.v3.bidirection.task_request.sent",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    text_length=len(text),
                    encoding=DOUBAO_AUDIO_ENCODING,
                    stream=True,
                    reqid=reqid,
                    session_id=session_id,
                    protocol_event=protocol.EVENT_TASK_REQUEST,
                    message_type=protocol.MSG_TYPE_FULL_CLIENT_REQUEST,
                )

                await websocket.send(protocol.build_finish_session_request(session_id))
                _log_event(
                    "doubao.tts.v3.stream.request.sent",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    text_length=0,
                    encoding=DOUBAO_AUDIO_ENCODING,
                    stream=True,
                    reqid=reqid,
                    session_id=session_id,
                    protocol_event=protocol.EVENT_FINISH_SESSION,
                    message_type=protocol.MSG_TYPE_FULL_CLIENT_REQUEST,
                )

                while True:
                    message = await asyncio.wait_for(websocket.recv(), timeout=float(TTS_TIMEOUT_SECONDS))
                    if isinstance(message, str):
                        parsed_text = self._parse_text_ws_message(message)
                        if parsed_text["type"] == "audio":
                            audio = parsed_text["audio"]
                            if audio:
                                chunk_count += 1
                                total_bytes += len(audio)
                                self._log_v3_audio_chunk(reqid, voice_type, text, chunk_count, len(audio), total_bytes)
                                yield audio
                            if parsed_text.get("is_last"):
                                break
                        elif parsed_text["type"] == "error":
                            raise SpeechProviderRuntimeError(
                                parsed_text.get("message") or "Doubao V3 stream error",
                                status_code=parsed_text.get("code"),
                                response_text=_truncate(parsed_text.get("raw")),
                            )
                        continue

                    parsed = self._parse_v3_binary_response(message)
                    if parsed["type"] == "audio":
                        audio = parsed["audio"]
                        if audio:
                            chunk_count += 1
                            total_bytes += len(audio)
                            self._log_v3_audio_chunk(reqid, voice_type, text, chunk_count, len(audio), total_bytes)
                            yield audio
                        if parsed.get("is_last"):
                            break
                    elif parsed["type"] == "end":
                        break
                    elif parsed["type"] == "metadata":
                        _log_event(
                            "doubao.tts.v3.stream.response",
                            api_version="v3",
                            auth_mode=self.auth_mode,
                            reqid=reqid,
                            session_id=session_id,
                            protocol_event=parsed.get("event"),
                            message_type=parsed.get("message_type"),
                            payload=_truncate(parsed.get("raw")),
                        )
                    elif parsed["type"] == "error":
                        raise SpeechProviderRuntimeError(
                            parsed.get("message") or "Doubao V3 stream error",
                            status_code=parsed.get("code"),
                            response_text=_truncate(parsed.get("raw")),
                        )

                _log_event(
                    "doubao.tts.v3.bidirection.session_finished",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    protocol_event=protocol.EVENT_SESSION_FINISHED,
                    session_id=session_id,
                    reqid=reqid,
                    audio_bytes=total_bytes,
                )
                await websocket.send(protocol.build_finish_connection_request())
                _log_event(
                    "doubao.tts.v3.stream.request.sent",
                    api_version="v3",
                    auth_mode=self.auth_mode,
                    endpoint=self.v3_endpoint,
                    resource_id=self.resource_id,
                    speaker=voice_type,
                    text_length=0,
                    encoding=DOUBAO_AUDIO_ENCODING,
                    stream=True,
                    reqid=reqid,
                    session_id=session_id,
                    protocol_event=protocol.EVENT_FINISH_CONNECTION,
                    message_type=protocol.MSG_TYPE_FULL_CLIENT_REQUEST,
                )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            _log_event(
                "doubao.tts.v3.error",
                api_version="v3",
                auth_mode=self.auth_mode,
                endpoint=self.v3_endpoint,
                resource_id=self.resource_id,
                speaker=voice_type,
                text_length=len(text),
                encoding=DOUBAO_AUDIO_ENCODING,
                stream=True,
                reqid=reqid,
                session_id=session_id,
                elapsed_ms=elapsed_ms,
                exception_repr=repr(exc),
                status=getattr(exc, "status_code", None),
                message=str(exc),
            )
            if isinstance(exc, SpeechProviderRuntimeError):
                raise
            raise SpeechProviderRuntimeError(
                f"Doubao V3 TTS stream failed (reqid={reqid}): {exc}",
                exception_repr=repr(exc),
            ) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _log_event(
            "doubao.tts.v3.stream.end",
            api_version="v3",
            auth_mode=self.auth_mode,
            endpoint=self.v3_endpoint,
            resource_id=self.resource_id,
            speaker=voice_type,
            text_length=len(text),
            encoding=DOUBAO_AUDIO_ENCODING,
            stream=True,
            reqid=reqid,
            session_id=session_id,
            elapsed_ms=elapsed_ms,
            chunk_count=chunk_count,
            total_bytes=total_bytes,
        )

    async def _wait_for_v3_event(
        self,
        websocket: Any,
        *,
        expected_events: set[int],
        reqid: str,
        voice_type: str,
        text: str,
    ) -> None:
        while True:
            message = await asyncio.wait_for(websocket.recv(), timeout=float(TTS_TIMEOUT_SECONDS))
            if isinstance(message, str):
                parsed_text = self._parse_text_ws_message(message)
                if parsed_text["type"] == "error":
                    raise SpeechProviderRuntimeError(
                        parsed_text.get("message") or "Doubao V3 stream error",
                        status_code=parsed_text.get("code"),
                        response_text=_truncate(parsed_text.get("raw")),
                    )
                continue
            parsed = self._parse_v3_binary_response(message)
            event = parsed.get("event")
            _log_event(
                "doubao.tts.v3.stream.response",
                api_version="v3",
                auth_mode=self.auth_mode,
                endpoint=self.v3_endpoint,
                resource_id=self.resource_id,
                speaker=voice_type,
                text_length=len(text),
                encoding=DOUBAO_AUDIO_ENCODING,
                stream=True,
                reqid=reqid,
                protocol_event=event,
                message_type=parsed.get("message_type"),
                payload=_truncate(parsed.get("raw")),
            )
            if parsed["type"] == "error":
                raise SpeechProviderRuntimeError(
                    parsed.get("message") or "Doubao V3 stream error",
                    status_code=parsed.get("code"),
                    response_text=_truncate(parsed.get("raw")),
                )
            if event in expected_events:
                return

    def _log_v3_audio_chunk(
        self,
        reqid: str,
        voice_type: str,
        text: str,
        chunk_index: int,
        chunk_bytes: int,
        total_bytes: int,
    ) -> None:
        _log_event(
            "doubao.tts.v3.bidirection.audio_chunk",
            api_version="v3",
            auth_mode=self.auth_mode,
            endpoint=self.v3_endpoint,
            resource_id=self.resource_id,
            speaker=voice_type,
            text_length=len(text),
            encoding=DOUBAO_AUDIO_ENCODING,
            stream=True,
            reqid=reqid,
            chunk_index=chunk_index,
            audio_chunk_size=chunk_bytes,
            total_bytes=total_bytes,
        )

    def _websocket_connect_kwargs(self, *, headers: dict[str, str]) -> dict[str, Any]:
        connect_kwargs: dict[str, Any] = {
            "open_timeout": min(float(TTS_TIMEOUT_SECONDS), 30.0),
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": None,
        }
        connect_parameters = inspect.signature(websockets.connect).parameters
        if "additional_headers" in connect_parameters:
            connect_kwargs["additional_headers"] = headers
        else:
            connect_kwargs["extra_headers"] = headers
        return connect_kwargs

    def _parse_text_ws_message(self, message: str) -> dict[str, Any]:
        try:
            payload = json.loads(message)
        except Exception:
            return {"type": "error", "message": message, "raw": message}
        if payload.get("data"):
            try:
                audio = base64.b64decode(payload["data"])
            except Exception:
                audio = b""
            return {"type": "audio", "audio": audio, "is_last": bool(payload.get("is_last") or payload.get("done"))}
        if payload.get("audio"):
            raw_audio = payload["audio"]
            if isinstance(raw_audio, str):
                try:
                    return {"type": "audio", "audio": base64.b64decode(raw_audio), "is_last": bool(payload.get("is_last"))}
                except Exception:
                    pass
        if payload.get("code") not in (None, 0, "0"):
            return {
                "type": "error",
                "code": payload.get("code"),
                "message": payload.get("message") or payload.get("error") or "Doubao V3 text frame error",
                "raw": json.dumps(payload, ensure_ascii=False),
            }
        return {"type": "metadata", "raw": json.dumps(payload, ensure_ascii=False)}

    def _parse_v3_binary_response(self, response: bytes) -> dict[str, Any]:
        try:
            message = protocol.parse_response(response)
        except Exception as exc:
            return {"type": "error", "message": str(exc), "raw": response.hex()}
        raw = ""
        if message.payload_msg is not None:
            raw = json.dumps(message.payload_msg, ensure_ascii=False)
        elif message.payload:
            raw = message.payload.decode("utf-8", errors="replace")

        if message.message_type == protocol.MSG_TYPE_AUDIO_ONLY_SERVER:
            is_last = message.event in {protocol.EVENT_TTS_ENDED, protocol.EVENT_SESSION_FINISHED}
            return {
                "type": "audio",
                "audio": message.payload,
                "event": message.event,
                "message_type": message.message_type,
                "sequence": message.sequence,
                "is_last": is_last,
                "raw": raw,
            }
        if message.event == protocol.EVENT_TTS_RESPONSE and message.payload and message.payload_msg is None:
            return {
                "type": "audio",
                "audio": message.payload,
                "event": message.event,
                "message_type": message.message_type,
                "sequence": message.sequence,
                "is_last": False,
                "raw": "",
            }
        if message.message_type == protocol.MSG_TYPE_ERROR or message.event in {
            protocol.EVENT_CONNECTION_FAILED,
            protocol.EVENT_SESSION_FAILED,
        }:
            return {
                "type": "error",
                "code": message.error_code,
                "message": raw or "Doubao V3 protocol error",
                "event": message.event,
                "message_type": message.message_type,
                "raw": raw,
            }
        if message.event in {protocol.EVENT_TTS_ENDED, protocol.EVENT_SESSION_FINISHED}:
            return {"type": "end", "event": message.event, "message_type": message.message_type, "raw": raw}
        return {"type": "metadata", "event": message.event, "message_type": message.message_type, "raw": raw}

    async def _synthesize_speech_v1(
        self,
        *,
        text: str,
        voice: str | None,
        lang: str | None,
        speed: float | None,
    ) -> SynthesisResult:
        self._ensure_configured()
        started = time.perf_counter()
        payload = self._v1_payload(text=text, voice=voice, speed=speed, operation="query")
        reqid = payload["request"]["reqid"]
        _log_event(
            "doubao.tts.request.begin",
            api_version="v1",
            endpoint=DOUBAO_V1_HTTP_TTS_URL,
            reqid=reqid,
            text_length=len(text),
            voice_type=payload["audio"]["voice_type"],
            cluster=self.cluster,
            encoding=DOUBAO_AUDIO_ENCODING,
            stream=False,
        )
        try:
            async with httpx.AsyncClient(timeout=TTS_TIMEOUT_SECONDS, trust_env=False) as client:
                response = await client.post(DOUBAO_V1_HTTP_TTS_URL, headers=self._v1_headers(), json=payload)
        except Exception as exc:
            raise SpeechProviderRuntimeError(
                f"Doubao V1 TTS HTTP request failed (reqid={reqid}): {exc}",
                exception_repr=repr(exc),
            ) from exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise SpeechProviderRuntimeError(
                f"Doubao V1 TTS HTTP error (reqid={reqid})",
                status_code=response.status_code,
                response_text=_truncate(response.text),
            )
        data = response.json()
        if data.get("code") not in (0, "0", None) or not data.get("data"):
            raise SpeechProviderRuntimeError(
                f"Doubao V1 TTS API error (reqid={reqid}, code={data.get('code')}): {data.get('message')}",
                status_code=response.status_code,
                response_text=_truncate(json.dumps(data, ensure_ascii=False)),
            )
        audio = base64.b64decode(data["data"])
        _log_event(
            "doubao.tts.request.success",
            api_version="v1",
            endpoint=DOUBAO_V1_HTTP_TTS_URL,
            reqid=reqid,
            text_length=len(text),
            elapsed_ms=elapsed_ms,
            audio_bytes=len(audio),
        )
        return SynthesisResult(audio=audio, media_type=DOUBAO_AUDIO_MEDIA_TYPE)

    async def _stream_speech_v1(
        self,
        *,
        text: str,
        voice: str | None,
        lang: str | None,
        speed: float | None,
    ) -> AsyncIterator[bytes]:
        payload = self._v1_payload(text=text, voice=voice, speed=speed, operation="submit")
        reqid = payload["request"]["reqid"]
        request_bytes = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        request_frame = bytes([0x11, 0x10, 0x11, 0x00]) + len(request_bytes).to_bytes(4, "big") + request_bytes
        try:
            async with websockets.connect(
                DOUBAO_V1_WS_TTS_URL,
                **self._websocket_connect_kwargs(headers={"Authorization": f"Bearer;{self.access_token}"}),
            ) as websocket:
                await websocket.send(request_frame)
                while True:
                    message = await asyncio.wait_for(websocket.recv(), timeout=float(TTS_TIMEOUT_SECONDS))
                    if isinstance(message, str):
                        raise SpeechProviderRuntimeError(
                            "Doubao V1 stream returned unexpected text frame",
                            response_text=_truncate(message),
                        )
                    parsed = self._parse_v1_binary_response(message)
                    if parsed["type"] == "audio":
                        if parsed["audio"]:
                            yield parsed["audio"]
                        if parsed.get("is_last"):
                            break
                    elif parsed["type"] == "error":
                        raise SpeechProviderRuntimeError(
                            parsed.get("message") or "Doubao V1 stream error",
                            status_code=parsed.get("code"),
                            response_text=_truncate(parsed.get("raw")),
                        )
        except Exception as exc:
            if isinstance(exc, SpeechProviderRuntimeError):
                raise
            raise SpeechProviderRuntimeError(
                f"Doubao V1 TTS stream failed (reqid={reqid}): {exc}",
                exception_repr=repr(exc),
            ) from exc

    def _parse_v1_binary_response(self, response: bytes) -> dict[str, Any]:
        if len(response) < 4:
            return {"type": "error", "message": "WebSocket response too short", "raw": response.hex()}
        header_size = response[0] & 0x0F
        message_type = response[1] >> 4
        flags = response[1] & 0x0F
        compression = response[2] & 0x0F
        payload = response[header_size * 4 :]
        if message_type == 0xB:
            if flags == 0:
                return {"type": "audio", "audio": b"", "is_last": False}
            if len(payload) < 8:
                return {"type": "error", "message": "Audio response payload too short", "raw": response.hex()}
            sequence = _read_int32(payload[:4])
            payload_size = _read_uint32(payload[4:8])
            return {
                "type": "audio",
                "audio": payload[8 : 8 + payload_size],
                "sequence": sequence,
                "is_last": sequence < 0,
            }
        if message_type == 0xF:
            code = None
            raw = payload
            if len(raw) >= 8:
                code = _read_uint32(raw[:4])
                raw = raw[8:]
            if compression == 1:
                raw = gzip.decompress(raw)
            message = raw.decode("utf-8", errors="replace")
            return {"type": "error", "code": code, "message": message, "raw": message}
        return {"type": "metadata", "raw": response.hex()}
