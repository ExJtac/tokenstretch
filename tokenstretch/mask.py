"""Redact secrets in any value that will be printed or put in --json output."""
from __future__ import annotations

import re
from typing import Any

_KEY = re.compile(
    r"(api[_-]?key|auth(?:orization|enticat)|token|secret|password|passwd|credential|bearer|private[_-]?key)",
    re.I,
)
_VALUE = re.compile(
    r"(?i)(\bsk-[A-Za-z0-9_\-]{8,}|\bBearer\s+[A-Za-z0-9_\-\.]+|"
    r"\bkey-[A-Za-z0-9_\-]{8,}|\beyJ[A-Za-z0-9_\-]{20,})"
)


def mask_text(text: str) -> str:
    if not text:
        return text
    return _VALUE.sub("[redacted]", text)


def mask_value(key: str | None, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: mask_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_value(key, v) for v in value]
    s = value if isinstance(value, str) else str(value)
    if key and _KEY.search(str(key)):
        return "[redacted]"
    return mask_text(s)


def mask_finding_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return mask_value(None, evidence) if isinstance(evidence, dict) else {}
