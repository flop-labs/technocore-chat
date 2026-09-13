"""MCP front end for technocore-chat. See server.py for the tools and both transports.

Deliberately thin: importing `technocore_mcp.server` must keep meaning the module, so the
`MCPServer` instance living inside it is not re-exported here to shadow it.
"""

from .server import VERSION, main, streamable_http_app

__all__ = ["VERSION", "main", "streamable_http_app"]
