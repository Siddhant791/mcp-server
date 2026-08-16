from mcp.server.mcpserver.server import MCPServer

mcp = MCPServer(name="todo-server")

todos: list[dict] = []


@mcp.tool()
def get_todos() -> list[dict]:
    """Get the current todo list."""
    return todos


@mcp.tool()
def add_todo(title: str) -> str:
    """Add a new todo item to the list."""
    todos.append({"title": title})
    return f"Added todo: {title}"


if __name__ == "__main__":
    print('MCP server started')
    import asyncio

    asyncio.run(mcp.run_stdio_async())
