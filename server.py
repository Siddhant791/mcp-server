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


app = mcp.sse_app()
