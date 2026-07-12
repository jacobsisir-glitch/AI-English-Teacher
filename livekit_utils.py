from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4

from livekit import api

from config import (
    FUNASR_WS_URL,
    LIVEKIT_API_KEY,
    LIVEKIT_API_SECRET,
    LIVEKIT_WS_URL,
    VOICE_CONVERSATION_PROVIDER,
    VOICE_DEFAULT_ROOM,
)


_IDENTITY_CLEAN_RE = re.compile(r"[^A-Za-z0-9_.:@-]+")
_ROOM_CLEAN_RE = re.compile(r"[^A-Za-z0-9_.:@/-]+")


@dataclass(frozen=True)
class LiveKitTokenPayload:
    server_url: str
    participant_token: str
    room_name: str
    identity: str

    def as_response(self) -> dict[str, str]:
        return {
            "serverUrl": self.server_url,
            "participantToken": self.participant_token,
            "roomName": self.room_name,
            "identity": self.identity,
        }


def livekit_is_configured() -> bool:
    return bool(LIVEKIT_WS_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)


def livekit_voice_stack_is_configured() -> bool:
    if VOICE_CONVERSATION_PROVIDER == "qwen_omni_realtime":
        return livekit_is_configured()
    return livekit_is_configured() and bool(FUNASR_WS_URL)


def _normalize_identity(raw_value: str | None) -> str:
    candidate = _IDENTITY_CLEAN_RE.sub("-", str(raw_value or "").strip()).strip("-._:@")
    if candidate:
        return candidate[:96]
    return f"web-{uuid4().hex[:12]}"


def _normalize_room_name(raw_value: str | None) -> str:
    candidate = _ROOM_CLEAN_RE.sub("-", str(raw_value or "").strip()).strip("-._:/")
    if candidate:
        return candidate[:128]
    return VOICE_DEFAULT_ROOM


def _normalize_display_name(raw_value: str | None, *, fallback_identity: str) -> str:
    candidate = str(raw_value or "").strip()
    if candidate:
        return candidate[:128]
    return fallback_identity


def create_livekit_participant_token(
    *,
    room_name: str | None = None,
    user_id: str | None = None,
    display_name: str | None = None,
) -> LiveKitTokenPayload:
    if not livekit_is_configured():
        raise RuntimeError(
            "LiveKit environment is not fully configured. "
            "Please set LIVEKIT_WS_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET."
        )

    identity = _normalize_identity(user_id)
    resolved_room_name = _normalize_room_name(room_name)
    resolved_display_name = _normalize_display_name(display_name, fallback_identity=identity)

    grants = api.VideoGrants(
        room_join=True,
        room=resolved_room_name,
        can_publish=True,
        can_publish_data=True,
    )
    participant_token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(resolved_display_name)
        .with_grants(grants)
        .to_jwt()
    )

    return LiveKitTokenPayload(
        server_url=LIVEKIT_WS_URL,
        participant_token=participant_token,
        room_name=resolved_room_name,
        identity=identity,
    )


def create_livekit_worker_token(
    *,
    room_name: str | None = None,
    user_id: str | None = None,
    display_name: str | None = None,
) -> LiveKitTokenPayload:
    if not livekit_is_configured():
        raise RuntimeError(
            "LiveKit environment is not fully configured. "
            "Please set LIVEKIT_WS_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET."
        )

    identity = _normalize_identity(user_id or f"voice-worker-{uuid4().hex[:10]}")
    resolved_room_name = _normalize_room_name(room_name)
    resolved_display_name = _normalize_display_name(display_name or "AI Teacher Voice Worker", fallback_identity=identity)

    grants = api.VideoGrants(
        room_join=True,
        room=resolved_room_name,
        can_publish=VOICE_CONVERSATION_PROVIDER == "qwen_omni_realtime",
        can_subscribe=True,
        can_publish_data=True,
        hidden=VOICE_CONVERSATION_PROVIDER != "qwen_omni_realtime",
        agent=True,
    )
    participant_token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(resolved_display_name)
        .with_grants(grants)
        .to_jwt()
    )

    return LiveKitTokenPayload(
        server_url=LIVEKIT_WS_URL,
        participant_token=participant_token,
        room_name=resolved_room_name,
        identity=identity,
    )
