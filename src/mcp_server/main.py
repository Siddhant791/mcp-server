from mcp.server.mcpserver.server import MCPServer

mcp = MCPServer(name="todo-server")

todos: list[dict] = []
guest_collections: dict[str, list[dict]] = {"engagement": [], "marriage": []}


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


def _get_guest_list(collection: str) -> list[dict] | None:
    if collection not in guest_collections:
        return None
    return guest_collections[collection]


@mcp.tool()
def get_guests(collection: str) -> list[dict]:
    """Get the guest list for a wedding event. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. By default only non-deleted (active) guests are returned."""
    guests = _get_guest_list(collection)
    if guests is None:
        return [{"error": f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."}]
    return [g for g in guests if not g.get("deleted", False)]


@mcp.tool()
def add_guest(collection: str, name: str) -> str:
    """Add a new guest to the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. The guest is added with isInvited set to false by default."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."
    active = [g for g in guests if not g.get("deleted", False)]
    for g in active:
        if g["name"] == name:
            return f"Guest '{name}' already exists in {collection} list."
    for g in guests:
        if g["name"] == name and g.get("deleted", False):
            g["deleted"] = False
            g["isInvited"] = False
            return f"Guest '{name}' restored in {collection} list (was previously removed)."
    guests.append({"name": name, "isInvited": False, "deleted": False})
    return f"Added guest '{name}' to {collection} list."


@mcp.tool()
def remove_guest(collection: str, name: str) -> str:
    """Soft delete a guest from the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. The guest is not permanently removed but will not appear in default guest list queries."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."
    for g in guests:
        if g["name"] == name and not g.get("deleted", False):
            g["deleted"] = True
            return f"Guest '{name}' removed from {collection} list."
    active = [g["name"] for g in guests if not g.get("deleted", False)]
    if active:
        return f"Guest '{name}' not found in {collection} list. Available guests: {', '.join(active)}"
    return f"Guest '{name}' not found. The {collection} guest list is empty."


@mcp.tool()
def toggle_invited(collection: str, name: str) -> str:
    """Toggle a guest's invited status. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. Use this when the user wants to mark a guest as invited/not invited."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."
    for g in guests:
        if g["name"] == name and not g.get("deleted", False):
            g["isInvited"] = not g.get("isInvited", False)
            status_text = "invited" if g["isInvited"] else "not invited"
            return f"Guest '{name}' marked as {status_text} in {collection} list."
    active = [g["name"] for g in guests if not g.get("deleted", False)]
    if active:
        return f"Guest '{name}' not found in {collection} list. Available guests: {', '.join(active)}"
    return f"Guest '{name}' not found. The {collection} guest list is empty."


if __name__ == "__main__":
    import sys

    if "--sse" in sys.argv:
        import uvicorn
        app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        import asyncio
        asyncio.run(mcp.run_stdio_async())
