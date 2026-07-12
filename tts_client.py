from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from config import TTS_BASE_URL, TTS_DEFAULT_VOICE, TTS_ENABLED, TTS_PROVIDER, TTS_TIMEOUT_SECONDS, TTS_FALLBACK_ON_ERROR
from speech_providers import DoubaoTTSProvider, SpeechProviderError


logger = logging.getLogger("uvicorn.error")


@dataclass
class TTSClientError(Exception):
    status_code: int
    detail: str
    method: str = ""
    base_url: str = ""
    request_url: str = ""
    response_text: str = ""
    exception_repr: str = ""

    def __str__(self) -> str:
        return self.detail


_doubao_provider: DoubaoTTSProvider | None = None


def _log_event(event: str, **payload: Any) -> None:
    try:
        logger.info(json.dumps({"event": event, **payload}, ensure_ascii=False))
    except Exception:
        logger.info("%s %s", event, payload)


def _provider_name() -> str:
    return (TTS_PROVIDER or "local_melo").strip().lower()


def _doubao() -> DoubaoTTSProvider:
    global _doubao_provider
    if _doubao_provider is None:
        _doubao_provider = DoubaoTTSProvider()
    return _doubao_provider


def _base_url() -> str:
    return TTS_BASE_URL.rstrip("/")


def _build_url(path: str) -> str:
    return f"{_base_url()}/{path.lstrip('/')}"


