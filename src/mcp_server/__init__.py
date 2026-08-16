import asyncio

from mcp_server.main import mcp


def main() -> None:
    print("MCP server started")
    asyncio.run(mcp.run_stdio_async())
