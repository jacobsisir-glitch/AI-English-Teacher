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
    SILERO_VAD_THRESHOLD,
    VOICE_DEBUG_FORCE_SEGMENT_MODE,
    VOICE_DEBUG_FORCE_SEGMENT_MS,
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
from voice.transcript_publisher import LiveKitTranscriptPublisher
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
    ) -> None:
        self.room_name = room_name
        self.room_id_getter = room_id_getter
        self.room_state = room_state
        self.room = room
        self.publisher = publisher
        self.participant = participant
        self.publication = publication
        self.track = track
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
        self._debug_segment_started_at = 0.0
        self._last_chunk_state_emit_monotonic = 0.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"voice-user-{self.user_identity}")

    def _log_context(self) -> dict[str, object]:
        return {
            "room_id": self.room_id_getter(),
            "room_name": self.room_name,
            "user_identity": self.user_identity,
        }

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
            async for audio_event in self.audio_stream:
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
        finally:
            for normalized_pcm in self.audio_normalizer.flush():
                await self._consume_pcm(normalized_pcm)
            for action in self.vad.flush():
                await self._handle_vad_action(action.kind, action.pcm, action.event_ts)
            if self.state.active_utterance_id:
                await self._finish_utterance(time.time(), reason=self._stop_reason, force_flush_on_stop=True)
            await self.funasr.close()

    async def _consume_pcm(self, pcm: bytes) -> None:
        actions = self.vad.feed(pcm)
        for action in actions:
            await self._handle_vad_action(action.kind, action.pcm, action.event_ts)
        if (
            VOICE_DEBUG_FORCE_SEGMENT_MODE
            and self.state.active_utterance_id
            and self.state.telemetry is not None
            and self._debug_segment_started_at > 0
            and ((time.monotonic() - self._debug_segment_started_at) * 1000) >= VOICE_DEBUG_FORCE_SEGMENT_MS
        ):
            await self._finish_utterance(time.time(), reason="debug_interval")
            if self.vad.active:
                await self._start_utterance(b"", time.time())

    async def _handle_vad_action(self, kind: str, pcm: bytes, event_ts: float | None) -> None:
        if kind == "speech_start":
            await self._start_utterance(pcm, event_ts or time.time())
            return
        if kind == "speech_chunk":
            if self.state.active_utterance_id:
                await self.funasr.send_audio_chunk(pcm)
                structured_voice_log("asr.audio_chunk_sent", **self._log_context(), bytes=len(pcm), utterance_id=self.state.active_utterance_id)
                self.room_state.last_audio_chunk_sent = time.strftime("%H:%M:%S")
                self._mark_room_stage("asr.audio_chunk_sent")
                now_mono = time.monotonic()
                if (now_mono - self._last_chunk_state_emit_monotonic) >= 0.35:
                    self._last_chunk_state_emit_monotonic = now_mono
                    await self.publisher.publish_state(
                        self.user_identity,
                        state="asr.audio_chunk_sent",
                        payload={"utteranceId": self.state.active_utterance_id, "bytes": len(pcm)},
                    )
            return
        if kind == "speech_end":
            await self._finish_utterance(event_ts or time.time(), reason="vad_end")

    async def _start_utterance(self, initial_pcm: bytes, speech_start_ts: float) -> None:
        utterance_id = str(uuid4())
        self.state.active_utterance_id = utterance_id
        self._debug_segment_started_at = time.monotonic()
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

    async def _finish_utterance(self, speech_end_ts: float, *, reason: str = "vad_end", force_flush_on_stop: bool = False) -> None:
        if not self.state.active_utterance_id or self.state.telemetry is None:
            return
        utterance_id = self.state.active_utterance_id
        self.state.telemetry.speech_end_ts = speech_end_ts
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
        final_event = await self.funasr.finish_utterance()
        if final_event is None:
            structured_voice_log("voice.speech_end_without_final", **self.state.telemetry.to_log_payload())
        self.state.active_utterance_id = ""
        self._debug_segment_started_at = 0.0

    async def _on_partial(self, event: FunASRTranscriptEvent) -> None:
        self.state.last_partial_text = event.text
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
            payload={"utteranceId": event.utterance_id},
        )

    async def _on_final(self, event: FunASRTranscriptEvent) -> None:
        self.state.last_final_text = event.text
        self.state.last_partial_text = ""
        if self.state.telemetry is not None:
            self.state.telemetry.mark_final()
            structured_voice_log("voice.utterance_final", **self.state.telemetry.to_log_payload())
            structured_voice_log("voice.funasr_final", **self.state.telemetry.to_log_payload())
        self.room_state.last_funasr_final = time.strftime("%H:%M:%S")
        self._mark_room_stage("funasr.final")
        await self.publisher.publish_final(
            self.user_identity,
            utterance_id=event.utterance_id,
            text=event.text,
        )
        await self.publisher.publish_state(
            self.user_identity,
            state="funasr.final",
            payload={"utteranceId": event.utterance_id},
        )
        await self.publisher.publish_state(
            self.user_identity,
            state="utterance.complete",
            payload={"utteranceId": event.utterance_id},
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
    def __init__(self, room_name: str) -> None:
        self.state = RoomSessionState(
            room_name=room_name,
            worker_identity=f"voice-worker-{room_name}-{uuid4().hex[:8]}",
        )
        self.room = rtc.Room()
        self.publisher: LiveKitTranscriptPublisher | None = None
        self._participant_processors: dict[str, ParticipantVoiceProcessor] = {}
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
        existing = self._participant_processors.get(participant.identity)
        if existing is not None:
            return
        if self.publisher is None:
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
        )
        self._participant_processors[participant.identity] = processor
        self.state.participants[participant.identity] = processor.state
        processor.start()
        structured_voice_log(
            "voice.track_subscribed",
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
        processor = self._participant_processors.get(identity)
        if processor is None or processor.state.track_sid != track_sid:
            return
        await self._stop_participant(identity, reason=reason)

    async def _stop_participant(self, identity: str, reason: str = "participant_stop") -> None:
        processor = self._participant_processors.pop(identity, None)
        self.state.participants.pop(identity, None)
        if processor is None:
            return
        await processor.stop(reason=reason)
        structured_voice_log(
            "voice.participant_cleanup",
            room_id=self.state.room_id or self.state.room_name,
            room_name=self.state.room_name,
            user_identity=identity,
            reason=reason,
        )

    async def _stop_all_participants(self) -> None:
        for identity in list(self._participant_processors.keys()):
            await self._stop_participant(identity, reason="room_shutdown")

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
    def __init__(self) -> None:
        self._sessions: dict[str, LiveKitRoomBridge] = {}
        self._lock = asyncio.Lock()

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
                bridge = LiveKitRoomBridge(room_name)
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
