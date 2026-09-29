"""Errors safe to include in task state and intelligence artifacts."""

from __future__ import annotations

import os
import re


def redact_secrets(value: object) -> str:
    """Remove common credentials without reading the user's auth cache."""
    text = str(value)
    for name, secret in os.environ.items():
        if len(secret) >= 6 and any(
            marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text
    )
    text = re.sub(r"(?i)(bearer\s+)[^\s,\"']+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"authorization|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s,\"'&}]+",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"(?i)(https?://)[^/\s@]+:[^/\s@]+@", r"\1[REDACTED]@", text)


class IntelligenceError(RuntimeError):
    def __init__(self, stage: str, message: str):
        self.stage = stage
        self.message = redact_secrets(message)
        super().__init__(self.message)


LOGIN_INSTRUCTION = (
    "Run `uv run python -m app.intelligence.runtime login` locally and sign in "
    "with ChatGPT. For a headless machine, add `--device-auth`. Then retry. "
    "Codex never falls back to API-key billing."
)


class CodexAuthError(IntelligenceError):
    def __init__(self, message: str | None = None):
        super().__init__(
            "codex_auth",
            message or f"ChatGPT authentication is required. {LOGIN_INSTRUCTION}",
        )
