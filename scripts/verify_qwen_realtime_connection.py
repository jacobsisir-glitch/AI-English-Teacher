from __future__ import annotations

import asyncio
import json
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (  # noqa: E402
    QWEN_REALTIME_API_KEY,
    QWEN_REALTIME_AUDIO_FORMAT,
    QWEN_REALTIME_MODEL,
    QWEN_REALTIME_REGION,
    QWEN_REALTIME_VAD_MODE,
    QWEN_REALTIME_VOICE,
    QWEN_REALTIME_WORKSPACE_ID,
)


CONNECT_TIMEOUT_S = 15
EVENT_TIMEOUT_S = 15


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    category: str
    message: str


def _build_beijing_url() -> str:
    workspace_id = QWEN_REALTIME_WORKSPACE_ID.strip()
    model = QWEN_REALTIME_MODEL.strip()
    return f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model={model}"


def _masked_workspace() -> str:
    workspace_id = QWEN_REALTIME_WORKSPACE_ID.strip()
    if len(workspace_id) <= 8:
        return "<workspace:redacted>"
    return f"{workspace_id[:3]}...{workspace_id[-3:]}"


def _masked_url() -> str:
    model = QWEN_REALTIME_MODEL.strip() or "<missing-model>"
    return f"wss://{_masked_workspace()}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model={model}"


def _missing_env() -> list[str]:
    missing = []
    if not QWEN_REALTIME_API_KEY.strip():
        missing.append("QWEN_REALTIME_API_KEY")
    if not QWEN_REALTIME_WORKSPACE_ID.strip():
        missing.append("QWEN_REALTIME_WORKSPACE_ID")
    if not QWEN_REALTIME_MODEL.strip():
        missing.append("QWEN_REALTIME_MODEL")
    if QWEN_REALTIME_REGION.strip().lower() != "beijing":
        missing.append("QWEN_REALTIME_REGION=beijing")
    return missing


def _session_update_payload() -> dict[str, Any]:
    session: dict[str, Any] = {
        "modalities": ["text", "audio"],
        "instructions": "You are Lumina. Reply briefly. This is a connection verification only.",
        "voice": QWEN_REALTIME_VOICE.strip() or "Tina",
        "input_audio_format": QWEN_REALTIME_AUDIO_FORMAT.strip() or "pcm",
        "output_audio_format": QWEN_REALTIME_AUDIO_FORMAT.strip() or "pcm",
    }
    vad_mode = QWEN_REALTIME_VAD_MODE.strip().lower()
    if vad_mode and vad_mode != "manual":
        session["turn_detection"] = {"type": vad_mode}
    return {
        "event_id": "verify_session_update",
        "type": "session.update",
        "session": session,
    }


def _classify_text(text: str) -> str:
    lowered = text.lower()
    if "401" in lowered or "unauthorized" in lowered or "authentication" in lowered or "api key" in lowered:
        return "401/鉴权失败"
    if "workspace" in lowered or "not found" in lowered:
        return "Workspace ID错误"
    if "permission" in lowered or "quota" in lowered or "access denied" in lowered or "forbidden" in lowered:
        return "模型无权限"
    if "model" in lowered and ("not" in lowered or "invalid" in lowered or "unsupported" in lowered):
        return "模型无权限"
    if "region" in lowered or "beijing" in lowered or "endpoint" in lowered:
        return "地域不匹配"
    if "session.update" in lowered or "invalid parameter" in lowered or "parameter" in lowered:
        return "session.update参数错误"
    return "WebSocket网络失败"


def _classify_invalid_status(exc: InvalidStatus) -> VerifyResult:
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in {401, 403}:
        return VerifyResult(False, "401/鉴权失败", f"server rejected WebSocket handshake with HTTP {status_code}")
    if status_code == 404:
        return VerifyResult(False, "Workspace ID错误", "server returned HTTP 404 for the realtime endpoint")
    return VerifyResult(False, _classify_text(str(exc)), f"server rejected WebSocket handshake: {exc}")


def _extract_error(payload: dict[str, Any]) -> VerifyResult:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    code = str(error.get("code") or payload.get("code") or "")
    message = str(error.get("message") or payload.get("message") or payload)
    category = _classify_text(f"{code} {message}")
    return VerifyResult(False, category, f"{code} {message}".strip())


async def _recv_json(ws, *, timeout_s: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


async def verify() -> VerifyResult:
    missing = _missing_env()
    if missing:
        return VerifyResult(False, "缺少环境变量", ", ".join(missing))

    url = _build_beijing_url()
    ssl_context = ssl.create_default_context()
    headers = {"Authorization": f"Bearer {QWEN_REALTIME_API_KEY.strip()}"}

    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            ssl=ssl_context,
            open_timeout=CONNECT_TIMEOUT_S,
            ping_interval=None,
            max_size=None,
        ) as ws:
            session_created = None
            while True:
                payload = await _recv_json(ws, timeout_s=EVENT_TIMEOUT_S)
                event_type = str(payload.get("type") or "")
                if event_type == "error":
                    return _extract_error(payload)
                if event_type == "session.created":
                    session_created = payload
                    break
                if event_type == "session.updated":
                    session_created = payload
                    break
            await ws.send(json.dumps(_session_update_payload(), ensure_ascii=False))
            while True:
                payload = await _recv_json(ws, timeout_s=EVENT_TIMEOUT_S)
                event_type = str(payload.get("type") or "")
                if event_type == "error":
                    return _extract_error(payload)
                if event_type == "session.updated":
                    session_id = ""
                    session = payload.get("session")
                    if isinstance(session, dict):
                        session_id = str(session.get("id") or "")
                    if not session_id and isinstance(session_created, dict):
                        created_session = session_created.get("session")
                        if isinstance(created_session, dict):
                            session_id = str(created_session.get("id") or "")
                    return VerifyResult(True, "握手成功", f"session update acknowledged; session_id={_mask_id(session_id)}")
    except InvalidStatus as exc:
        return _classify_invalid_status(exc)
    except ConnectionClosed as exc:
        category = _classify_text(str(exc))
        return VerifyResult(False, category, f"connection closed during verification: {exc}")
    except asyncio.TimeoutError:
        return VerifyResult(False, "WebSocket网络失败", "timed out waiting for realtime session event")
    except OSError as exc:
        return VerifyResult(False, "WebSocket网络失败", str(exc))
    except Exception as exc:
        return VerifyResult(False, _classify_text(str(exc)), repr(exc))


def _mask_id(value: str) -> str:
    value = value.strip()
    if not value:
        return "<none>"
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}...{value[-4:]}"


async def main() -> int:
    print("Qwen realtime connection verification")
    print(f"endpoint: {_masked_url()}")
    print(f"region: {QWEN_REALTIME_REGION.strip() or '<missing>'}")
    print(f"model: {QWEN_REALTIME_MODEL.strip() or '<missing>'}")
    result = await verify()
    status = "OK" if result.ok else "FAILED"
    print(f"result: {status}")
    print(f"category: {result.category}")
    print(f"detail: {result.message}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
