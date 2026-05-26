from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from livekit import rtc
from livekit.rtc._proto.track_pb2 import TrackKind, TrackSource

from config import (
    FUNASR_MODE,
    FUNASR_MODEL_NAME,
    FUNASR_WS_URL,
    LIVEKIT_WS_URL,
    SILERO_CHANNELS,
    SILERO_MIN_SILENCE_MS,
    SILERO_MIN_SPEECH_MS,
    SILERO_PRE_SPEECH_MS,
    SILERO_SAMPLE_RATE,
    SILERO_SPEECH_END_HOLD_MS,
    SILERO_VAD_THRESHOLD,
    VOICE_FINAL_ACK_RETRY,
    VOICE_FINAL_ACK_TIMEOUT_MS,
)
from livekit_utils import create_livekit_worker_token, livekit_voice_stack_is_configured
from voice.audio_buffer import LiveKitAudioNormalizer
from voice.funasr_client import FunASRClient, FunASRTranscriptEvent
from voice.session_state import (
    ParticipantSessionState,
    RoomSessionState,
    UtteranceTelemetry,
    structured_voice_log,
)
from voice.partial_accumulator import PartialAccumulator
from voice.transcript_publisher import LiveKitTranscriptPublisher
from voice.utterance_manager import CommitContext, CommitGate, UtteranceManager
from voice.vad_controller import SileroVADController


