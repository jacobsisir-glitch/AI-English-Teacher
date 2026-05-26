from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from voice.session_state import structured_voice_log


class UtteranceStatus(Enum):
    LISTENING = "listening"
    RECOGNIZING = "recognizing"
    FINAL_READY = "final_ready"
    PUBLISHED = "published"
    ACKED = "acked"
    SUBMITTED = "submitted"
    FAILED = "failed"


@dataclass
class UtteranceState:
    utterance_id: str
    room_name: str
    participant_id: str
    started_at: float = field(default_factory=time.time)
    speech_started_at: float = 0.0
    speech_ended_at: float | None = None
    last_non_empty_partial: str = ""
    offline_final_text: str = ""
    selected_text: str = ""
    selected_source: str = ""
    status: UtteranceStatus = UtteranceStatus.LISTENING
    ack_received: bool = False
    publish_attempts: int = 0
    last_publish_at: float = 0.0
    first_partial_at: float | None = None
    final_at: float | None = None

    @property
    def first_partial_ms(self) -> int | None:
        if self.first_partial_at is None or self.speech_started_at <= 0:
            return None
        return int((self.first_partial_at - self.speech_started_at) * 1000)

    @property
    def final_ms(self) -> int | None:
        if self.final_at is None or self.speech_started_at <= 0:
            return None
        return int((self.final_at - self.speech_started_at) * 1000)

    def to_log_payload(self) -> dict[str, Any]:
        return {
            "utterance_id": self.utterance_id,
            "room_name": self.room_name,
            "participant_id": self.participant_id,
            "status": self.status.value,
            "selected_text": self.selected_text,
            "selected_source": self.selected_source,
            "ack_received": self.ack_received,
            "publish_attempts": self.publish_attempts,
            "first_partial_ms": self.first_partial_ms,
            "final_ms": self.final_ms,
        }


@dataclass(frozen=True)
class CommitDecision:
    allowed: bool
    reason: str
    display_message: str = ""


@dataclass(frozen=True)
class CommitContext:
    mode: str = "normal_chat"
    class_mode: bool = False
    pending_question: bool = False
    pending_question_text: str = ""
    short_answer_allowed: bool = False


