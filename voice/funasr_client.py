from __future__ import annotations

import asyncio
import json
import ssl
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import websockets
from websockets import ClientConnection
from websockets.exceptions import ConnectionClosed

from voice.session_state import structured_voice_log


AsyncEventCallback = Callable[..., Awaitable[None] | None]


@dataclass
class FunASRTranscriptEvent:
    utterance_id: str
    text: str
    is_final: bool
    raw: dict[str, Any]


class FunASRClient:
    def __init__(
        self,
        *,
        ws_url: str,
        mode: str = "2pass",
        model_name: str = "",
        sample_rate: int = 16000,
        on_partial: AsyncEventCallback | None = None,
        on_final: AsyncEventCallback | None = None,
        on_state: AsyncEventCallback | None = None,
        log_context_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.mode = mode
        self.model_name = model_name
        self.sample_rate = sample_rate
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_state = on_state
        self.log_context_provider = log_context_provider
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._current_utterance_id = ""
        self._current_chunks: list[bytes] = []
        self._current_final_future: asyncio.Future[FunASRTranscriptEvent | None] | None = None
        self._first_message_sent = False
        self._first_message_received = False
        self._last_partial_event: FunASRTranscriptEvent | None = None

    async def ensure_connected(self) -> None:
        if self._ws is not None and self._recv_task is not None and not self._recv_task.done():
            return
        parsed_url = urlparse(self.ws_url)
        ssl_context = self._build_ssl_context(parsed_url.scheme, parsed_url.hostname or "")
        self._log(
            "funasr.ws.connect.begin",
            ws_url=self.ws_url,
            ws_scheme=parsed_url.scheme,
            ssl_enabled=ssl_context is not None,
        )
        try:
            connect_kwargs: dict[str, Any] = {
                "max_size": None,
                "ping_interval": 20,
                "ping_timeout": 20,
                "subprotocols": ["binary"],
            }
            if parsed_url.scheme == "wss":
                connect_kwargs["ssl"] = ssl_context
            self._ws = await websockets.connect(self.ws_url, **connect_kwargs)
            self._recv_task = asyncio.create_task(self._recv_loop())
            self._first_message_sent = False
            self._first_message_received = False
            self._log(
                "funasr.ws.connect.success",
                ws_url=self.ws_url,
                ws_scheme=parsed_url.scheme,
                ssl_enabled=ssl_context is not None,
                selected_subprotocol=getattr(self._ws, "subprotocol", ""),
            )
            await self._emit_state("funasr.connected")
        except Exception as exc:
            self._log(
                "funasr.ws.connect.exception_type",
                ws_url=self.ws_url,
                ws_scheme=parsed_url.scheme,
                exception_type=type(exc).__name__,
            )
            self._log(
                "funasr.ws.connect.exception_repr",
                ws_url=self.ws_url,
                ws_scheme=parsed_url.scheme,
                exception_repr=repr(exc),
            )
            self._log("funasr.error", stage="ensure_connected", error=str(exc))
            await self._emit_state("funasr.error", stage="ensure_connected", error=str(exc))
            raise

    async def close(self) -> None:
        self._log("funasr.close.called", utteranceId=self._current_utterance_id)
        self._closed = True
        try:
            if self._ws is not None:
                await self._ws.close()
            if self._recv_task is not None:
                await asyncio.gather(self._recv_task, return_exceptions=True)
        finally:
            self._ws = None
            self._recv_task = None
            self._log("funasr.close.result", utteranceId=self._current_utterance_id)

    async def start_utterance(self, utterance_id: str, *, initial_pcm: bytes = b"") -> None:
        self._current_utterance_id = utterance_id
        self._current_chunks = []
        self._current_final_future = asyncio.get_running_loop().create_future()
        self._last_partial_event = None
        await self.ensure_connected()
        await self._send_json(self._build_start_payload(utterance_id))
        if initial_pcm:
            await self.send_audio_chunk(initial_pcm)

    async def send_audio_chunk(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._current_chunks.append(bytes(pcm))
        try:
            await self.ensure_connected()
            assert self._ws is not None
            await self._ws.send(pcm)
        except ConnectionClosed:
            await self._recover_and_replay()
        except Exception as exc:
            self._log("funasr.error", stage="send_audio_chunk", error=str(exc))
            await self._emit_state("funasr.error", stage="send_audio_chunk", error=str(exc))
            raise

    async def finish_utterance(self, *, timeout_s: float = 8.0) -> FunASRTranscriptEvent | None:
        if not self._current_utterance_id:
            return None

        utterance_id = self._current_utterance_id
        self._log("funasr.final_timeout.begin", utteranceId=utterance_id, timeout_s=timeout_s)
        await self._send_json({"is_speaking": False})
        future = self._current_final_future
        try:
            if future is None:
                self._log("funasr.final_timeout.end", utteranceId=utterance_id, outcome="missing_future")
                return None
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
            self._log("funasr.final_timeout.end", utteranceId=utterance_id, outcome="final_received")
            return result
        except asyncio.TimeoutError:
            self._log("funasr.final_timeout.end", utteranceId=utterance_id, outcome="timeout")
            await self._emit_state("funasr.final_timeout", utteranceId=utterance_id)
            fallback_event = self._build_timeout_fallback_final(utterance_id)
            if fallback_event is not None:
                self._log("funasr.final.detected", utteranceId=utterance_id, detection_source="timeout_partial_fallback")
                self._log("funasr.final.publish", utteranceId=utterance_id, text=fallback_event.text, publish_source="timeout_partial_fallback")
                await self._emit(self.on_final, fallback_event)
                return fallback_event
            return None
        finally:
            self._current_utterance_id = ""
            self._current_chunks = []
            self._current_final_future = None
            self._last_partial_event = None

    async def _recover_and_replay(self) -> None:
        await self._emit_state("funasr.reconnecting", utteranceId=self._current_utterance_id)
        if self._ws is not None:
            await self._ws.close()
        self._ws = None
        if self._recv_task is not None:
            await asyncio.gather(self._recv_task, return_exceptions=True)
        self._recv_task = None
        await self.ensure_connected()
        if self._current_utterance_id:
            await self._send_json(self._build_start_payload(self._current_utterance_id))
            if self._current_chunks:
                assert self._ws is not None
                for chunk in self._current_chunks:
                    await self._ws.send(chunk)

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if not isinstance(message, str):
                    continue
                if not self._first_message_received:
                    self._first_message_received = True
                    self._log("funasr.ws.first_message_received", raw=message[:500])
                self._log("funasr.ws.message.received", raw=message[:500])
                await self._emit_state("funasr.message.received", raw=message[:500])
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    self._log("funasr.error", stage="recv_loop.decode", error="json decode failed")
                    continue
                await self._handle_server_message(payload)
        except ConnectionClosed:
            if not self._closed:
                await self._emit_state("funasr.disconnected", utteranceId=self._current_utterance_id)
        except Exception as exc:
            self._log("funasr.error", stage="recv_loop", error=str(exc))
            await self._emit_state("funasr.error", stage="recv_loop", error=str(exc))

    async def _handle_server_message(self, payload: dict[str, Any]) -> None:
        text = self._extract_text(payload)
        mode = str(payload.get("mode") or payload.get("type") or "").lower()
        wav_name = str(payload.get("wav_name") or self._current_utterance_id or "")
        is_final = self._is_final_message(payload, mode)
        self._log("funasr.message.mode", mode=mode)
        self._log("funasr.message.is_final", is_final=is_final)
        self._log("funasr.message.text", text=text)
        self._log("funasr.message.wav_name", wav_name=wav_name)

        if is_final and not text and self._last_partial_event is not None and self._last_partial_event.utterance_id == wav_name:
            text = self._last_partial_event.text

        if not text:
            return

        event = FunASRTranscriptEvent(
            utterance_id=wav_name,
            text=text,
            is_final=is_final,
            raw=payload,
        )
        if is_final:
            self._log("funasr.final.detected", utteranceId=event.utterance_id, detection_source="server_message")
            self._log("funasr.final", utteranceId=event.utterance_id, text=event.text)
            if self._current_final_future and not self._current_final_future.done():
                self._current_final_future.set_result(event)
            self._log("funasr.final.publish", utteranceId=event.utterance_id, text=event.text, publish_source="server_message")
            await self._emit(self.on_final, event)
        else:
            self._last_partial_event = event
            self._log("funasr.partial", utteranceId=event.utterance_id, text=event.text)
            await self._emit(self.on_partial, event)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        await self.ensure_connected()
        assert self._ws is not None
        await self._ws.send(json.dumps(payload, ensure_ascii=False))
        if not self._first_message_sent:
            self._first_message_sent = True
            self._log("funasr.ws.first_message_sent", payload=payload)

    async def _emit_state(self, state: str, **payload: Any) -> None:
        await self._emit(self.on_state, state, payload)

    async def _emit(self, callback: AsyncEventCallback | None, *args: Any) -> None:
        if callback is None:
            return
        result = callback(*args)
        if asyncio.iscoroutine(result):
            await result

    def _log(self, event: str, **payload: Any) -> None:
        context = self.log_context_provider() if self.log_context_provider is not None else {}
        structured_voice_log(event, **context, **payload)

    def _build_start_payload(self, utterance_id: str) -> dict[str, Any]:
        payload = {
            "mode": self.mode,
            "wav_name": utterance_id,
            "wav_format": "pcm",
            "audio_fs": self.sample_rate,
            "is_speaking": True,
            "chunk_size": [5, 10, 5],
            "chunk_interval": 10,
            "encoder_chunk_look_back": 4,
            "decoder_chunk_look_back": 1,
            "itn": True,
            "hotwords": "",
        }
        if self.model_name:
            payload["model"] = self.model_name
        return payload

    def _build_timeout_fallback_final(self, utterance_id: str) -> FunASRTranscriptEvent | None:
        if self._last_partial_event is None:
            return None
        if self._last_partial_event.utterance_id != utterance_id:
            return None
        if not self._last_partial_event.text.strip():
            return None
        return FunASRTranscriptEvent(
            utterance_id=utterance_id,
            text=self._last_partial_event.text,
            is_final=True,
            raw={**self._last_partial_event.raw, "fallback_final": True},
        )

    @staticmethod
    def _is_final_message(payload: dict[str, Any], mode: str) -> bool:
        if "2pass-online" in mode:
            return False
        if "2pass-offline" in mode:
            return True
        return bool(
            payload.get("is_final")
            or payload.get("final")
            or payload.get("sentence_end")
            or "offline" in mode
            or "final" in mode
        )

    @staticmethod
    def _build_ssl_context(scheme: str, hostname: str) -> ssl.SSLContext | None:
        if scheme != "wss":
            return None
        if hostname in {"127.0.0.1", "localhost", "::1"}:
            ssl_context = ssl._create_unverified_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            return ssl_context
        return ssl.create_default_context()

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        text = payload.get("text")
        if isinstance(text, str):
            return text.strip()
        if isinstance(text, list):
            return "".join(str(item) for item in text).strip()
        result = payload.get("result")
        if isinstance(result, dict):
            nested_text = result.get("text")
            if isinstance(nested_text, str):
                return nested_text.strip()
            if isinstance(nested_text, list):
                return "".join(str(item) for item in nested_text).strip()
        if isinstance(result, list):
            pieces: list[str] = []
            for item in result:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    pieces.append(item["text"])
                elif isinstance(item, str):
                    pieces.append(item)
            if pieces:
                return "".join(pieces).strip()
        if isinstance(payload.get("result"), str):
            return str(payload["result"]).strip()
        if isinstance(payload.get("sentence"), str):
            return str(payload["sentence"]).strip()
        return ""