class ParticipantVoiceProcessor:
    def __init__(
        self,
        *,
        room_name: str,
        room_id_getter,
        room_state: RoomSessionState,
        room: rtc.Room,
        publisher: LiveKitTranscriptPublisher,
        participant: rtc.RemoteParticipant,
        publication: rtc.RemoteTrackPublication,
        track: rtc.RemoteAudioTrack,
        context_provider=None,
    ) -> None:
        self.room_name = room_name
        self.room_id_getter = room_id_getter
        self.room_state = room_state
        self.room = room
        self.publisher = publisher
        self.participant = participant
        self.publication = publication
        self.track = track
        self.context_provider = context_provider
        self.user_identity = participant.identity
        self.state = ParticipantSessionState(
            user_identity=self.user_identity,
            room_name=room_name,
            track_sid=publication.sid,
        )
        self.audio_stream: rtc.AudioStream | None = None
        self.audio_normalizer = LiveKitAudioNormalizer(
            target_sample_rate=SILERO_SAMPLE_RATE,
            target_channels=SILERO_CHANNELS,
        )
        self.vad = SileroVADController(
            sample_rate=SILERO_SAMPLE_RATE,
            threshold=SILERO_VAD_THRESHOLD,
            min_silence_duration_ms=SILERO_MIN_SILENCE_MS,
            min_speech_duration_ms=SILERO_MIN_SPEECH_MS,
            pre_speech_ms=SILERO_PRE_SPEECH_MS,
        )
        self.funasr = FunASRClient(
            ws_url=FUNASR_WS_URL,
            mode=FUNASR_MODE,
            model_name=FUNASR_MODEL_NAME,
            sample_rate=SILERO_SAMPLE_RATE,
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_state=self._on_funasr_state,
            log_context_provider=self._log_context,
        )
        self._task: asyncio.Task | None = None
        self._received_first_frame = False
        self._stop_reason = "participant_stop"
        self._vad_state = "idle"
        self._last_chunk_state_emit_monotonic = 0.0
        self._pending_finish_task: asyncio.Task | None = None
        self._pending_finish_started_monotonic = 0.0
        self._awaiting_final = False
        self._finishing_utterance_id = ""
        self._deferred_utterance_chunks: list[bytes] = []
        self._deferred_utterance_start_ts: float | None = None
        self._deferred_utterance_end_ts: float | None = None
        self._deferred_finish_requested = False
        self.utterance_mgr = UtteranceManager(
            room_name=room_name,
            participant_id=self.user_identity,
            log_context_provider=self._log_context,
        )
        self.partial_acc = PartialAccumulator(
            log_context_provider=self._log_context,
        )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"voice-user-{self.user_identity}")

    def _log_context(self) -> dict[str, object]:
        return {
            "room_id": self.room_id_getter(),
            "room_name": self.room_name,
            "user_identity": self.user_identity,
        }

    def _commit_context(self) -> CommitContext:
        raw_context = {}
        if self.context_provider is not None:
            try:
                raw_context = dict(self.context_provider() or {})
            except Exception as exc:
                structured_voice_log(
                    "transcript.commit.context_error",
                    **self._log_context(),
                    error=str(exc),
                )
        mode = str(raw_context.get("mode") or "normal_chat").strip() or "normal_chat"
        class_mode = bool(raw_context.get("class_mode") or mode == "class_mode")
        pending_question = bool(raw_context.get("pending_question"))
        short_answer_allowed = bool(raw_context.get("short_answer_allowed"))
        return CommitContext(
            mode="pending_question" if class_mode and pending_question else mode,
            class_mode=class_mode,
            pending_question=pending_question,
            pending_question_text=str(raw_context.get("pending_question_text") or ""),
            short_answer_allowed=short_answer_allowed,
        )

    def _utterance_duration_ms(self, utterance_id: str) -> int:
        state = self.utterance_mgr.get_state(utterance_id)
        if state is not None and state.speech_started_at > 0:
            end_ts = state.speech_ended_at or time.time()
            return max(0, int((end_ts - state.speech_started_at) * 1000))
        if self.state.telemetry is not None and self.state.telemetry.utterance_id == utterance_id:
            end_ts = self.state.telemetry.speech_end_ts or time.time()
            return max(0, int((end_ts - self.state.telemetry.speech_start_ts) * 1000))
        return 0

    @staticmethod
    def _is_offline_final_source(source: str) -> bool:
        s = str(source or "").lower()
        return "offline" in s

    @staticmethod
    def _is_server_final_source(source: str) -> bool:
        s = str(source or "").lower()
        return "server_final" in s or s == "final"

    def _mark_room_stage(self, stage: str) -> None:
        self.room_state.last_stage = stage

    async def stop(self, reason: str = "participant_stop") -> None:
        self._stop_reason = reason
        if self.audio_stream is not None:
            await self.audio_stream.aclose()
            self.audio_stream = None
        if self._task is not None and self._task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.gather(self._task, return_exceptions=True), timeout=6.5)
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        await self.publisher.publish_state(
            self.user_identity,
            state="track.subscribed",
            payload={"trackSid": self.publication.sid},
        )
        self._mark_room_stage("track.subscribed")
        structured_voice_log(
            "voice.track_subscribed",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            track_sid=self.publication.sid,
        )

        self.audio_stream = rtc.AudioStream.from_track(
            track=self.track,
            sample_rate=48000,
            num_channels=1,
            frame_size_ms=20,
        )
        try:
            await self.funasr.ensure_connected()
        except Exception as exc:
            structured_voice_log(
                "voice.funasr_preconnect_failed",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                error=str(exc),
            )

        try:
            async for audio_event in self.audio_stream:
                try:
                    normalized_chunks = self.audio_normalizer.transform(audio_event.frame)
                    for normalized_pcm in normalized_chunks:
                        if not self._received_first_frame and normalized_pcm:
                            self._received_first_frame = True
                            structured_voice_log(
                                "voice.first_audio_frame_received",
                                room_id=self.room_id_getter(),
                                room_name=self.room_name,
                                user_identity=self.user_identity,
                                track_sid=self.publication.sid,
                            )
                            await self.publisher.publish_state(
                                self.user_identity,
                                state="audio.first_frame",
                                payload={"trackSid": self.publication.sid},
                            )
                            self._mark_room_stage("audio.first_frame")
                        await self._consume_pcm(normalized_pcm)
                except asyncio.CancelledError:
                    raise
                except Exception as frame_exc:
                    structured_voice_log(
                        "voice.audio_frame.error",
                        room_id=self.room_id_getter(),
                        room_name=self.room_name,
                        user_identity=self.user_identity,
                        error=str(frame_exc),
                    )
        except asyncio.CancelledError:
            raise
        finally:
            await self._cancel_pending_finish_task()
            for normalized_pcm in self.audio_normalizer.flush():
                await self._consume_pcm(normalized_pcm)
            for action in self.vad.flush():
                await self._handle_vad_action(action.kind, action.pcm, action.event_ts)
            await self._cancel_pending_finish_task()
            if self.state.active_utterance_id:
                await self._finish_utterance(time.time(), reason=self._stop_reason, force_flush_on_stop=True)
            await self.funasr.close()

    async def _consume_pcm(self, pcm: bytes) -> None:
        actions = self.vad.feed(pcm)
        for action in actions:
            await self._handle_vad_action(action.kind, action.pcm, action.event_ts)

    async def _handle_vad_action(self, kind: str, pcm: bytes, event_ts: float | None) -> None:
        if kind == "speech_start":
            if self.state.active_utterance_id and self._pending_finish_task is not None:
                await self._resume_utterance(pcm, event_ts or time.time())
                return
            if self.state.active_utterance_id and self._awaiting_final:
                self._buffer_deferred_utterance_audio(pcm, event_ts=event_ts or time.time(), mark_start=True)
                structured_voice_log(
                    "vad.speech_start_buffered",
                    **self._log_context(),
                    utterance_id=self.state.active_utterance_id,
                    event_ts=event_ts or time.time(),
                    reason="awaiting_final",
                    buffered_bytes=sum(len(chunk) for chunk in self._deferred_utterance_chunks),
                )
                return
            if self.state.active_utterance_id:
                structured_voice_log(
                    "vad.speech_start_ignored",
                    **self._log_context(),
                    utterance_id=self.state.active_utterance_id,
                    event_ts=event_ts or time.time(),
                    reason="awaiting_final",
                )
                return
            await self._start_utterance(pcm, event_ts or time.time())
            return
        if kind == "speech_chunk":
            if self.state.active_utterance_id and not self._awaiting_final:
                await self._send_active_audio_chunk(pcm)
            elif self._awaiting_final and pcm:
                self._buffer_deferred_utterance_audio(pcm, event_ts=event_ts or time.time())
            return
        if kind == "speech_end":
            if self._awaiting_final and self._deferred_utterance_chunks:
                self._deferred_finish_requested = True
                self._deferred_utterance_end_ts = event_ts or time.time()
                structured_voice_log(
                    "vad.speech_end_buffered",
                    **self._log_context(),
                    utterance_id=self.state.active_utterance_id,
                    event_ts=event_ts or time.time(),
                    reason="awaiting_final",
                    buffered_bytes=sum(len(chunk) for chunk in self._deferred_utterance_chunks),
                )
                return
            await self._schedule_finish_utterance(event_ts or time.time(), reason="vad_end")

    def _buffer_deferred_utterance_audio(self, pcm: bytes, *, event_ts: float, mark_start: bool = False) -> None:
        if mark_start and self._deferred_utterance_start_ts is None:
            self._deferred_utterance_start_ts = event_ts
        if pcm:
            self._deferred_utterance_chunks.append(bytes(pcm))

    async def _replay_deferred_utterance_if_needed(self) -> None:
        if not self._deferred_utterance_chunks:
            self._deferred_utterance_start_ts = None
            self._deferred_utterance_end_ts = None
            self._deferred_finish_requested = False
            return
        initial_pcm = b"".join(self._deferred_utterance_chunks)
        speech_start_ts = self._deferred_utterance_start_ts or time.time()
        speech_end_ts = self._deferred_utterance_end_ts or time.time()
        should_finish = self._deferred_finish_requested
        self._deferred_utterance_chunks = []
        self._deferred_utterance_start_ts = None
        self._deferred_utterance_end_ts = None
        self._deferred_finish_requested = False
        structured_voice_log(
            "vad.deferred_utterance_replayed",
            **self._log_context(),
            event_ts=speech_start_ts,
            buffered_bytes=len(initial_pcm),
            finish_requested=should_finish,
        )
        await self._start_utterance(initial_pcm, speech_start_ts)
        if should_finish:
            await self._finish_utterance(speech_end_ts, reason="awaiting_final_replay")

    async def _start_utterance(self, initial_pcm: bytes, speech_start_ts: float) -> None:
        utterance_id = str(uuid4())
        self.state.active_utterance_id = utterance_id
        self._awaiting_final = False
        self._finishing_utterance_id = ""
        self.utterance_mgr.start_utterance(utterance_id, speech_start_ts)
        self.room_state.last_vad_event = "speech_start"
        self._mark_room_stage("vad.speech_start")
        self.state.telemetry = UtteranceTelemetry(
            room_name=self.room_name,
            room_id=self.room_id_getter(),
            user_identity=self.user_identity,
            utterance_id=utterance_id,
            speech_start_ts=speech_start_ts,
            speech_start_monotonic=time.monotonic(),
        )
        self._set_vad_state("speech", event_name="vad.speech_start", event_ts=speech_start_ts, utterance_id=utterance_id)
        await self.publisher.publish_state(
            self.user_identity,
            state="speech.start",
            payload={"utteranceId": utterance_id},
        )
        structured_voice_log("voice.speech_start", **self.state.telemetry.to_log_payload())
        await self.funasr.start_utterance(utterance_id, initial_pcm=initial_pcm)

    async def _resume_utterance(self, initial_pcm: bytes, speech_restart_ts: float) -> None:
        utterance_id = self.state.active_utterance_id
        resume_after_ms = 0
        if self._pending_finish_started_monotonic > 0:
            resume_after_ms = int((time.monotonic() - self._pending_finish_started_monotonic) * 1000)
        await self._cancel_pending_finish_task()
        self.room_state.last_vad_event = "speech_resume"
        self._mark_room_stage("vad.speech_resume")
        structured_voice_log(
            "vad.speech_resume",
            **self._log_context(),
            utterance_id=utterance_id,
            event_ts=speech_restart_ts,
            resume_after_ms=resume_after_ms,
        )
        if initial_pcm:
            await self._send_active_audio_chunk(initial_pcm)

    async def _schedule_finish_utterance(self, speech_end_ts: float, *, reason: str) -> None:
        if not self.state.active_utterance_id:
            return
        if self._awaiting_final:
            return
        utterance_id = self.state.active_utterance_id
        hold_ms = max(SILERO_SPEECH_END_HOLD_MS, 0)
        structured_voice_log(
            "vad.speech_end.detected",
            **self._log_context(),
            utterance_id=utterance_id,
            event_ts=speech_end_ts,
            reason=reason,
            hold_ms=hold_ms,
        )
        if hold_ms <= 0:
            await self._finish_utterance(speech_end_ts, reason=reason)
            return
        await self._cancel_pending_finish_task()
        self._pending_finish_started_monotonic = time.monotonic()
        self._pending_finish_task = asyncio.create_task(
            self._delayed_finish_utterance(
                utterance_id=utterance_id,
                speech_end_ts=speech_end_ts,
                reason=reason,
                hold_ms=hold_ms,
            ),
            name=f"voice-finish-hold-{self.user_identity}",
        )

    async def _delayed_finish_utterance(self, *, utterance_id: str, speech_end_ts: float, reason: str, hold_ms: int) -> None:
        try:
            await asyncio.sleep(hold_ms / 1000)
            if self._pending_finish_task is not asyncio.current_task():
                return
            self._pending_finish_task = None
            self._pending_finish_started_monotonic = 0.0
            if self.state.active_utterance_id != utterance_id:
                return
            await self._finish_utterance(speech_end_ts, reason=reason)
        except asyncio.CancelledError:
            return

    async def _cancel_pending_finish_task(self) -> None:
        task = self._pending_finish_task
        self._pending_finish_task = None
        self._pending_finish_started_monotonic = 0.0
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _send_active_audio_chunk(self, pcm: bytes) -> None:
        utterance_id = self.state.active_utterance_id
        if not utterance_id or not pcm:
            return
        try:
            await self.funasr.send_audio_chunk(pcm)
        except Exception as exc:
            structured_voice_log(
                "asr.audio_chunk.failed",
                **self._log_context(),
                bytes=len(pcm),
                utterance_id=utterance_id,
                error=str(exc),
            )
            return
        structured_voice_log("asr.audio_chunk_sent", **self._log_context(), bytes=len(pcm), utterance_id=utterance_id)
        self.room_state.last_audio_chunk_sent = time.strftime("%H:%M:%S")
        self._mark_room_stage("asr.audio_chunk_sent")
        now_mono = time.monotonic()
        if (now_mono - self._last_chunk_state_emit_monotonic) >= 0.35:
            self._last_chunk_state_emit_monotonic = now_mono
            await self.publisher.publish_state(
                self.user_identity,
                state="asr.audio_chunk_sent",
                payload={"utteranceId": utterance_id, "bytes": len(pcm)},
            )

    async def _finish_utterance(self, speech_end_ts: float, *, reason: str = "vad_end", force_flush_on_stop: bool = False) -> None:
        if not self.state.active_utterance_id or self.state.telemetry is None:
            return
        if self._awaiting_final and self._finishing_utterance_id == self.state.active_utterance_id:
            return
        await self._cancel_pending_finish_task()
        self._awaiting_final = True
        utterance_id = self.state.active_utterance_id
        self._finishing_utterance_id = utterance_id
        self.state.telemetry.speech_end_ts = speech_end_ts
        self.utterance_mgr.mark_speech_end(utterance_id, speech_end_ts)
        self.room_state.last_vad_event = "speech_end"
        self._mark_room_stage("vad.speech_end")
        self._set_vad_state("idle", event_name="vad.speech_end", event_ts=speech_end_ts, utterance_id=utterance_id)
        await self.publisher.publish_state(
            self.user_identity,
            state="speech.end",
            payload={"utteranceId": utterance_id},
        )
        structured_voice_log("asr.flush_sent", **self._log_context(), utterance_id=utterance_id, reason=reason)
        self.room_state.last_flush_sent = time.strftime("%H:%M:%S")
        self._mark_room_stage("asr.flush_sent")
        await self.publisher.publish_state(
            self.user_identity,
            state="asr.flush_sent",
            payload={"utteranceId": utterance_id, "reason": reason},
        )
        if force_flush_on_stop:
            structured_voice_log("asr.force_flush_on_stop", **self._log_context(), utterance_id=utterance_id, reason=reason)
            self.room_state.last_stage = "asr.force_flush_on_stop"
            await self.publisher.publish_state(
                self.user_identity,
                state="asr.force_flush_on_stop",
                payload={"utteranceId": utterance_id, "reason": reason},
            )
        try:
            final_event = await self.funasr.finish_utterance()
            if final_event is None:
                structured_voice_log("voice.speech_end_without_final", **self.state.telemetry.to_log_payload())
                await self._publish_partial_fallback(utterance_id)
            elif "timeout_partial" in (final_event.source or ""):
                structured_voice_log(
                    "voice.finish_utterance.timeout_fallback_suppressed",
                    **self.state.telemetry.to_log_payload(),
                    final_source=final_event.source,
                    text=final_event.text,
                    funasr_connected=self.funasr.is_connected,
                )
                if final_event.source not in {"partial_fallback"}:
                    await self._publish_recognition_incomplete(
                        utterance_id,
                        reason="timeout_partial_fallback_blocked",
                        preview_text=self.partial_acc.get(utterance_id) or final_event.text,
                    )
            else:
                structured_voice_log(
                    "voice.finish_utterance.got_final",
                    **self.state.telemetry.to_log_payload(),
                    final_source=final_event.source,
                )
        except Exception as exc:
            structured_voice_log(
                "voice.finish_utterance.error",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                error=str(exc),
            )
            await self._publish_partial_fallback(utterance_id)
        finally:
            self._awaiting_final = False
            self._finishing_utterance_id = ""
            self.utterance_mgr.clear_active()
            self.state.active_utterance_id = ""
            self.state.telemetry = None
            await self._replay_deferred_utterance_if_needed()

    async def _publish_partial_fallback(self, utterance_id: str) -> None:
        state = self.utterance_mgr.get_state(utterance_id)
        if state is not None and state.selected_text:
            return  # already published a final for this utterance
        total_audio_ms = self._utterance_duration_ms(utterance_id)
        fallback_text = self.partial_acc.get(utterance_id)
        context = self._commit_context()
        if not fallback_text:
            structured_voice_log(
                "utterance.final.fallback_skipped",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                fallback_text=fallback_text,
                reason="empty_accumulated_text",
            )
            await self._publish_recognition_incomplete(utterance_id, reason="no_final", preview_text=fallback_text)
            return
        decision = CommitGate.evaluate(
            utterance_id=utterance_id,
            selected_text=fallback_text,
            source="partial_fallback",
            utterance_duration_ms=total_audio_ms,
            context=context,
            has_offline_final=False,
            has_server_final=False,
            accumulated_partial_text=fallback_text,
        )
        if not decision.allowed:
            blocked_state = self.utterance_mgr.get_state(utterance_id)
            if blocked_state is not None:
                blocked_state.selected_text = ""
                blocked_state.selected_source = ""
            structured_voice_log(
                "utterance.final.fallback_skipped",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                fallback_text=fallback_text,
                reason=decision.reason,
                context_mode=context.mode,
                pending_question=context.pending_question,
            )
            structured_voice_log(
                "transcript.commit.blocked",
                **self._log_context(),
                utterance_id=utterance_id,
                text=fallback_text,
                source="partial_fallback",
                reason=decision.reason,
                utterance_duration_ms=total_audio_ms,
                context_mode=context.mode,
            )
            structured_voice_log(
                "transcript.commit.blocked.reason",
                **self._log_context(),
                utterance_id=utterance_id,
                reason=decision.reason,
            )
            await self._publish_recognition_incomplete(
                utterance_id,
                reason=decision.reason,
                preview_text=fallback_text,
                message=decision.display_message,
            )
            return
        selected = self.utterance_mgr.select_text(utterance_id, fallback_text, "partial_fallback")
        if selected is None:
            return
        structured_voice_log(
            "utterance.final_text.selected",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            utterance_id=utterance_id,
            text=fallback_text,
            source="partial_fallback",
        )
        structured_voice_log(
            "transcript.commit.allowed",
            **self._log_context(),
            utterance_id=utterance_id,
            text=fallback_text,
            source="partial_fallback",
            reason=decision.reason,
            utterance_duration_ms=total_audio_ms,
            context_mode=context.mode,
        )
        try:
            await self.publisher.publish_final(
                self.user_identity,
                utterance_id=utterance_id,
                text=fallback_text,
                source="partial_fallback",
            )
            self.utterance_mgr.mark_published(utterance_id)
            asyncio.create_task(
                self._watch_ack(utterance_id, fallback_text, "partial_fallback"),
                name=f"voice-ack-{self.user_identity}-{utterance_id[:8]}",
            )
        except Exception as exc:
            self.utterance_mgr.mark_failed(utterance_id, str(exc))
            structured_voice_log(
                "utterance.submit.failed",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                error=str(exc),
            )

    async def _publish_recognition_incomplete(
        self,
        utterance_id: str,
        *,
        reason: str,
        preview_text: str = "",
        message: str = "",
    ) -> None:
        display_message = message or "我听到了一部分，但还没拿到完整识别结果，请再说一遍。"
        structured_voice_log(
            "recognition_incomplete.no_final",
            **self._log_context(),
            utterance_id=utterance_id,
            reason=reason,
            preview_text=preview_text,
        )
        await self.publisher.publish_state(
            self.user_identity,
            state="recognition_incomplete",
            payload={
                "utteranceId": utterance_id,
                "reason": reason,
                "previewText": preview_text,
                "message": display_message,
            },
        )

    async def _on_partial(self, event: FunASRTranscriptEvent) -> None:
        self.state.last_partial_text = event.text
        structured_voice_log(
            "funasr.partial.received",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            utterance_id=event.utterance_id,
            text=event.text,
            text_length=len(event.text),
        )
        if event.text.strip():
            self.utterance_mgr.record_partial(event.utterance_id, event.text)
            self.partial_acc.feed(event.utterance_id, event.text)
        should_log_first_partial = self.state.telemetry is not None and self.state.telemetry.asr_first_partial_ms is None
        if self.state.telemetry is not None:
            self.state.telemetry.mark_first_partial()
        if should_log_first_partial and self.state.telemetry is not None:
            structured_voice_log("voice.funasr_first_partial", **self.state.telemetry.to_log_payload())
        self._mark_room_stage("funasr.partial")
        await self.publisher.publish_partial(
            self.user_identity,
            utterance_id=event.utterance_id,
            text=event.text,
        )
        await self.publisher.publish_state(
            self.user_identity,
            state="funasr.partial",
            payload={
                "utteranceId": event.utterance_id,
                "textLength": len(event.text),
                "asrFirstPartialMs": self.state.telemetry.asr_first_partial_ms if self.state.telemetry is not None else None,
            },
        )

    async def _on_final(self, event: FunASRTranscriptEvent) -> None:
        utterance_id = event.utterance_id
        final_source = event.source or ("timeout_partial_fallback" if event.raw.get("fallback_final") else "server_final")

        structured_voice_log(
            "funasr.final.received",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            utterance_id=utterance_id,
            text=event.text,
            text_length=len(event.text),
            source=final_source,
        )

        is_timeout_fallback = "timeout_partial" in final_source
        if is_timeout_fallback and self.funasr.is_connected:
            structured_voice_log(
                "utterance.final.skipped.timeout_fallback_connection_alive",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                source=final_source,
                text=event.text,
            )
            return

        state = self.utterance_mgr.get_state(utterance_id)
        if state is not None and state.status.value in ("acked", "submitted"):
            structured_voice_log(
                "late_final_after_submit",
                **self._log_context(),
                utterance_id=utterance_id,
                text=event.text,
                source=final_source,
                existing_text=state.selected_text,
                existing_source=state.selected_source,
                existing_status=state.status.value,
            )
            return

        selected = self.utterance_mgr.select_text(utterance_id, event.text, final_source)
        if selected is None:
            return

        selected_text, selected_source = selected

        duration_ms = self._utterance_duration_ms(utterance_id)
        accumulated_partial_text = self.partial_acc.get(utterance_id)
        context = self._commit_context()
        decision = CommitGate.evaluate(
            utterance_id=utterance_id,
            selected_text=selected_text,
            source=selected_source,
            utterance_duration_ms=duration_ms,
            context=context,
            has_offline_final=self._is_offline_final_source(selected_source),
            has_server_final=self._is_server_final_source(selected_source),
            accumulated_partial_text=accumulated_partial_text,
        )
        if not decision.allowed:
            structured_voice_log(
                "transcript.commit.blocked",
                **self._log_context(),
                utterance_id=utterance_id,
                text=selected_text,
                source=selected_source,
                reason=decision.reason,
                utterance_duration_ms=duration_ms,
                context_mode=context.mode,
            )
            structured_voice_log(
                "transcript.commit.blocked.reason",
                **self._log_context(),
                utterance_id=utterance_id,
                reason=decision.reason,
            )
            if "fallback" in selected_source:
                structured_voice_log(
                    "late_final_after_blocked_fallback",
                    **self._log_context(),
                    utterance_id=utterance_id,
                    text=selected_text,
                    source=selected_source,
                )
            await self._publish_recognition_incomplete(
                utterance_id,
                reason=decision.reason,
                preview_text=accumulated_partial_text or selected_text,
                message=decision.display_message,
            )
            return

        structured_voice_log(
            "transcript.commit.allowed",
            **self._log_context(),
            utterance_id=utterance_id,
            text=selected_text,
            source=selected_source,
            reason=decision.reason,
            utterance_duration_ms=duration_ms,
            context_mode=context.mode,
            has_offline_final=self._is_offline_final_source(selected_source),
            has_server_final=self._is_server_final_source(selected_source),
        )

        self.state.last_final_text = selected_text
        self.state.last_partial_text = ""
        if self.state.telemetry is not None:
            self.state.telemetry.mark_final()
            structured_voice_log(
                "voice.utterance_final",
                **self.state.telemetry.to_log_payload(),
                final_source=selected_source,
                final_text_length=len(selected_text),
            )
        else:
            structured_voice_log(
                "voice.late_utterance_final",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                final_source=selected_source,
                final_text_length=len(selected_text),
            )
        self.room_state.last_funasr_final = time.strftime("%H:%M:%S")
        self._mark_room_stage("funasr.final")

        structured_voice_log(
            "utterance.submit.begin",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            utterance_id=utterance_id,
            text=selected_text,
            source=selected_source,
        )

        try:
            await self.publisher.publish_final(
                self.user_identity,
                utterance_id=utterance_id,
                text=selected_text,
                source=selected_source,
            )
            self.utterance_mgr.mark_published(utterance_id)
        except Exception as exc:
            self.utterance_mgr.mark_failed(utterance_id, str(exc))
            structured_voice_log(
                "utterance.submit.failed",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                error=str(exc),
            )
            raise

        # Spawn ack watcher (does not block the recv loop)
        asyncio.create_task(
            self._watch_ack(utterance_id, selected_text, selected_source),
            name=f"voice-ack-{self.user_identity}-{utterance_id[:8]}",
        )

        await self.publisher.publish_state(
            self.user_identity,
            state="funasr.final",
            payload={
                "utteranceId": utterance_id,
                "finalSource": selected_source,
                "finalTextLength": len(selected_text),
                "asrFinalMs": self.state.telemetry.asr_final_ms if self.state.telemetry is not None else None,
            },
        )
        await self.publisher.publish_state(
            self.user_identity,
            state="utterance.complete",
            payload={"utteranceId": utterance_id},
        )

    async def _watch_ack(self, utterance_id: str, text: str, source: str) -> None:
        """Wait for frontend ack, retry publish on timeout."""
        acked = await self.utterance_mgr.wait_for_ack(utterance_id, VOICE_FINAL_ACK_TIMEOUT_MS)
        if acked:
            self.utterance_mgr.cleanup_ack(utterance_id)
            return

        for attempt in range(1, VOICE_FINAL_ACK_RETRY + 1):
            retry_delay_ms = VOICE_FINAL_ACK_TIMEOUT_MS * (attempt + 1)
            structured_voice_log(
                "transcript.publish.retry",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                utterance_id=utterance_id,
                attempt=attempt,
                retry_delay_ms=retry_delay_ms,
            )
            try:
                await self.publisher.publish_final(
                    self.user_identity,
                    utterance_id=utterance_id,
                    text=text,
                    source=source,
                )
                self.utterance_mgr.mark_published(utterance_id)
            except Exception as exc:
                structured_voice_log(
                    "transcript.publish.retry_failed",
                    room_id=self.room_id_getter(),
                    room_name=self.room_name,
                    user_identity=self.user_identity,
                    utterance_id=utterance_id,
                    attempt=attempt,
                    error=str(exc),
                )

            acked = await self.utterance_mgr.wait_for_ack(utterance_id, retry_delay_ms)
            if acked:
                self.utterance_mgr.cleanup_ack(utterance_id)
                return

        self.utterance_mgr.mark_failed(utterance_id, "ack_timeout")
        self.utterance_mgr.cleanup_ack(utterance_id)
        self.utterance_mgr.cleanup_ack(utterance_id)
        structured_voice_log(
            "transcript.publish.failed",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            utterance_id=utterance_id,
            error="ack_timeout",
            total_attempts=VOICE_FINAL_ACK_RETRY + 1,
        )

    async def _on_funasr_state(self, state: str, payload: dict) -> None:
        structured_voice_log(
            f"voice.{state.replace('.', '_')}",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            **payload,
        )
        if state == "funasr.message.received":
            self.room_state.last_funasr_message = time.strftime("%H:%M:%S")
        self._mark_room_stage(state)
        await self.publisher.publish_state(self.user_identity, state=state, payload=payload)

    def _set_vad_state(self, next_state: str, *, event_name: str, event_ts: float, utterance_id: str) -> None:
        if self._vad_state != next_state:
            structured_voice_log(
                "vad.state_change",
                **self._log_context(),
                previous_state=self._vad_state,
                next_state=next_state,
                utterance_id=utterance_id,
            )
            asyncio.create_task(
                self.publisher.publish_state(
                    self.user_identity,
                    state="vad.state_change",
                    payload={"vadState": next_state, "utteranceId": utterance_id},
                )
            )
        self._vad_state = next_state
        structured_voice_log(
            event_name,
            **self._log_context(),
            utterance_id=utterance_id,
            event_ts=event_ts,
        )