class CommitGate:
    """Gate all text that is allowed to become stt.final."""

    FINAL_SOURCES = {"2pass-offline", "offline", "offline_mode", "server_final", "server_final_flag", "final"}
    FALLBACK_SOURCES = {"partial_fallback", "timeout_partial_fallback", "timeout_partial_last_chance", "fallback"}
    NOISE_WORDS = {"嗯", "啊", "哦", "呃", "额", "唔", "um", "uh", "er", "ah", "oh", "eh", "hm", "hmm", "mmm"}
    SHORT_ANSWER_WHITELIST = {
        "birds",
        "bird",
        "subject",
        "verb",
        "object",
        "yes",
        "no",
        "ok",
        "okay",
        "主语",
        "谓语",
        "宾语",
        "动词",
        "名词",
        "好",
        "对",
        "可以",
        "不要",
    }
    FRAGMENT_BLOCKLIST = {"下你", "课程", "her", "lo", "loheris", "singgoing", "is every"}

    @classmethod
    def evaluate(
        cls,
        *,
        utterance_id: str,
        selected_text: str,
        source: str,
        utterance_duration_ms: int,
        context: CommitContext,
        has_offline_final: bool,
        has_server_final: bool,
        accumulated_partial_text: str,
    ) -> CommitDecision:
        del utterance_id, accumulated_partial_text
        text = cls._normalize_text(selected_text)
        source_key = cls._normalize_source(source)
        if not text:
            return CommitDecision(False, "empty_text", "语音识别没有拿到有效文字，请再说一遍。")
        if cls._is_noise(text):
            return CommitDecision(False, "noise_text", "我听到了一点声音，但像是语气词，请再说一遍。")

        if cls._is_fallback_source(source_key):
            if cls._allow_pending_question_fallback(text=text, context=context):
                return CommitDecision(True, "pending_question_short_fallback_allowed")
            return CommitDecision(False, "partial_fallback_blocked", _INCOMPLETE_MESSAGE)

        if not cls._is_final_source(source_key):
            return CommitDecision(False, "non_final_source", _INCOMPLETE_MESSAGE)

        if not (has_offline_final or has_server_final):
            return CommitDecision(False, "missing_final_marker", _INCOMPLETE_MESSAGE)

        fragment_reason = cls._fragment_reason(text, utterance_duration_ms)
        if fragment_reason:
            return CommitDecision(False, fragment_reason, _INCOMPLETE_MESSAGE)

        return CommitDecision(True, "final_source_allowed")

    @classmethod
    def _allow_pending_question_fallback(cls, *, text: str, context: CommitContext) -> bool:
        if not (context.class_mode and context.pending_question):
            return False
        if cls._is_noise(text):
            return False
        normalized = cls._normalize_text(text).lower()
        compact = re.sub(r"\s+", "", normalized)
        if normalized in cls.SHORT_ANSWER_WHITELIST or compact in cls.SHORT_ANSWER_WHITELIST:
            return True
        if context.short_answer_allowed:
            return cls._looks_like_short_answer(text)
        return False

    @classmethod
    def _looks_like_short_answer(cls, text: str) -> bool:
        if cls._contains_chinese(text):
            return 1 <= len(re.sub(r"\s+", "", text)) <= 6
        words = cls._english_words(text)
        return 1 <= len(words) <= 2 and all(1 <= len(word) <= 24 for word in words)

    @classmethod
    def _fragment_reason(cls, text: str, duration_ms: int) -> str:
        normalized = cls._normalize_text(text).lower()
        compact = re.sub(r"\s+", "", normalized)
        if normalized in cls.FRAGMENT_BLOCKLIST or compact in cls.FRAGMENT_BLOCKLIST:
            return "blocked_fragment_text"
        if duration_ms > 1500:
            if cls._contains_chinese(text) and len(re.sub(r"\s+", "", text)) < 4:
                return "too_short_chinese_for_duration"
            if not cls._contains_chinese(text) and len(cls._english_words(text)) < 2:
                return "too_short_english_for_duration"
        if not cls._contains_chinese(text):
            words = cls._english_words(text)
            if any(word in cls.FRAGMENT_BLOCKLIST for word in words):
                return "blocked_fragment_word"
            if len(words) == 1 and re.search(r"(loher|singgoing)", words[0]):
                return "garbled_english_fragment"
        return ""

    @classmethod
    def _is_final_source(cls, source: str) -> bool:
        return source in cls.FINAL_SOURCES or "offline" in source or "server_final" in source

    @classmethod
    def _is_fallback_source(cls, source: str) -> bool:
        return source in cls.FALLBACK_SOURCES or "fallback" in source

    @classmethod
    def _is_noise(cls, text: str) -> bool:
        normalized = cls._normalize_text(text).lower()
        return normalized in cls.NOISE_WORDS

    @staticmethod
    def _normalize_source(source: str) -> str:
        return str(source or "").strip().lower()

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip())

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text))

    @staticmethod
    def _english_words(text: str) -> list[str]:
        return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text.lower())


_INCOMPLETE_MESSAGE = "我听到了一部分，但还没拿到完整识别结果，请再说一遍。"


# Module-level ack registry: utterance_id → UtteranceManager
_ack_registry: dict[str, UtteranceManager] = {}


def receive_ack(utterance_id: str, text: str) -> bool:
    """Called by POST /api/voice/ack endpoint. Returns True if ack was delivered."""
    manager = _ack_registry.get(utterance_id)
    if manager is None:
        return False
    return manager.deliver_ack(utterance_id, text)


