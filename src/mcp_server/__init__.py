import asyncio
import sys

from mcp_server.main import mcp


def main() -> None:
    print("MCP server started")
    if "--sse" in sys.argv:
        import uvicorn
        app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        asyncio.run(mcp.run_stdio_async())