class LiveKitRoomBridge:
    def __init__(self, room_name: str, *, context_provider=None) -> None:
        self.state = RoomSessionState(
            room_name=room_name,
            worker_identity=f"voice-worker-{room_name}-{uuid4().hex[:8]}",
        )
        self.room = rtc.Room()
        self.context_provider = context_provider
        self.publisher: LiveKitTranscriptPublisher | None = None
        self._processors: dict[tuple[str, str], ParticipantVoiceProcessor] = {}
        self._session_task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._ready = asyncio.Event()
        self._failed = asyncio.Event()

    def start(self) -> None:
        if self._session_task is None:
            self._session_task = asyncio.create_task(self._run(), name=f"voice-room-{self.state.room_name}")
            self._session_task.add_done_callback(self._on_session_done)

    async def stop(self) -> None:
        self._closed.set()
        await self._stop_all_participants()
        if self.room.isconnected():
            await self.room.disconnect()
        if self._session_task is not None and self._session_task is not asyncio.current_task():
            self._session_task.cancel()
            await asyncio.gather(self._session_task, return_exceptions=True)

    async def wait_until_ready(self, timeout_s: float = 8.0) -> bool:
        if self.state.connected:
            return True
        if self._failed.is_set():
            return False
        waiter = asyncio.create_task(self._wait_for_ready_or_failed())
        try:
            await asyncio.wait_for(waiter, timeout=timeout_s)
        except asyncio.TimeoutError:
            self.state.status = "failed"
            self.state.last_error = self.state.last_error or f"worker startup timed out after {timeout_s:.1f}s"
            self._failed.set()
            structured_voice_log(
                "voice.worker_startup_timeout",
                room_id=self.state.room_id or self.state.room_name,
                room_name=self.state.room_name,
                user_identity=self.state.worker_identity,
                error=self.state.last_error,
            )
            return False
        return self.state.connected and not self._failed.is_set()

    async def _wait_for_ready_or_failed(self) -> None:
        while not self._ready.is_set() and not self._failed.is_set():
            await asyncio.sleep(0.05)

    def _on_session_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self.state.connected = False
        self.state.status = "failed"
        self.state.last_error = str(exc)
        self._failed.set()
        structured_voice_log(
            "voice.worker_task_failed",
            room_id=self.state.room_id or self.state.room_name,
            room_name=self.state.room_name,
            user_identity=self.state.worker_identity,
            error=self.state.last_error,
        )

    async def _run(self) -> None:
        self.state.status = "starting"
        self.state.last_stage = "worker_starting"
        structured_voice_log(
            "voice.worker_starting",
            room_id=self.state.room_name,
            room_name=self.state.room_name,
            user_identity=self.state.worker_identity,
            ws_url=LIVEKIT_WS_URL,
        )
        try:
            token_payload = create_livekit_worker_token(room_name=self.state.room_name, user_id=self.state.worker_identity)
            structured_voice_log(
                "voice.room_connect_start",
                room_id=self.state.room_name,
                room_name=self.state.room_name,
                user_identity=self.state.worker_identity,
                ws_url=LIVEKIT_WS_URL,
                token_room_name=token_payload.room_name,
                token_identity=token_payload.identity,
            )
            self.state.status = "connecting"
            self.state.last_stage = "room.connect.start"
            await self.room.connect(
                LIVEKIT_WS_URL,
                token_payload.participant_token,
                rtc.RoomOptions(auto_subscribe=True),
            )
            self.state.connected = True
            self.state.status = "connected"
            self.state.last_stage = "room.connect.success"
            self.state.room_id = await self.room.sid
            self.state.local_participant_identity = self.room.local_participant.identity or token_payload.identity
            self.publisher = LiveKitTranscriptPublisher(
                self.room,
                room_name=self.state.room_name,
                room_id=self.state.room_id or self.state.room_name,
            )
            structured_voice_log(
                "voice.room_connect_success",
                room_id=self.state.room_id or self.state.room_name,
                room_name=self.state.room_name,
                user_identity=self.state.worker_identity,
                local_participant_identity=self.state.local_participant_identity,
            )
            self.room.on("participant_connected", self._on_participant_connected)
            self.room.on("track_subscribed", self._on_track_subscribed)
            self.room.on("track_unsubscribed", self._on_track_unsubscribed)
            self.room.on("participant_disconnected", self._on_participant_disconnected)
            self.room.on("disconnected", self._on_room_disconnected)
            self._ready.set()

            for participant in self.room.remote_participants.values():
                await self._handle_remote_participant_discovered(participant)
                for publication in participant.track_publications.values():
                    if publication.track is not None and publication.kind == TrackKind.KIND_AUDIO:
                        await self._ensure_participant_processor(publication.track, publication, participant)

            await self._closed.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state.connected = False
            self.state.status = "failed"
            self.state.last_error = str(exc)
            self.state.last_stage = "worker_start_failed"
            self._failed.set()
            structured_voice_log(
                "voice.worker_start_failed",
                room_id=self.state.room_id or self.state.room_name,
                room_name=self.state.room_name,
                user_identity=self.state.worker_identity,
                error=self.state.last_error,
            )
            await self._broadcast_worker_error(self.state.last_error)
            raise

    def _on_room_disconnected(self, *_args) -> None:
        asyncio.create_task(self._handle_room_disconnect())

    async def _handle_room_disconnect(self) -> None:
        structured_voice_log(
            "voice.room_disconnected",
            room_id=self.state.room_id or self.state.room_name,
            room_name=self.state.room_name,
            user_identity=self.state.worker_identity,
        )
        await self._stop_all_participants()
        self.state.connected = False
        if self.state.status != "failed":
            self.state.status = "disconnected"
        self.state.last_stage = "room.disconnected"
        self._closed.set()

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        asyncio.create_task(self._handle_remote_participant_discovered(participant))

    async def _handle_remote_participant_discovered(self, participant: rtc.RemoteParticipant) -> None:
        structured_voice_log(
            "voice.remote_participant_discovered",
            room_id=self.state.room_id or self.state.room_name,
            room_name=self.state.room_name,
            user_identity=participant.identity,
            worker_identity=self.state.worker_identity,
        )
        if self.publisher is not None and participant.identity != self.state.worker_identity:
            await self.publisher.publish_state(
                participant.identity,
                state="worker.joined",
                payload={"workerIdentity": self.state.local_participant_identity or self.state.worker_identity},
            )

    def _on_track_subscribed(
        self,
        track: rtc.RemoteAudioTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        asyncio.create_task(self._ensure_participant_processor(track, publication, participant))

    async def _ensure_participant_processor(
        self,
        track: rtc.RemoteAudioTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if publication.kind != TrackKind.KIND_AUDIO or publication.source != TrackSource.SOURCE_MICROPHONE:
            return
        if participant.identity == self.state.worker_identity:
            return
        processor_key = (participant.identity, publication.sid)
        if processor_key in self._processors:
            structured_voice_log(
                "voice.track_subscribed.duplicate_skipped",
                room_id=self.state.room_id or self.state.room_name,
                room_name=self.state.room_name,
                user_identity=participant.identity,
                track_sid=publication.sid,
            )
            return
        if self.publisher is None:
            structured_voice_log(
                "voice.processor.create_skipped",
                room_id=self.state.room_id or self.state.room_name,
                room_name=self.state.room_name,
                user_identity=participant.identity,
                track_sid=publication.sid,
                reason="publisher_not_ready",
            )
            return
        processor = ParticipantVoiceProcessor(
            room_name=self.state.room_name,
            room_id_getter=lambda: self.state.room_id or self.state.room_name,
            room_state=self.state,
            room=self.room,
            publisher=self.publisher,
            participant=participant,
            publication=publication,
            track=track,
            context_provider=self.context_provider,
        )
        self._processors[processor_key] = processor
        self.state.participants[participant.identity] = processor.state
        try:
            processor.start()
        except Exception as exc:
            self._processors.pop(processor_key, None)
            self.state.participants.pop(participant.identity, None)
            structured_voice_log(
                "voice.processor.create_failed",
                room_id=self.state.room_id or self.state.room_name,
                room_name=self.state.room_name,
                user_identity=participant.identity,
                track_sid=publication.sid,
                error=str(exc),
            )
            return
        structured_voice_log(
            "voice.processor.created",
            room_id=self.state.room_id or self.state.room_name,
            room_name=self.state.room_name,
            user_identity=participant.identity,
            track_sid=publication.sid,
        )

    def _on_track_unsubscribed(
        self,
        _track: rtc.RemoteAudioTrack | None,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        asyncio.create_task(self._stop_participant_if_matching(participant.identity, publication.sid, reason="track_unsubscribed"))

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        asyncio.create_task(self._stop_participant(participant.identity, reason="participant_disconnected"))

    async def _stop_participant_if_matching(self, identity: str, track_sid: str, reason: str = "track_unsubscribed") -> None:
        processor_key = (identity, track_sid)
        processor = self._processors.get(processor_key)
        if processor is None:
            return
        await self._stop_processor(processor_key, reason=reason)

    async def _stop_processor(self, processor_key: tuple[str, str], reason: str = "participant_stop") -> None:
        processor = self._processors.pop(processor_key, None)
        if processor is None:
            return
        await processor.stop(reason=reason)
        structured_voice_log(
            "voice.processor.cleanup",
            room_id=self.state.room_id or self.state.room_name,
            room_name=self.state.room_name,
            user_identity=processor_key[0],
            track_sid=processor_key[1],
            reason=reason,
        )

    async def _stop_participant(self, identity: str, reason: str = "participant_stop") -> None:
        matching_keys = [key for key in self._processors if key[0] == identity]
        self.state.participants.pop(identity, None)
        for key in matching_keys:
            await self._stop_processor(key, reason=reason)
        if not matching_keys:
            structured_voice_log(
                "voice.participant_cleanup.no_processor",
                room_id=self.state.room_id or self.state.room_name,
                room_name=self.state.room_name,
                user_identity=identity,
                reason=reason,
            )

    async def _stop_all_participants(self) -> None:
        for key in list(self._processors.keys()):
            await self._stop_processor(key, reason="room_shutdown")

    async def _broadcast_worker_error(self, error_message: str) -> None:
        if self.publisher is None or not self.room.isconnected():
            return
        for participant in self.room.remote_participants.values():
            if participant.identity == self.state.worker_identity:
                continue
            await self.publisher.publish_state(
                participant.identity,
                state="worker.error",
                payload={"error": error_message},
            )


class VoiceWorkerManager:
    def __init__(self, *, context_provider=None) -> None:
        self._sessions: dict[str, LiveKitRoomBridge] = {}
        self._lock = asyncio.Lock()
        self.context_provider = context_provider

    async def ensure_session(self, room_name: str) -> bool:
        if not livekit_voice_stack_is_configured():
            structured_voice_log(
                "voice.session_skipped",
                room_id=room_name,
                room_name=room_name,
                reason="voice stack not configured",
            )
            return False
        failed_bridge: LiveKitRoomBridge | None = None
        async with self._lock:
            existing = self._sessions.get(room_name)
            if existing is not None and existing.state.status in {"failed", "disconnected"}:
                self._sessions.pop(room_name, None)
                failed_bridge = existing
                existing = None
            if existing is not None:
                bridge = existing
            else:
                bridge = LiveKitRoomBridge(room_name, context_provider=self.context_provider)
                self._sessions[room_name] = bridge
                bridge.start()
                structured_voice_log("voice.session_started", room_id=room_name, room_name=room_name)
        if failed_bridge is not None:
            await failed_bridge.stop()
        ready = await bridge.wait_until_ready()
        if not ready:
            raise RuntimeError(bridge.state.last_error or f"Voice worker failed to join room {room_name}.")
        return True

    async def stop_session(self, room_name: str) -> None:
        async with self._lock:
            bridge = self._sessions.pop(room_name, None)
        if bridge is not None:
            await bridge.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for bridge in sessions:
            await bridge.stop()

    @property
    def active_rooms(self) -> list[str]:
        return list(self._sessions.keys())

    def get_status(self, room_name: str) -> dict[str, object]:
        bridge = self._sessions.get(room_name)
        if bridge is None:
            return {
                "roomName": room_name,
                "status": "idle",
                "connected": False,
                "workerIdentity": "",
                "localParticipantIdentity": "",
                "roomId": "",
                "lastError": "",
                "last_vad_event": "",
                "last_audio_chunk_sent": "",
                "last_flush_sent": "",
                "last_funasr_message": "",
                "last_funasr_final": "",
                "last_stage": "",
            }
        return {
            "roomName": room_name,
            "status": bridge.state.status,
            "connected": bridge.state.connected,
            "workerIdentity": bridge.state.worker_identity,
            "localParticipantIdentity": bridge.state.local_participant_identity,
            "roomId": bridge.state.room_id,
            "lastError": bridge.state.last_error,
            "last_vad_event": bridge.state.last_vad_event,
            "last_audio_chunk_sent": bridge.state.last_audio_chunk_sent,
            "last_flush_sent": bridge.state.last_flush_sent,
            "last_funasr_message": bridge.state.last_funasr_message,
            "last_funasr_final": bridge.state.last_funasr_final,
            "last_stage": bridge.state.last_stage,
        }
