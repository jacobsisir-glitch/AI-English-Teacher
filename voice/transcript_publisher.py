from __future__ import annotations

import json
from typing import Any

from livekit import rtc


class LiveKitTranscriptPublisher:
    def __init__(self, room: rtc.Room, *, room_name: str, room_id: str) -> None:
        self.room = room
        self.room_name = room_name
        self.room_id = room_id

    async def publish_partial(self, user_identity: str, *, utterance_id: str, text: str) -> None:
        await self._send_topic(
            "stt.partial",
            user_identity,
            {
                "roomName": self.room_name,
                "roomId": self.room_id,
                "userIdentity": user_identity,
                "utteranceId": utterance_id,
                "text": text,
            },
        )

    async def publish_final(self, user_identity: str, *, utterance_id: str, text: str) -> None:
        await self._send_topic(
            "stt.final",
            user_identity,
            {
                "roomName": self.room_name,
                "roomId": self.room_id,
                "userIdentity": user_identity,
                "utteranceId": utterance_id,
                "text": text,
            },
        )

    async def publish_state(self, user_identity: str, *, state: str, payload: dict[str, Any] | None = None) -> None:
        message = {
            "roomName": self.room_name,
            "roomId": self.room_id,
            "userIdentity": user_identity,
            "state": state,
        }
        if payload:
            message.update(payload)
        await self._send_topic("voice.state", user_identity, message)

    async def _send_topic(self, topic: str, user_identity: str, payload: dict[str, Any]) -> None:
        if not self.room.isconnected():
            return
        await self.room.local_participant.send_text(
            json.dumps(payload, ensure_ascii=False),
            topic=topic,
            destination_identities=[user_identity],
        )
