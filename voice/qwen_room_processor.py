from __future__ import annotations

import asyncio
import contextlib
import inspect
import hashlib
import time
from typing import Any

from livekit import rtc

from config import QWEN_AUDIO_PLAYBACK_BUFFER_MS, QWEN_REALTIME_OUTPUT_SAMPLE_RATE, QWEN_REALTIME_VAD_MODE
from voice.audio_buffer import LiveKitAudioNormalizer, PCMWindowBuffer
from voice.providers.qwen_omni_realtime import (
    QWEN_INTERNAL_TOPICS,
    QwenOmniRealtimeSession,
    QwenRealtimeConfigError,
    build_lumina_realtime_instructions,
)
from voice.session_state import ParticipantSessionState, RoomSessionState, structured_voice_log
from voice.transcript_publisher import LiveKitTranscriptPublisher
from voice.vad_controller import SileroVADController
from config import (
    SILERO_CHANNELS,
    SILERO_MIN_SILENCE_MS,
    SILERO_MIN_SPEECH_MS,
    SILERO_PRE_SPEECH_MS,
    SILERO_SAMPLE_RATE,
    SILERO_VAD_THRESHOLD,
)


class QwenParticipantVoiceProcessor:
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
        self.qwen: QwenOmniRealtimeSession | None = None
        self._task: asyncio.Task | None = None
        self._audio_source: rtc.AudioSource | None = None
        self._audio_track: rtc.LocalAudioTrack | None = None
        self._audio_buffer = PCMWindowBuffer(window_samples=QWEN_REALTIME_OUTPUT_SAMPLE_RATE // 50)
        self._audio_queue: asyncio.Queue[tuple[str, bytes, bool]] = asyncio.Queue(maxsize=256)
        self._audio_publish_task: asyncio.Task | None = None
        self._current_audio_response_id = ""
        self._published_track_sid = ""
        self._frame_count_by_response: dict[str, int] = {}
        self._first_audio_enqueued_at: dict[str, float] = {}
        self._first_audio_frame_published_at: dict[str, float] = {}
        self._cancelled_audio_response_ids: set[str] = set()
        self._stopped = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"qwen-voice-user-{self.user_identity}")

    async def stop(self, reason: str = "participant_stop") -> None:
        del reason
        self._stopped = True
        if self.audio_stream is not None:
            await self.audio_stream.aclose()
            self.audio_stream = None
        if self.qwen is not None:
            await self.qwen.close()
        await self._stop_audio_publisher()
        if self._audio_source is not None:
            await self._audio_source.aclose()
            self._audio_source = None
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        await self.publisher.publish_state(
            self.user_identity,
            state="qwen.track.subscribed",
            payload={"trackSid": self.publication.sid},
        )
        await self._publish_teacher_audio_track()
        instructions = build_lumina_realtime_instructions(class_mode=self._class_mode())
        structured_voice_log(
            "qwen.session.instructions.selected",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            instructions_length=len(instructions),
            instructions_hash=hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:12],
        )
        self.qwen = QwenOmniRealtimeSession(
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            participant_id=self.user_identity,
            instructions=instructions,
            on_topic=self._publish_qwen_topic,
            on_audio=self._publish_qwen_audio,
        )
        try:
            await self.qwen.connect()
        except QwenRealtimeConfigError as exc:
            await self._publish_error("configuration_error", str(exc))
            return
        except Exception as exc:
            await self._publish_error("connect_failed", str(exc))
            return

        self.audio_stream = rtc.AudioStream.from_track(
            track=self.track,
            sample_rate=48000,
            num_channels=1,
            frame_size_ms=20,
        )
        try:
            async for audio_event in self.audio_stream:
                for pcm in self.audio_normalizer.transform(audio_event.frame):
                    await self._consume_input_pcm(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._publish_error("input_audio_failed", str(exc))
        finally:
            for pcm in self.audio_normalizer.flush():
                await self._consume_input_pcm(pcm)
            if self.qwen is not None:
                if QWEN_REALTIME_VAD_MODE == "manual":
                    await self.qwen.manual_commit()
                await self.qwen.close()

    async def _consume_input_pcm(self, pcm: bytes) -> None:
        if self.qwen is None or not pcm:
            return
        if self.qwen.expired():
            await self._publish_error(
                "session_expired",
                "Qwen realtime session reached QWEN_REALTIME_MAX_SESSION_MINUTES. Please reconnect voice mode.",
            )
            await self.qwen.close()
            return
        await self.qwen.append_audio(pcm)
        if QWEN_REALTIME_VAD_MODE != "manual":
            return
        for action in self.vad.feed(pcm):
            if action.kind == "speech_start":
                await self.qwen.cancel_response(reason="manual_vad_speech_start")
                await self.publisher.publish_state(
                    self.user_identity,
                    state="speech.start",
                    payload={"source": "qwen_manual_silero"},
                )
            elif action.kind == "speech_end":
                await self.publisher.publish_state(
                    self.user_identity,
                    state="speech.end",
                    payload={"source": "qwen_manual_silero"},
                )
                await self.qwen.manual_commit()

    async def _publish_teacher_audio_track(self) -> None:
        self._audio_source = rtc.AudioSource(sample_rate=QWEN_REALTIME_OUTPUT_SAMPLE_RATE, num_channels=1)
        self._audio_track = rtc.LocalAudioTrack.create_audio_track("lumina-qwen-audio", self._audio_source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.Value("SOURCE_UNKNOWN")
        publish_result = self.room.local_participant.publish_track(self._audio_track, options)
        if inspect.isawaitable(publish_result):
            publish_result = await publish_result
        self._published_track_sid = str(
            getattr(publish_result, "sid", "")
            or getattr(publish_result, "track_sid", "")
            or getattr(getattr(publish_result, "track", None), "sid", "")
            or ""
        )
        structured_voice_log(
            "qwen.livekit.audio_track_published",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            sample_rate=QWEN_REALTIME_OUTPUT_SAMPLE_RATE,
            track_sid=self._published_track_sid,
        )
        await self.publisher.publish_state(
            self.user_identity,
            state="qwen.audio_track.published",
            payload={"sampleRate": QWEN_REALTIME_OUTPUT_SAMPLE_RATE, "trackSid": self._published_track_sid},
        )
        self._ensure_audio_publish_task()

    async def _publish_qwen_audio(self, audio: bytes, response_id: str) -> None:
        if self._stopped or self._audio_source is None:
            return
        if response_id in self._cancelled_audio_response_ids:
            structured_voice_log(
                "qwen.livekit.audio_delta_dropped_late",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                response_id=response_id,
                audio_bytes=len(audio),
            )
            return
        self._ensure_audio_publish_task()
        await self._enqueue_audio(response_id, audio, done=not audio)

    async def _publish_qwen_topic(self, topic: str, payload: dict[str, Any]) -> None:
        if topic == QWEN_INTERNAL_TOPICS["response_cancelled"] and self._audio_source is not None:
            response_id = str(payload.get("responseId") or "")
            if response_id:
                self._cancelled_audio_response_ids.add(response_id)
            self._audio_source.clear_queue()
            self._clear_audio_queue(reason="response_cancelled", response_id=response_id)
        if topic == QWEN_INTERNAL_TOPICS["input_partial"]:
            text = str(payload.get("text") or "")
            await self.publisher.publish_partial(self.user_identity, utterance_id=str(payload.get("utteranceId") or ""), text=text)
        await self.publisher.publish_text(topic, self.user_identity, payload)
        state = _topic_to_state(topic)
        if state:
            await self.publisher.publish_state(self.user_identity, state=state, payload=payload)
            self.room_state.last_stage = state
            if state == "qwen.input.transcript_final":
                self.room_state.last_funasr_final = time.strftime("%H:%M:%S")

    async def _publish_error(self, code: str, message: str) -> None:
        structured_voice_log(
            "qwen.session.error",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            code=code,
            message=message,
        )
        self.room_state.status = "failed"
        self.room_state.last_error = message
        await self.publisher.publish_state(
            self.user_identity,
            state="qwen.session.error",
            payload={"code": code, "message": message},
        )
        await self.publisher.publish_text(
            QWEN_INTERNAL_TOPICS["session_error"],
            self.user_identity,
            {
                "roomId": self.room_id_getter(),
                "roomName": self.room_name,
                "participantId": self.user_identity,
                "code": code,
                "message": message,
            },
        )

    def _class_mode(self) -> bool:
        if self.context_provider is None:
            return False
        try:
            raw = dict(self.context_provider() or {})
        except Exception:
            return False
        return bool(raw.get("class_mode") or raw.get("mode") == "class_mode")

    def _log_frame_capture(self, response_id: str, bytes_count: int, samples_per_channel: int, *, tail: bool) -> None:
        count = self._frame_count_by_response.get(response_id, 0) + 1
        self._frame_count_by_response[response_id] = count
        if response_id and response_id not in self._first_audio_frame_published_at:
            self._first_audio_frame_published_at[response_id] = time.monotonic()
            first_enqueued_at = self._first_audio_enqueued_at.get(response_id, 0.0)
            structured_voice_log(
                "qwen.sync.measured_lag",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                response_id=response_id,
                publish_buffer_ms=int((self._first_audio_frame_published_at[response_id] - first_enqueued_at) * 1000)
                if first_enqueued_at
                else 0,
                track_sid=self._published_track_sid,
            )
        if count == 1 or tail or count % 50 == 0:
            structured_voice_log(
                "qwen.livekit.audio_frame_captured",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                response_id=response_id,
                track_sid=self._published_track_sid,
                audio_bytes=bytes_count,
                samples_per_channel=samples_per_channel,
                frame_count=count,
                tail=tail,
            )

    def _ensure_audio_publish_task(self) -> None:
        if self._audio_publish_task is None or self._audio_publish_task.done():
            self._audio_publish_task = asyncio.create_task(
                self._audio_publisher_loop(),
                name=f"qwen-audio-publisher-{self.user_identity}",
            )

    async def _stop_audio_publisher(self) -> None:
        if self._audio_publish_task is None:
            return
        if not self._audio_publish_task.done():
            self._audio_publish_task.cancel()
        await asyncio.gather(self._audio_publish_task, return_exceptions=True)
        self._audio_publish_task = None
        self._clear_audio_queue(reason="stop", response_id=self._current_audio_response_id)

    async def _enqueue_audio(self, response_id: str, audio: bytes, *, done: bool) -> None:
        if response_id and not self._first_audio_enqueued_at.get(response_id) and audio:
            self._first_audio_enqueued_at[response_id] = time.monotonic()
        try:
            self._audio_queue.put_nowait((response_id, audio, done))
        except asyncio.QueueFull:
            structured_voice_log(
                "qwen.sync.audio_buffer_cleared",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                response_id=response_id,
                reason="audio_queue_full",
                queue_size=self._audio_queue.qsize(),
            )
            self._clear_audio_queue(reason="audio_queue_full", response_id=response_id)
            self._audio_queue.put_nowait((response_id, audio, done))

    def _clear_audio_queue(self, *, reason: str, response_id: str) -> None:
        cleared = 0
        while True:
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.task_done()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        self._audio_buffer.flush()
        self._current_audio_response_id = ""
        structured_voice_log(
            "qwen.sync.audio_buffer_cleared",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            response_id=response_id,
            reason=reason,
            cleared_count=cleared,
        )

    async def _audio_publisher_loop(self) -> None:
        while not self._stopped:
            try:
                response_id, audio, done = await self._audio_queue.get()
            except asyncio.CancelledError:
                raise
            try:
                if response_id in self._cancelled_audio_response_ids:
                    continue
                if response_id and response_id != self._current_audio_response_id:
                    self._audio_buffer.flush()
                    self._current_audio_response_id = response_id
                    buffered_audio, buffered_done = await self._collect_initial_audio(response_id, audio, done)
                    if buffered_audio:
                        await self._publish_pcm_bytes(buffered_audio, response_id)
                    elif buffered_done:
                        structured_voice_log(
                            "qwen.sync.audio_buffer_underrun",
                            room_id=self.room_id_getter(),
                            room_name=self.room_name,
                            user_identity=self.user_identity,
                            response_id=response_id,
                            reason="done_without_audio",
                        )
                    if buffered_done:
                        await self._flush_audio_tail(response_id)
                    continue
                if audio:
                    await self._publish_pcm_bytes(audio, response_id)
                if done:
                    await self._flush_audio_tail(response_id)
            finally:
                self._audio_queue.task_done()

    async def _collect_initial_audio(self, response_id: str, first_audio: bytes, first_done: bool) -> tuple[bytes, bool]:
        buffer_ms = max(0, QWEN_AUDIO_PLAYBACK_BUFFER_MS)
        if buffer_ms <= 0 or first_done:
            return first_audio, first_done
        started_at = time.monotonic()
        chunks = [first_audio] if first_audio else []
        done = False
        structured_voice_log(
            "qwen.sync.audio_buffer_started",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            response_id=response_id,
            target_buffer_ms=buffer_ms,
            audio_bytes=len(first_audio),
        )
        deadline = started_at + (buffer_ms / 1000.0)
        while not done:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                next_response_id, audio, item_done = await asyncio.wait_for(self._audio_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                if next_response_id != response_id:
                    if next_response_id:
                        self._cancelled_audio_response_ids.add(response_id)
                        with contextlib.suppress(asyncio.QueueFull):
                            self._audio_queue.put_nowait((next_response_id, audio, item_done))
                    done = True
                    break
                if audio:
                    chunks.append(audio)
                done = bool(item_done)
            finally:
                self._audio_queue.task_done()
        combined = b"".join(chunks)
        structured_voice_log(
            "qwen.sync.audio_buffer_released",
            room_id=self.room_id_getter(),
            room_name=self.room_name,
            user_identity=self.user_identity,
            response_id=response_id,
            target_buffer_ms=buffer_ms,
            wall_buffer_ms=int((time.monotonic() - started_at) * 1000),
            buffered_audio_ms=_pcm_duration_ms(len(combined)),
            audio_bytes=len(combined),
        )
        return combined, done

    async def _publish_pcm_bytes(self, audio: bytes, response_id: str) -> None:
        if not audio or self._audio_source is None:
            return
        for frame_pcm in self._audio_buffer.push(audio):
            await self._capture_frame_pcm(frame_pcm, response_id, tail=False)

    async def _flush_audio_tail(self, response_id: str) -> None:
        tail = self._audio_buffer.flush()
        if not tail:
            return
        frame_bytes = QWEN_REALTIME_OUTPUT_SAMPLE_RATE // 50 * 2
        padded = tail + (b"\x00" * max(0, frame_bytes - len(tail)))
        await self._capture_frame_pcm(padded, response_id, tail=True)

    async def _capture_frame_pcm(self, frame_pcm: bytes, response_id: str, *, tail: bool) -> None:
        if self._audio_source is None:
            return
        frame = rtc.AudioFrame(
            frame_pcm,
            sample_rate=QWEN_REALTIME_OUTPUT_SAMPLE_RATE,
            num_channels=1,
            samples_per_channel=len(frame_pcm) // 2,
        )
        try:
            capture_result = self._audio_source.capture_frame(frame)
            if inspect.isawaitable(capture_result):
                await capture_result
            self._log_frame_capture(response_id, len(frame_pcm), frame.samples_per_channel, tail=tail)
        except Exception as exc:
            structured_voice_log(
                "qwen.livekit.capture_frame_error",
                room_id=self.room_id_getter(),
                room_name=self.room_name,
                user_identity=self.user_identity,
                response_id=response_id,
                track_sid=self._published_track_sid,
                error_type=type(exc).__name__,
            )
            raise


def _topic_to_state(topic: str) -> str:
    return {
        QWEN_INTERNAL_TOPICS["input_partial"]: "qwen.input.transcript_partial",
        QWEN_INTERNAL_TOPICS["input_final"]: "qwen.input.transcript_final",
        QWEN_INTERNAL_TOPICS["output_delta"]: "qwen.output_text.delta",
        QWEN_INTERNAL_TOPICS["output_done"]: "qwen.output_text.done",
        QWEN_INTERNAL_TOPICS["response_started"]: "qwen.response.started",
        QWEN_INTERNAL_TOPICS["response_done"]: "qwen.response.done",
        QWEN_INTERNAL_TOPICS["response_cancelled"]: "qwen.response.cancelled",
        QWEN_INTERNAL_TOPICS["session_error"]: "qwen.session.error",
    }.get(topic, "")


def _pcm_duration_ms(byte_count: int) -> int:
    if byte_count <= 0:
        return 0
    return int(byte_count / (QWEN_REALTIME_OUTPUT_SAMPLE_RATE * 2) * 1000)
