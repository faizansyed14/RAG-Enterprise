"""RAG engine SDK (vendored, local-mode tree indexing + tree-search chat)."""
from typing import TYPE_CHECKING as _TYPE_CHECKING

from .chat_stream import ChatStream
from .client import RagEngineClient, RagEngineLocalClient
from .errors import RagEngineError
from .types import (ChatConfig, ChatProcessOptions, CloudIndexConfig,
                    IndexConfig, LocalIndexConfig)

if _TYPE_CHECKING:
    from .flash import tree_flash
    from .imaging import highlight_region
    from .tree_pdf import build_tree_pdf, tree_pdf_main
    from .tree_markdown import md_to_tree
    from .tree_optimize import optimize_tree

__all__ = [
    "RagEngineClient", "RagEngineLocalClient",
    "RagEngineError",
    "IndexConfig", "CloudIndexConfig", "LocalIndexConfig", "ChatConfig",
    "ChatProcessOptions", "ChatStream",
    "build_tree_pdf", "tree_pdf_main", "tree_flash",
    "optimize_tree", "md_to_tree", "highlight_region",
]

_LAZY = {
    "highlight_region": ".imaging",
    "tree_flash": ".flash",
    "optimize_tree": ".tree_optimize",
    "md_to_tree": ".tree_markdown",
}
_SUBMODULES = {"agent_tools", "chat_stream", "client", "errors",
               "flash", "imaging", "integrations", "local_api", "local_chat",
               "mcp_bridge", "tree_pdf",
               "tree_markdown", "tree_optimize", "types", "utils"}


def __getattr__(name):
    if name.startswith("_"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    if name in _SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    module = importlib.import_module(_LAZY.get(name, ".tree_pdf"), __name__)
    try:
        value = getattr(module, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__) | _SUBMODULES)
