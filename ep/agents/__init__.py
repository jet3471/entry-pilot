"""Agent adapters. `get(name)` returns a shared adapter instance."""
from ep.agents.base import Adapter
from ep.agents.claude import ClaudeAdapter
from ep.agents.codex import CodexAdapter
from ep.agents.gemini import GeminiAdapter
from ep.agents.grok import GrokAdapter

ADAPTERS = {"claude": ClaudeAdapter(), "codex": CodexAdapter(), "gemini": GeminiAdapter(), "grok": GrokAdapter()}


def get(name: str) -> Adapter:
    return ADAPTERS[name]
