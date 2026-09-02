"""Local read-only MCP server surface for OpenShard's run history.

Kept import-light: this package's ``__init__`` never imports the ``mcp``
SDK itself, so ``import openshard.mcp`` works even when the optional
``mcp`` dependency isn't installed. Only ``openshard.mcp.server`` (loaded
lazily by ``openshard mcp serve``) requires it.
"""
