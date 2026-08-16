import asyncio

from mcp_server.main import mcp


def main() -> None:
    asyncio.run(mcp.run_stdio_async())