class UtteranceManager:
    """Manages utterance lifecycle for a single participant.

    Responsibilities:
    - Track utterance state through full lifecycle
    - TranscriptSelector: priority-based text selection
    - Ack mechanism with retry
    - Structured logging
    """

    def __init__(
        self,
        room_name: str,
        participant_id: str,
        log_context_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.room_name = room_name
        self.participant_id = participant_id
        self._log_context_provider = log_context_provider
        self._utterances: dict[str, UtteranceState] = {}
        self._active_utterance_id: str = ""
        self._ack_events: dict[str, asyncio.Event] = {}
        self._ack_texts: dict[str, str] = {}

    # ── utterance lifecycle ──────────────────────────────────────────

    def start_utterance(self, utterance_id: str, speech_start_ts: float) -> UtteranceState:
        self._active_utterance_id = utterance_id
        state = UtteranceState(
            utterance_id=utterance_id,
            room_name=self.room_name,
            participant_id=self.participant_id,
            started_at=time.time(),
            speech_started_at=speech_start_ts,
            status=UtteranceStatus.LISTENING,
        )
        self._utterances[utterance_id] = state
        self._ack_events.pop(utterance_id, None)
        self._ack_texts.pop(utterance_id, None)
        self._prune()
        self._log("utterance.created", utterance_id=utterance_id, speech_start_ts=speech_start_ts)
        return state

    def mark_speech_end(self, utterance_id: str, speech_end_ts: float) -> None:
        state = self._get(utterance_id)
        if state is None:
            return
        state.speech_ended_at = speech_end_ts
        state.status = UtteranceStatus.RECOGNIZING
        self._log("utterance.speech_ended", utterance_id=utterance_id, speech_end_ts=speech_end_ts)

    def record_partial(self, utterance_id: str, text: str) -> None:
        state = self._get(utterance_id)
        if state is None:
            return
        if state.first_partial_at is None:
            state.first_partial_at = time.time()
        if text.strip():
            state.last_non_empty_partial = text

    def record_offline_final(self, utterance_id: str, text: str) -> None:
        state = self._get(utterance_id)
        if state is None:
            return
        if text.strip():
            state.offline_final_text = text

    # ── TranscriptSelector ───────────────────────────────────────────

    @staticmethod
    def _source_rank(source: str) -> int:
        """Priority: offline final(10) > server_final(8) > online partial(6) > fallback(3).

        server_final means the message passed FunASR's is_final check but is not
        from the 2pass-offline pipeline — it is still a server-declared final
        result and therefore outranks streaming online partials.
        """
        s = source or ""
        if "offline" in s:
            return 10
        if "server_final" in s:
            return 8
        if "online" in s:
            return 6
        return 3

    @staticmethod
    def _is_noise(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True
        noise_words = {"嗯", "啊", "哦", "呃", "额", "唔", "um", "uh", "er", "ah", "oh", "eh", "hm", "hmm", "mmm"}
        if t.lower() in noise_words:
            return True
        return all(ch in noise_words or ch in (" ", ".", ",", "!") for ch in t.lower())

    def select_text(self, utterance_id: str, candidate_text: str, source: str) -> tuple[str, str] | None:
        """Apply priority rules to select the best transcript text.

        Returns (selected_text, effective_source) or None if the candidate should be discarded.
        Priority: offline final > late offline > stable partial fallback > nothing.
        Rules:
        - Non-empty can't be overwritten by empty
        - Noise words filtered (fallback to last non-empty partial)
        - Lower-priority sources don't replace higher-priority ones
        """
        state = self._get(utterance_id)
        if state is None:
            return None

        candidate_rank = self._source_rank(source)
        existing_rank = self._source_rank(state.selected_source) if state.selected_source else -1

        # Skip if we already have a higher-priority final
        if state.selected_text and existing_rank > candidate_rank:
            self._log(
                "transcript.selector.skipped.lower_priority",
                utterance_id=utterance_id,
                candidate_source=source,
                candidate_rank=candidate_rank,
                existing_source=state.selected_source,
                existing_rank=existing_rank,
            )
            return None

        # Don't overwrite non-empty with empty at same priority
        if existing_rank == candidate_rank and state.selected_text and not candidate_text.strip():
            self._log(
                "transcript.selector.skipped.empty_override",
                utterance_id=utterance_id,
                source=source,
            )
            return None

        # Duplicate at same priority with existing text
        if existing_rank == candidate_rank and state.selected_text:
            return None

        selected_text = candidate_text
        effective_source = source

        # Noise filter
        if self._is_noise(selected_text):
            fallback = state.last_non_empty_partial
            if fallback.strip() and not self._is_noise(fallback):
                self._log(
                    "transcript.selector.noise_filtered",
                    utterance_id=utterance_id,
                    original_text=selected_text,
                    fallback_text=fallback,
                )
                selected_text = fallback
                effective_source = f"{source}+noise_fallback"
            else:
                self._log(
                    "transcript.selector.skipped.noise_only",
                    utterance_id=utterance_id,
                    text=selected_text,
                )
                return None

        if not selected_text.strip():
            self._log("transcript.selector.skipped.empty", utterance_id=utterance_id)
            return None

        state.selected_text = selected_text
        state.selected_source = effective_source
        state.final_at = time.time()
        state.status = UtteranceStatus.FINAL_READY

        self._log(
            "transcript.selector.selected",
            utterance_id=utterance_id,
            text=selected_text,
            source=effective_source,
            candidate_source=source,
        )
        return selected_text, effective_source

    def get_fallback_text(self, utterance_id: str) -> str:
        """Return the best available partial text as fallback."""
        state = self._get(utterance_id)
        if state is None:
            return ""
        fallback = state.last_non_empty_partial
        if fallback.strip() and not self._is_noise(fallback):
            return fallback
        return ""

    # ── publish + ack ────────────────────────────────────────────────

    def mark_published(self, utterance_id: str) -> None:
        state = self._get(utterance_id)
        if state is None:
            return
        state.status = UtteranceStatus.PUBLISHED
        state.publish_attempts += 1
        state.last_publish_at = time.time()
        # Register for ack delivery
        _ack_registry[utterance_id] = self
        self._log(
            "transcript.publish.complete",
            utterance_id=utterance_id,
            text=state.selected_text,
            attempt=state.publish_attempts,
        )

    def create_ack_event(self, utterance_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._ack_events[utterance_id] = event
        return event

    def deliver_ack(self, utterance_id: str, text: str) -> bool:
        """Deliver ack from frontend. Returns True if utterance was found."""
        state = self._get(utterance_id)
        if state is None:
            return False
        state.ack_received = True
        state.status = UtteranceStatus.ACKED
        self._ack_texts[utterance_id] = text
        event = self._ack_events.get(utterance_id)
        if event is not None:
            event.set()
        self._log("transcript.publish.acked", utterance_id=utterance_id, text=text)
        return True

    async def wait_for_ack(self, utterance_id: str, timeout_ms: int) -> bool:
        """Wait for frontend ack. Returns True if acked, False if timeout."""
        state = self._get(utterance_id)
        if state is not None and state.ack_received:
            return True
        event = self._ack_events.get(utterance_id)
        if event is None:
            event = asyncio.Event()
            self._ack_events[utterance_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_ms / 1000)
            return True
        except asyncio.TimeoutError:
            return False

    def mark_submitted(self, utterance_id: str) -> None:
        state = self._get(utterance_id)
        if state is None:
            return
        state.status = UtteranceStatus.SUBMITTED

    def mark_failed(self, utterance_id: str, error: str) -> None:
        state = self._get(utterance_id)
        if state is None:
            return
        state.status = UtteranceStatus.FAILED
        self._log("transcript.publish.failed", utterance_id=utterance_id, error=error)

    # ── helpers ──────────────────────────────────────────────────────

    @property
    def active_utterance_id(self) -> str:
        return self._active_utterance_id

    def get_state(self, utterance_id: str) -> UtteranceState | None:
        return self._utterances.get(utterance_id)

    def clear_active(self) -> None:
        """Clear only the active utterance tracking state.

        Does NOT touch ack_registry, ack_events, or ack_texts — those are
        managed separately via cleanup_ack() so the frontend ack can still
        be delivered after the utterance is no longer active.
        """
        self._active_utterance_id = ""

    def cleanup_ack(self, utterance_id: str) -> None:
        """Remove ack-related state for a finished utterance.

        Called on: ack success, ack timeout (all retries exhausted),
        TTL prune, or participant/room disconnect.
        """
        _ack_registry.pop(utterance_id, None)
        self._ack_events.pop(utterance_id, None)
        self._ack_texts.pop(utterance_id, None)

    def reset(self) -> None:
        for uid in list(self._utterances.keys()):
            self.cleanup_ack(uid)
        self._utterances.clear()
        self._active_utterance_id = ""

    def _get(self, utterance_id: str) -> UtteranceState | None:
        return self._utterances.get(utterance_id)

    def _prune(self) -> None:
        if len(self._utterances) <= 32:
            return
        keep_ids = list(self._utterances.keys())[-16:]
        for uid in list(self._utterances.keys()):
            if uid not in keep_ids:
                self._utterances.pop(uid, None)
                self.cleanup_ack(uid)

    def _log(self, event: str, **payload: Any) -> None:
        context = self._log_context_provider() if self._log_context_provider else {}
        structured_voice_log(event, **context, **payload)
