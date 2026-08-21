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


@mcp.tool()
def toggle_todo(title: str) -> str:
    """Toggle a todo item's completed status. Use this when the user wants to mark a todo as done/completed/finished, or mark a completed todo as not done/incomplete/pending. Pass the exact title of the todo item to toggle."""
    for todo in todos:
        if todo["title"] == title:
            todo["completed"] = not todo.get("completed", False)
            status_text = "completed" if todo["completed"] else "incomplete"
            return f"Todo '{title}' marked as {status_text}."

    available = [t["title"] for t in todos]
    if available:
        return f"Todo '{title}' not found. Available todos: {', '.join(available)}"
    return f"Todo '{title}' not found. The todo list is empty."


if __name__ == "__main__":
    import sys

    if "--sse" in sys.argv:
        import uvicorn
        app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        import asyncio
        asyncio.run(mcp.run_stdio_async())
