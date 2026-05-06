from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Any


MSG_TYPE_FULL_CLIENT_REQUEST = 0x1
MSG_TYPE_AUDIO_ONLY_CLIENT = 0x2
MSG_TYPE_FULL_SERVER_RESPONSE = 0x9
MSG_TYPE_AUDIO_ONLY_SERVER = 0xB
MSG_TYPE_FRONT_END_RESULT_SERVER = 0xC
MSG_TYPE_ERROR = 0xF

FLAG_NO_SEQ = 0x0
FLAG_POSITIVE_SEQ = 0x1
FLAG_LAST_NO_SEQ = 0x2
FLAG_NEGATIVE_SEQ = 0x3
FLAG_WITH_EVENT = 0x4

SERIALIZATION_RAW = 0x0
SERIALIZATION_JSON = 0x1

COMPRESSION_NONE = 0x0
COMPRESSION_GZIP = 0x1

EVENT_NONE = 0
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_START_SESSION = 100
EVENT_CANCEL_SESSION = 101
EVENT_FINISH_SESSION = 102
EVENT_SESSION_STARTED = 150
EVENT_SESSION_CANCELED = 151
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_TASK_REQUEST = 200
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359

CONNECTION_EVENTS = {
    EVENT_START_CONNECTION,
    EVENT_FINISH_CONNECTION,
    EVENT_CONNECTION_STARTED,
    EVENT_CONNECTION_FAILED,
    EVENT_CONNECTION_FINISHED,
}


@dataclass
class VolcMessage:
    message_type: int
    flag: int
    serialization: int
    compression: int
    event: int = EVENT_NONE
    session_id: str = ""
    connect_id: str = ""
    sequence: int | None = None
    error_code: int | None = None
    payload: bytes = b""
    payload_msg: Any = None


def _int32(value: int) -> bytes:
    return int(value).to_bytes(4, "big", signed=True)


def _uint32(value: int) -> bytes:
    return int(value).to_bytes(4, "big", signed=False)


def _read_int32(data: bytes, offset: int) -> tuple[int, int]:
    return int.from_bytes(data[offset : offset + 4], "big", signed=True), offset + 4


def _read_uint32(data: bytes, offset: int) -> tuple[int, int]:
    return int.from_bytes(data[offset : offset + 4], "big", signed=False), offset + 4


def _encode_payload(payload: dict[str, Any] | str | bytes | None, compression: int) -> bytes:
    if payload is None:
        raw = b"{}"
    elif isinstance(payload, bytes):
        raw = payload
    elif isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if compression == COMPRESSION_GZIP:
        return gzip.compress(raw)
    return raw


def build_client_message(
    *,
    event: int,
    payload: dict[str, Any] | str | bytes | None = None,
    session_id: str = "",
    compression: int = COMPRESSION_NONE,
) -> bytes:
    payload_bytes = _encode_payload(payload, compression)
    header = bytes(
        [
            0x11,
            (MSG_TYPE_FULL_CLIENT_REQUEST << 4) | FLAG_WITH_EVENT,
            (SERIALIZATION_JSON << 4) | compression,
            0x00,
        ]
    )
    frame = bytearray(header)
    frame.extend(_int32(event))
    if event not in CONNECTION_EVENTS:
        session_bytes = session_id.encode("utf-8")
        frame.extend(_uint32(len(session_bytes)))
        frame.extend(session_bytes)
    frame.extend(_uint32(len(payload_bytes)))
    frame.extend(payload_bytes)
    return bytes(frame)


def build_start_connection_request() -> bytes:
    return build_client_message(event=EVENT_START_CONNECTION, payload={})


def build_finish_connection_request() -> bytes:
    return build_client_message(event=EVENT_FINISH_CONNECTION, payload={})


def build_start_session_request(session_id: str, payload: dict[str, Any]) -> bytes:
    return build_client_message(event=EVENT_START_SESSION, session_id=session_id, payload=payload)


def build_finish_session_request(session_id: str) -> bytes:
    return build_client_message(event=EVENT_FINISH_SESSION, session_id=session_id, payload={})


def build_task_request(session_id: str, payload: dict[str, Any]) -> bytes:
    return build_client_message(event=EVENT_TASK_REQUEST, session_id=session_id, payload=payload)


def parse_response(data: bytes) -> VolcMessage:
    if len(data) < 4:
        raise ValueError(f"response too short: {len(data)} bytes")

    header_size = data[0] & 0x0F
    message_type = data[1] >> 4
    flag = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    offset = header_size * 4
    message = VolcMessage(
        message_type=message_type,
        flag=flag,
        serialization=serialization,
        compression=compression,
    )

    if flag in {FLAG_POSITIVE_SEQ, FLAG_NEGATIVE_SEQ}:
        message.sequence, offset = _read_int32(data, offset)

    if message_type == MSG_TYPE_ERROR:
        message.error_code, offset = _read_uint32(data, offset)

    if flag == FLAG_WITH_EVENT:
        message.event, offset = _read_int32(data, offset)
        if message.event not in CONNECTION_EVENTS:
            session_len, offset = _read_uint32(data, offset)
            if session_len > 0:
                message.session_id = data[offset : offset + session_len].decode("utf-8", errors="replace")
                offset += session_len
        else:
            remaining_after_event = len(data) - offset
            if message.event in {EVENT_CONNECTION_STARTED, EVENT_CONNECTION_FAILED, EVENT_CONNECTION_FINISHED} and remaining_after_event >= 8:
                # Some server connection events include connect_id before payload.
                possible_len = int.from_bytes(data[offset : offset + 4], "big", signed=False)
                if possible_len <= remaining_after_event - 4:
                    message.connect_id = data[offset + 4 : offset + 4 + possible_len].decode("utf-8", errors="replace")
                    offset += 4 + possible_len

    if len(data) - offset >= 4:
        payload_len, offset = _read_uint32(data, offset)
        message.payload = data[offset : offset + payload_len]
    else:
        message.payload = b""

    if message.compression == COMPRESSION_GZIP and message.payload:
        message.payload = gzip.decompress(message.payload)

    if message.serialization == SERIALIZATION_JSON and message.payload:
        try:
            message.payload_msg = json.loads(message.payload.decode("utf-8", errors="replace"))
        except Exception:
            message.payload_msg = None

    return message
