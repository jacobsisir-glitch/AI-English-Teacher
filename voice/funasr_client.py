from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import websockets
from websockets import ClientConnection
from websockets.exceptions import ConnectionClosed

from config import (
    FUNASR_CHUNK_INTERVAL,
    FUNASR_CHUNK_SIZE,
    FUNASR_FALLBACK_MIN_STABLE_MS,
    FUNASR_FINAL_DRAIN_MS,
    FUNASR_LATE_FINAL_GRACE_MS,
    FUNASR_FINAL_RESCUE_WAIT_MS,
    FUNASR_FINAL_WAIT_FALLBACK_MS,
    FUNASR_FINAL_WAIT_OFFLINE_MS,
)
from voice.session_state import structured_voice_log


AsyncEventCallback = Callable[..., Awaitable[None] | None]


@dataclass
class FunASRTranscriptEvent:
    utterance_id: str
    text: str
    is_final: bool
    raw: dict[str, Any]
    source: str = ""


class FunASRClient:
    def __init__(
        self,
        *,
        ws_url: str,
        mode: str = "2pass",
        model_name: str = "",
        sample_rate: int = 16000,
        chunk_size: list[int] | None = None,
        chunk_interval: int = FUNASR_CHUNK_INTERVAL,
        final_wait_offline_ms: int = FUNASR_FINAL_WAIT_OFFLINE_MS,
        final_wait_fallback_ms: int = FUNASR_FINAL_WAIT_FALLBACK_MS,
        final_drain_window_ms: int = FUNASR_FINAL_DRAIN_MS,
        final_rescue_wait_ms: int = FUNASR_FINAL_RESCUE_WAIT_MS,
        late_final_grace_ms: int = FUNASR_LATE_FINAL_GRACE_MS,
        fallback_min_stable_ms: int = FUNASR_FALLBACK_MIN_STABLE_MS,
        on_partial: AsyncEventCallback | None = None,
        on_final: AsyncEventCallback | None = None,
        on_state: AsyncEventCallback | None = None,
        log_context_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.mode = mode
        self.model_name = model_name
        self.sample_rate = sample_rate
        self.chunk_size = list(chunk_size or FUNASR_CHUNK_SIZE)
        self.chunk_interval = chunk_interval
        self.final_wait_offline_ms = final_wait_offline_ms
        self.final_wait_fallback_ms = final_wait_fallback_ms
        self.final_drain_window_ms = final_drain_window_ms
        self.final_rescue_wait_ms = final_rescue_wait_ms
        self.late_final_grace_ms = late_final_grace_ms
        self.fallback_min_stable_ms = fallback_min_stable_ms
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_state = on_state
        self.log_context_provider = log_context_provider
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._current_utterance_id = ""
        self._last_finished_utterance_id = ""
        self._current_chunks: list[bytes] = []
        self._current_final_future: asyncio.Future[FunASRTranscriptEvent | None] | None = None
        self._first_message_sent = False
        self._first_message_received = False
        self._last_partial_event: FunASRTranscriptEvent | None = None
        self._last_partial_received_monotonic = 0.0
        self._completed_finals: dict[str, str] = {}
        self._drain_utterance_id = ""
        self._drain_until_monotonic = 0.0
        self._late_timeout_recoveries: dict[str, float] = {}

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._recv_task is not None and not self._recv_task.done()

    async def ensure_connected(self) -> None:
        if self.is_connected:
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
                "ping_interval": None,
                "ping_timeout": None,
                "close_timeout": 5,
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
        self._prune_late_timeout_recoveries()
        self._close_active_drain_window(outcome="new_utterance_started")
        self._current_utterance_id = utterance_id
        self._last_finished_utterance_id = ""
        self._current_chunks = []
        self._current_final_future = asyncio.get_running_loop().create_future()
        self._last_partial_event = None
        self._last_partial_received_monotonic = 0.0
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

    def _compute_dynamic_wait(self) -> tuple[float, float, float, float]:
        """Compute wait times based on utterance audio duration.

        Longer Chinese/English sentences get more time for the offline final.
        """
        total_ms = self._estimate_current_audio_ms()
        configured_offline_s = max(self.final_wait_offline_ms, 0) / 1000
        if total_ms < 2000:
            offline_s = max(3.0, configured_offline_s)
        elif total_ms < 5000:
            offline_s = max(5.0, configured_offline_s)
        elif total_ms < 8000:
            offline_s = max(6.0, configured_offline_s)
        else:
            offline_s = max(8.0, configured_offline_s)
        fallback_s = 0.0
        drain_s = max(max(self.final_drain_window_ms, 0) / 1000, 1.0)
        # Rescue: wait longer if partial text looks unreliable for the audio duration
        rescue_s = 0.0
        partial_text = self._last_partial_text(self._current_utterance_id)
        if not partial_text and total_ms >= 2000:
            rescue_s = min(3.0, max(self.final_rescue_wait_ms, 0) / 1000)
        elif partial_text and total_ms >= 5000:
            # Long utterance with short partial — likely incomplete, wait more
            if len(partial_text) < 10:
                rescue_s = min(1.5, max(self.final_rescue_wait_ms, 0) / 2000)
        self._log(
            "funasr.dynamic_wait",
            utteranceId=self._current_utterance_id,
            total_audio_ms=total_ms,
            offline_wait_s=offline_s,
            fallback_wait_s=fallback_s,
            drain_window_s=drain_s,
            rescue_wait_s=rescue_s,
        )
        return offline_s, fallback_s, drain_s, rescue_s

    async def finish_utterance(self, *, timeout_s: float = 16.0) -> FunASRTranscriptEvent | None:
        if not self._current_utterance_id:
            return None

        utterance_id = self._current_utterance_id
        preferred_wait_s, fallback_wait_s, drain_window_s, rescue_wait_s = self._compute_dynamic_wait()
        self._log(
            "funasr.final_timeout.begin",
            utteranceId=utterance_id,
            timeout_s=timeout_s,
            preferred_wait_s=preferred_wait_s,
            fallback_wait_s=fallback_wait_s,
            drain_window_s=drain_window_s,
            rescue_wait_s=rescue_wait_s,
        )
        await self._send_json({"is_speaking": False})
        future = self._current_final_future
        try:
            if future is None:
                self._log("funasr.final_timeout.end", utteranceId=utterance_id, outcome="missing_future")
                return None
            if preferred_wait_s > 0:
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), timeout=preferred_wait_s)
                    self._log(
                        "funasr.final_timeout.end",
                        utteranceId=utterance_id,
                        outcome="final_received",
                        wait_stage="offline_preferred",
                    )
                    return result
                except asyncio.TimeoutError:
                    self._log(
                        "funasr.final_wait.stage_timeout",
                        utteranceId=utterance_id,
                        wait_stage="offline_preferred",
                        waited_ms=int(preferred_wait_s * 1000),
                    )

            if fallback_wait_s > 0:
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), timeout=fallback_wait_s)
                    self._log(
                        "funasr.final_timeout.end",
                        utteranceId=utterance_id,
                        outcome="final_received",
                        wait_stage="fallback_grace",
                    )
                    return result
                except asyncio.TimeoutError:
                    self._log(
                        "funasr.final_wait.stage_timeout",
                        utteranceId=utterance_id,
                        wait_stage="fallback_grace",
                        waited_ms=int(fallback_wait_s * 1000),
                    )

            drain_outcome = "skipped"
            if drain_window_s > 0:
                self._open_drain_window(utterance_id=utterance_id, drain_window_s=drain_window_s)
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), timeout=drain_window_s)
                    self._log(
                        "funasr.final_timeout.end",
                        utteranceId=utterance_id,
                        outcome="final_received",
                        wait_stage="drain_window",
                    )
                    drain_outcome = "late_final_received"
                    self._close_drain_window(utterance_id=utterance_id, outcome=drain_outcome)
                    return result
                except asyncio.TimeoutError:
                    drain_outcome = "expired"

            self._log("funasr.final_timeout.end", utteranceId=utterance_id, outcome="timeout")
            await self._emit_state("funasr.final_timeout", utteranceId=utterance_id)
            fallback_event = self._build_timeout_fallback_final(utterance_id)
            if fallback_event is None:
                fallback_event = self._build_last_chance_partial_final(utterance_id)
            if fallback_event is None and rescue_wait_s > 0 and self._should_wait_longer_for_server_final(utterance_id):
                self._log(
                    "funasr.final_wait.rescue_begin",
                    utteranceId=utterance_id,
                    rescue_wait_ms=int(rescue_wait_s * 1000),
                    total_audio_ms=self._estimate_current_audio_ms(),
                    partial_text_length=len(self._last_partial_text(utterance_id)),
                )
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), timeout=rescue_wait_s)
                    self._log(
                        "funasr.final_timeout.end",
                        utteranceId=utterance_id,
                        outcome="final_received",
                        wait_stage="server_final_rescue",
                    )
                    self._close_drain_window(utterance_id=utterance_id, outcome="rescue_final_received")
                    return result
                except asyncio.TimeoutError:
                    self._log(
                        "funasr.final_wait.stage_timeout",
                        utteranceId=utterance_id,
                        wait_stage="server_final_rescue",
                        waited_ms=int(rescue_wait_s * 1000),
                    )
            if fallback_event is not None:
                if not self._mark_finalized(fallback_event.utterance_id, fallback_event.source):
                    self._log(
                        "funasr.final.duplicate_ignored",
                        utteranceId=fallback_event.utterance_id,
                        final_source=fallback_event.source,
                    )
                    return None
                self._log(
                    "funasr.final.detected",
                    utteranceId=utterance_id,
                    detection_source=fallback_event.source,
                )
                self._log(
                    "funasr.final.publish",
                    utteranceId=utterance_id,
                    text=fallback_event.text,
                    publish_source=fallback_event.source,
                )
                await self._emit(self.on_final, fallback_event)
                drain_outcome = "late_partial_fallback"
                if fallback_event.source == "timeout_partial_last_chance":
                    drain_outcome = "late_partial_last_chance"
                self._close_drain_window(utterance_id=utterance_id, outcome=drain_outcome)
                return fallback_event
            self._register_late_timeout_recovery(utterance_id)
            self._close_drain_window(utterance_id=utterance_id, outcome=drain_outcome)
            return None
        finally:
            self._close_drain_window(utterance_id=utterance_id, outcome="finalized")
            self._last_finished_utterance_id = utterance_id
            # Keep _current_utterance_id until next start_utterance so late offline finals
            # can still be matched via explicit wav_name. _last_finished_utterance_id
            # is used as secondary match target for late/drain/recovery finals.
            self._current_chunks = []
            self._current_final_future = None
            self._last_partial_event = None
            self._last_partial_received_monotonic = 0.0

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
                self._log("funasr.message.received.raw_brief", raw=message[:200])
                await self._emit_state("funasr.message.received", raw=message[:500])
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    self._log("funasr.error", stage="recv_loop.decode", error="json decode failed")
                    self._log("funasr.message.ignored.reason", reason="json_decode_failed", raw=message[:200])
                    continue
                await self._handle_server_message(payload)
        except ConnectionClosed:
            self._ws = None
            if not self._closed:
                self._log("funasr.connection_closed", utteranceId=self._current_utterance_id)
                await self._emit_state("funasr.disconnected", utteranceId=self._current_utterance_id)
        except Exception as exc:
            self._log("funasr.error", stage="recv_loop", error=str(exc))
            await self._emit_state("funasr.error", stage="recv_loop", error=str(exc))

    @staticmethod
    def _extract_wav_name(payload: dict[str, Any]) -> str:
        """Extract wav_name from FunASR response, trying multiple known field names."""
        for key in ("wav_name", "wavName", "name", "utt_id", "uttId", "utterance_id", "utteranceId"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    async def _handle_server_message(self, payload: dict[str, Any]) -> None:
        text = self._extract_text(payload)
        mode = str(payload.get("mode") or payload.get("type") or "").lower()
        raw_wav_name = self._extract_wav_name(payload)
        is_final = self._is_final_message(payload, mode)
        event_source = self._resolve_event_source(is_final=is_final, mode=mode, payload=payload)
        is_offline_final = is_final and event_source == "2pass-offline"
        is_online = not is_final
        current_uid = self._current_utterance_id
        last_finished_uid = self._last_finished_utterance_id
        drain_uid = self._drain_utterance_id if self._drain_until_monotonic > time.monotonic() else ""

        # ── wav_name resolution ───────────────────────────────────────
        # Online partials without explicit wav_name can safely fall back to
        # current_utterance_id (they arrive while the utterance is active).
        # Finals and late messages MUST carry an explicit wav_name to avoid
        # 串句 (cross-utterance confusion) when a new utterance has started.
        if raw_wav_name:
            wav_name = raw_wav_name
        elif is_online and current_uid:
            wav_name = current_uid
        else:
            # Final/drain/recovery without explicit wav_name → untrusted
            self._log(
                "funasr.message.ignored.reason",
                reason="final_missing_wav_name",
                mode=mode,
                is_final=is_final,
                current_uid=current_uid,
            )
            if is_final:
                self._log(
                    "funasr.final.utterance_mismatch",
                    wav_name="",
                    current_uid=current_uid,
                    text=text,
                    source=event_source,
                )
            return

        # Determine match targets
        wav_matches_current = wav_name == current_uid if current_uid else False
        wav_matches_last_finished = wav_name == last_finished_uid if last_finished_uid else False
        drain_active = drain_uid and wav_name == drain_uid
        late_recovery_active = is_final and self._is_late_timeout_recovery_active(wav_name)

        self._log("funasr.message.mode", mode=mode)
        self._log("funasr.message.is_final", is_final=is_final)
        self._log("funasr.message.text", text=text)
        self._log("funasr.message.wav_name", wav_name=wav_name)
        self._log("funasr.message.source", source=event_source)

        # ── routing: determine if this message should be accepted ─────
        already_finalized = wav_name in self._completed_finals
        if already_finalized and not is_offline_final:
            self._log(
                "funasr.message.ignored",
                utteranceId=wav_name,
                reason="utterance_already_finalized",
                message_mode=mode,
                event_source=event_source,
            )
            self._log("funasr.message.ignored.reason", reason="utterance_already_finalized", wav_name=wav_name, mode=mode)
            return
        if already_finalized and is_offline_final:
            self._log(
                "funasr.offline_final.override",
                utteranceId=wav_name,
                reason="offline_final_overrides_earlier_final",
                previous_source=self._completed_finals.get(wav_name),
            )

        # Accept if: wav matches current, OR matches last finished, OR drain active, OR late recovery
        accepted = wav_matches_current or wav_matches_last_finished or drain_active or late_recovery_active
        if not accepted:
            self._log(
                "funasr.message.ignored",
                utteranceId=wav_name,
                current_utterance_id=current_uid,
                last_finished_uid=last_finished_uid,
                reason="utterance_mismatch",
                message_mode=mode,
                drain_active=drain_active,
                late_recovery_active=late_recovery_active,
            )
            self._log("funasr.message.ignored.reason", reason="utterance_mismatch",
                      wav_name=wav_name, current_uid=current_uid, mode=mode)
            if is_final:
                self._log("funasr.final.utterance_mismatch", wav_name=wav_name, current_uid=current_uid,
                          text=text, source=event_source)
            return

        if is_final and not text and self._last_partial_event is not None and self._last_partial_event.utterance_id == wav_name:
            text = self._last_partial_event.text

        if not text:
            return

        resolved_uid = wav_name
        event = FunASRTranscriptEvent(
            utterance_id=resolved_uid,
            text=text,
            is_final=is_final,
            raw=payload,
            source=event_source,
        )
        if is_final:
            if late_recovery_active:
                self._log(
                    "late_timeout_final.accepted",
                    utteranceId=event.utterance_id,
                    source=event.source,
                )
            if drain_active:
                self._log("late_final.accepted", utteranceId=event.utterance_id, source=event.source)
            if not already_finalized:
                self._mark_finalized(event.utterance_id, event.source)
            else:
                self._completed_finals[event.utterance_id] = event.source
            self._log("funasr.final.detected", utteranceId=event.utterance_id, detection_source=event.source)
            self._log("funasr.final.received", utteranceId=event.utterance_id, text=event.text, source=event.source)
            self._log("funasr.final", utteranceId=event.utterance_id, text=event.text)
            if self._current_final_future and not self._current_final_future.done():
                self._current_final_future.set_result(event)
            self._log("funasr.final.publish", utteranceId=event.utterance_id, text=event.text, publish_source=event.source)
            await self._emit(self.on_final, event)
        else:
            if drain_active:
                self._log("late_partial.accepted", utteranceId=event.utterance_id, text=event.text)
            self._last_partial_event = event
            self._last_partial_received_monotonic = time.monotonic()
            self._log("funasr.partial", utteranceId=event.utterance_id, text=event.text)
            await self._emit(self.on_partial, event)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        await self.ensure_connected()
        assert self._ws is not None
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except ConnectionClosed:
            self._log("funasr.send_json.connection_closed", payload=str(payload)[:200])
            self._ws = None
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
            "chunk_size": self.chunk_size,
            "chunk_interval": self.chunk_interval,
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
        total_audio_ms = self._estimate_current_audio_ms()
        partial_text = self._last_partial_text(utterance_id)
        if self._is_partial_too_short_for_audio_duration(total_audio_ms=total_audio_ms, partial_text=partial_text):
            self._log(
                "funasr.final.fallback_skipped",
                utteranceId=utterance_id,
                reason="partial_too_short_for_audio_duration",
                total_audio_ms=total_audio_ms,
                text_length=len(partial_text),
            )
            return None
        if self.fallback_min_stable_ms > 0 and self._last_partial_received_monotonic > 0:
            stable_ms = int((time.monotonic() - self._last_partial_received_monotonic) * 1000)
            if stable_ms < self.fallback_min_stable_ms:
                self._log(
                    "funasr.final.fallback_skipped",
                    utteranceId=utterance_id,
                    reason="partial_not_stable",
                    stable_ms=stable_ms,
                    required_stable_ms=self.fallback_min_stable_ms,
                )
                return None
        return FunASRTranscriptEvent(
            utterance_id=utterance_id,
            text=self._last_partial_event.text,
            is_final=True,
            raw={**self._last_partial_event.raw, "fallback_final": True},
            source="timeout_partial_fallback",
        )

    def _build_last_chance_partial_final(self, utterance_id: str) -> FunASRTranscriptEvent | None:
        if self._last_partial_event is None:
            return None
        if self._last_partial_event.utterance_id != utterance_id:
            return None
        if not self._last_partial_event.text.strip():
            return None
        total_audio_ms = self._estimate_current_audio_ms()
        partial_text = self._last_partial_text(utterance_id)
        if self._is_partial_too_short_for_audio_duration(total_audio_ms=total_audio_ms, partial_text=partial_text):
            self._log(
                "funasr.final.last_chance_skipped",
                utteranceId=utterance_id,
                reason="partial_too_short_for_audio_duration",
                total_audio_ms=total_audio_ms,
                text_length=len(partial_text),
            )
            return None
        self._log(
            "funasr.final.last_chance_partial",
            utteranceId=utterance_id,
            text=self._last_partial_event.text,
        )
        return FunASRTranscriptEvent(
            utterance_id=utterance_id,
            text=self._last_partial_event.text,
            is_final=True,
            raw={**self._last_partial_event.raw, "fallback_final": True, "last_chance_partial_final": True},
            source="timeout_partial_last_chance",
        )

    def _mark_finalized(self, utterance_id: str, source: str) -> bool:
        self._prune_completed_finals()
        if utterance_id in self._completed_finals:
            return False
        self._completed_finals[utterance_id] = source
        self._late_timeout_recoveries.pop(utterance_id, None)
        return True

    def _open_drain_window(self, *, utterance_id: str, drain_window_s: float) -> None:
        self._drain_utterance_id = utterance_id
        self._drain_until_monotonic = time.monotonic() + drain_window_s
        self._log(
            "utterance.drain_window.begin",
            utteranceId=utterance_id,
            drain_window_ms=int(drain_window_s * 1000),
        )

    def _close_drain_window(self, *, utterance_id: str, outcome: str) -> None:
        if self._drain_utterance_id != utterance_id:
            return
        self._log("utterance.drain_window.end", utteranceId=utterance_id, outcome=outcome)
        self._drain_utterance_id = ""
        self._drain_until_monotonic = 0.0

    def _close_active_drain_window(self, *, outcome: str) -> None:
        if not self._drain_utterance_id:
            return
        self._close_drain_window(utterance_id=self._drain_utterance_id, outcome=outcome)

    def _is_drain_window_active(self, utterance_id: str) -> bool:
        return (
            self._drain_utterance_id == utterance_id
            and self._drain_until_monotonic > time.monotonic()
        )

    def _prune_completed_finals(self) -> None:
        if len(self._completed_finals) <= 32:
            return
        keep_items = list(self._completed_finals.items())[-16:]
        self._completed_finals = dict(keep_items)

    def _register_late_timeout_recovery(self, utterance_id: str) -> None:
        if self.late_final_grace_ms <= 0:
            return
        self._late_timeout_recoveries[utterance_id] = time.monotonic() + (self.late_final_grace_ms / 1000)
        self._log(
            "funasr.late_final_recovery.begin",
            utteranceId=utterance_id,
            recovery_window_ms=self.late_final_grace_ms,
        )
        self._prune_late_timeout_recoveries()

    def _is_late_timeout_recovery_active(self, utterance_id: str) -> bool:
        if not utterance_id:
            return False
        deadline = self._late_timeout_recoveries.get(utterance_id)
        if deadline is None:
            return False
        if deadline <= time.monotonic():
            self._late_timeout_recoveries.pop(utterance_id, None)
            return False
        return True

    def _prune_late_timeout_recoveries(self) -> None:
        if not self._late_timeout_recoveries:
            return
        now = time.monotonic()
        expired = [utterance_id for utterance_id, deadline in self._late_timeout_recoveries.items() if deadline <= now]
        for utterance_id in expired:
            self._late_timeout_recoveries.pop(utterance_id, None)

    def _estimate_current_audio_ms(self) -> int:
        total_bytes = sum(len(chunk) for chunk in self._current_chunks)
        bytes_per_ms = (self.sample_rate * 2) / 1000
        if bytes_per_ms <= 0:
            return 0
        return int(total_bytes / bytes_per_ms)

    def _last_partial_text(self, utterance_id: str) -> str:
        if self._last_partial_event is None or self._last_partial_event.utterance_id != utterance_id:
            return ""
        return self._last_partial_event.text.strip()

    @staticmethod
    def _is_partial_too_short_for_audio_duration(*, total_audio_ms: int, partial_text: str) -> bool:
        text_length = len(partial_text)
        if total_audio_ms >= 1200 and text_length < 2:
            return True
        if total_audio_ms >= 1800 and text_length < 3:
            return True
        if total_audio_ms >= 2600 and text_length < 5:
            return True
        return False

    def _should_wait_longer_for_server_final(self, utterance_id: str) -> bool:
        total_audio_ms = self._estimate_current_audio_ms()
        partial_text = self._last_partial_text(utterance_id)
        if total_audio_ms < 1200:
            return False
        if not partial_text:
            return True
        return self._is_partial_too_short_for_audio_duration(
            total_audio_ms=total_audio_ms,
            partial_text=partial_text,
        )

    @staticmethod
    def _resolve_event_source(*, is_final: bool, mode: str, payload: dict[str, Any]) -> str:
        if not is_final:
            if "2pass-online" in mode:
                return "2pass-online"
            return "partial"
        if "2pass-offline" in mode:
            return "2pass-offline"
        if payload.get("is_final") or payload.get("final") or payload.get("sentence_end"):
            return "server_final_flag"
        if "offline" in mode:
            return "offline_mode"
        return "server_final"

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