def _truncate_text(text: str, limit: int = 300) -> str:
    normalized = (text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "...<truncated>"


def _legacy_voice(voice: str | None) -> str:
    requested = (voice or "").strip()
    if requested in {"", "doubao_default"}:
        return TTS_DEFAULT_VOICE
    return requested


async def list_voices() -> dict[str, Any]:
    if not TTS_ENABLED:
        raise TTSClientError(status_code=503, detail="TTS is disabled.")

    provider = _provider_name()
    _log_event("tts.backend.select", provider=provider, operation="voices")
    if provider == "doubao":
        try:
            return await _doubao().list_voices()
        except SpeechProviderError as exc:
            _log_event(
                "tts.doubao.error",
                provider=provider,
                reason=str(exc),
                status_code=exc.status_code,
                response_text=exc.response_text,
                exception_repr=exc.exception_repr,
            )
            if TTS_FALLBACK_ON_ERROR:
                _log_event(
                    "tts.voice.fallback",
                    provider=provider,
                    fallback_provider="local_melo",
                    reason="TTS_FALLBACK_ON_ERROR=true",
                )
                legacy = await _local_list_voices()
                if isinstance(legacy, dict):
                    legacy["provider_error"] = str(exc)
                    legacy["active_provider"] = "local_melo"
                return legacy
            raise TTSClientError(
                status_code=exc.status_code or 502,
                detail=str(exc),
                method="GET",
                base_url="doubao",
                request_url=getattr(_doubao(), "v3_endpoint", "") or "doubao",
                response_text=exc.response_text or "",
                exception_repr=exc.exception_repr or "",
            ) from exc

    if provider == "local_melo":
        return await _local_list_voices()

    raise TTSClientError(status_code=500, detail=f"Unsupported TTS_PROVIDER: {provider}")


async def synthesize_speech(text: str, voice: str, lang: str, speed: float) -> tuple[bytes, str]:
    if not TTS_ENABLED:
        raise TTSClientError(status_code=503, detail="TTS is disabled.")

    provider = _provider_name()
    _log_event(
        "tts.backend.select",
        provider=provider,
        operation="speak",
        text_length=len(text or ""),
        voice=voice,
        lang=lang,
    )

    if provider == "doubao":
        try:
            result = await _doubao().synthesize_speech(text=text, voice=voice, lang=lang, speed=speed)
            return result.audio, result.media_type
        except SpeechProviderError as exc:
            _log_event(
                "tts.doubao.error",
                provider=provider,
                text_length=len(text or ""),
                voice=voice,
                lang=lang,
                reason=str(exc),
                status_code=exc.status_code,
                response_text=exc.response_text,
                exception_repr=exc.exception_repr,
            )
            if TTS_FALLBACK_ON_ERROR:
                _log_event(
                    "tts.voice.fallback",
                    provider=provider,
                    fallback_provider="local_melo",
                    reason="TTS_FALLBACK_ON_ERROR=true",
                )
                return await _local_synthesize_speech(text=text, voice=_legacy_voice(voice), lang=lang, speed=speed)
            raise TTSClientError(
                status_code=exc.status_code or 502,
                detail=str(exc),
                method="POST",
                base_url="doubao",
                request_url="https://openspeech.bytedance.com/api/v1/tts",
                response_text=exc.response_text or "",
                exception_repr=exc.exception_repr or "",
            ) from exc

    if provider == "local_melo":
        return await _local_synthesize_speech(text=text, voice=_legacy_voice(voice), lang=lang, speed=speed)

    raise TTSClientError(status_code=500, detail=f"Unsupported TTS_PROVIDER: {provider}")


async def stream_speech(text: str, voice: str, lang: str, speed: float) -> AsyncIterator[bytes]:
    if not TTS_ENABLED:
        raise TTSClientError(status_code=503, detail="TTS is disabled.")

    provider = _provider_name()
    _log_event(
        "tts.backend.select",
        provider=provider,
        operation="stream",
        text_length=len(text or ""),
        voice=voice,
        lang=lang,
    )
    if provider != "doubao":
        raise TTSClientError(status_code=501, detail=f"TTS streaming is not supported by provider: {provider}")

    try:
        async for chunk in _doubao().stream_speech(text=text, voice=voice, lang=lang, speed=speed):
            yield chunk
    except SpeechProviderError as exc:
        raise TTSClientError(
            status_code=exc.status_code or 502,
            detail=str(exc),
            method="WEBSOCKET",
            base_url="doubao",
            request_url=getattr(_doubao(), "v3_endpoint", "") or "doubao",
            response_text=exc.response_text or "",
            exception_repr=exc.exception_repr or "",
        ) from exc


async def _local_list_voices() -> dict[str, Any]:
    method = "GET"
    request_url = _build_url("/voices")
    try:
        async with httpx.AsyncClient(timeout=TTS_TIMEOUT_SECONDS, trust_env=False) as client:
            response = await client.get(request_url)
    except httpx.HTTPError as exc:
        raise TTSClientError(
            status_code=502,
            detail=f"Failed to reach legacy TTS service: {exc}",
            method=method,
            base_url=_base_url(),
            request_url=request_url,
            exception_repr=repr(exc),
        ) from exc

    if response.status_code >= 400:
        raise TTSClientError(
            status_code=response.status_code,
            detail=_extract_error_detail(response),
            method=method,
            base_url=_base_url(),
            request_url=request_url,
            response_text=_truncate_text(response.text),
        )
    payload = response.json()
    if isinstance(payload, dict):
        payload.setdefault("active_provider", "local_melo")
    return payload


async def _local_synthesize_speech(text: str, voice: str, lang: str, speed: float) -> tuple[bytes, str]:
    method = "POST"
    request_url = _build_url("/speak")
    payload = {
        "text": text,
        "voice": voice,
        "lang": lang,
        "speed": speed,
    }
    try:
        async with httpx.AsyncClient(timeout=TTS_TIMEOUT_SECONDS, trust_env=False) as client:
            response = await client.post(request_url, json=payload)
    except httpx.HTTPError as exc:
        raise TTSClientError(
            status_code=502,
            detail=f"Failed to reach legacy TTS service: {exc}",
            method=method,
            base_url=_base_url(),
            request_url=request_url,
            exception_repr=repr(exc),
        ) from exc

    if response.status_code >= 400:
        raise TTSClientError(
            status_code=response.status_code,
            detail=_extract_error_detail(response),
            method=method,
            base_url=_base_url(),
            request_url=request_url,
            response_text=_truncate_text(response.text),
        )
    return response.content, response.headers.get("content-type", "audio/wav")


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"TTS service returned HTTP {response.status_code}"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if detail:
            return str(detail)
    return str(payload)
