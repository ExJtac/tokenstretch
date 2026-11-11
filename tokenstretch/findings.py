"""Adapter-agnostic finding and usage record types."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2, "info": 3}
CATEGORIES = ("waste", "tos", "privacy")


@dataclass
class Finding:
    id: str
    severity: str
    message: str
    fix: str
    source: str
    evidence: dict[str, Any] = field(default_factory=dict)
    category: str = "waste"
    slug: str = ""
    adapter_id: str = ""

    def to_json(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "severity": self.severity,
            "evidence": self.evidence,
            "message": self.message,
            "fix": self.fix,
            "source": self.source,
            "slug": self.slug,
            "category": self.category,
            "adapter": self.adapter_id,
        }
        return d


@dataclass
class UsageRecord:
    """Normalized usage. `input_tokens` is uncached input."""
    adapter_id: str
    source: str  # "log", "headless", or a file path for older callers
    task_id: str | None = None
    session_id: str | None = None
    ts: str | None = None
    model: str | None = None
    is_subagent: bool = False
    input_tokens: int = 0          # uncached
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None
    success: bool | None = None
    turns: int = 0
    unverified_semantics: bool = False
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def exit_code(findings: list[Finding]) -> int:
    """0 = clean, 1 = waste only (or lower-severity issues), 2 = ToS or privacy high."""
    if any(f.category in {"tos", "privacy"} and f.severity == "high" for f in findings):
        return 2
    actionable = [f for f in findings if f.severity in {"high", "med", "low"}]
    return 1 if actionable else 0
