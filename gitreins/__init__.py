"""GitReins — git-native AI agent co-harness (MCP server, static guards, agentic evaluator)."""

from engine.version import __version__ as __version__

# Declared so pyflakes recognises the re-export: it only honours ``__all__``,
# not the PEP 484 ``import X as X`` form (that convention is not applied to
# dunder names), so the bare re-export above is otherwise reported as unused.
__all__ = ["__version__"]
