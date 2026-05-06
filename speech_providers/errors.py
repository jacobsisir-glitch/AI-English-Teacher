from __future__ import annotations


class SpeechProviderError(Exception):
    """Base error raised by a speech provider."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_text: str | None = None,
        exception_repr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_text = response_text
        self.exception_repr = exception_repr


class SpeechProviderConfigError(SpeechProviderError):
    """Raised when a provider is selected but required config is missing."""


class SpeechProviderRuntimeError(SpeechProviderError):
    """Raised when a provider request fails at runtime."""
