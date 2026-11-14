"""The adapters, in order. Adding a tool = one module + one line here. Read through `adapters`."""
from .aider import AiderAdapter
from .cline import ClineAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .copilot_cli import CopilotCliAdapter
from .cursor import CursorAdapter
from .gemini_cli import GeminiCliAdapter
from .host import HostAdapter, implemented_check_ids
from .opencode import OpenCodeAdapter

REGISTRY = [
    HostAdapter(),
    ClaudeCodeAdapter(),
    CodexAdapter(),
    GeminiCliAdapter(),
    CopilotCliAdapter(),
    CursorAdapter(),
    AiderAdapter(),
    OpenCodeAdapter(),
    ClineAdapter(),
]

__all__ = ["REGISTRY", "implemented_check_ids"]
