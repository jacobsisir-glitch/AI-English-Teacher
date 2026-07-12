from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any


voice_logger = logging.getLogger("uvicorn.error")


def structured_voice_log(event: str, **payload: Any) -> None:
    record = {"event": event, **payload}
    voice_logger.info(json.dumps(record, ensure_ascii=False, default=str))


@dataclass
class UtteranceTelemetry:
    room_name: str
    room_id: str
    user_identity: str
    utterance_id: str
    speech_start_ts: float
    speech_start_monotonic: float
    speech_end_ts: float | None = None
    asr_first_partial_ms: int | None = None
    asr_final_ms: int | None = None

    def mark_first_partial(self) -> None:
        if self.asr_first_partial_ms is None:
            self.asr_first_partial_ms = int((time.monotonic() - self.speech_start_monotonic) * 1000)

    def mark_final(self) -> None:
        self.asr_final_ms = int((time.monotonic() - self.speech_start_monotonic) * 1000)

    def to_log_payload(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "room_name": self.room_name,
            "user_identity": self.user_identity,
            "utterance_id": self.utterance_id,
            "speech_start_ts": self.speech_start_ts,
            "speech_end_ts": self.speech_end_ts,
            "asr_first_partial_ms": self.asr_first_partial_ms,
            "asr_final_ms": self.asr_final_ms,
        }


@dataclass
class ParticipantSessionState:
    user_identity: str
    room_name: str
    track_sid: str
    last_partial_text: str = ""
    last_final_text: str = ""
    active_utterance_id: str = ""
    telemetry: UtteranceTelemetry | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class RoomSessionState:
    room_name: str
    worker_identity: str
    room_id: str = ""
    connected: bool = False
    status: str = "idle"
    last_error: str = ""
    local_participant_identity: str = ""
    last_stage: str = ""
    last_vad_event: str = ""
    last_audio_chunk_sent: str = ""
    last_flush_sent: str = ""
    last_funasr_message: str = ""
    last_funasr_final: str = ""
    participants: dict[str, ParticipantSessionState] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
