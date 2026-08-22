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
    """Get the guest list for a wedding event. Pass 'engagement' for engagement guests or 'marriage' for marriage guests, or any dynamically created collection name. By default only non-deleted (active) guests are returned."""
    guests = _get_guest_list(collection)
    if guests is None:
        return [{"error": f"Invalid collection '{collection}'."}]
    return [g for g in guests if not g.get("deleted", False)]


@mcp.tool()
def add_guest(collection: str, name: str, family_members: list[dict] = []) -> str:
    """Add a new guest to the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. Optionally include family_members as a list of objects with 'name' and 'relation' keys, e.g. [{"name": "Priya", "relation": "wife"}]. The guest is added with isInvited set to false by default."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'."
    active = [g for g in guests if not g.get("deleted", False)]
    for g in active:
        if g["name"] == name:
            return f"Guest '{name}' already exists in {collection} list."
    for g in guests:
        if g["name"] == name and g.get("deleted", False):
            g["deleted"] = False
            g["isInvited"] = False
            if family_members:
                existing_family = g.get("family_members", [])
                existing_names = {m["name"] for m in existing_family}
                for m in family_members:
                    if m["name"] not in existing_names:
                        existing_family.append({"name": m["name"], "relation": m.get("relation", "")})
                g["family_members"] = existing_family
            return f"Guest '{name}' restored in {collection} list (was previously removed)."
    guest: dict = {
        "name": name,
        "isInvited": False,
        "deleted": False,
        "family_members": [{"name": m["name"], "relation": m.get("relation", "")} for m in family_members] if family_members else [],
        "gifts_received": [],
    }
    guests.append(guest)
    return f"Added guest '{name}' to {collection} list."


@mcp.tool()
def remove_guest(collection: str, name: str) -> str:
    """Soft delete a guest from the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests, or any dynamically created collection name. The guest is not permanently removed but will not appear in default guest list queries."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'."
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
    """Toggle a guest's invited status. Pass 'engagement' for engagement guests or 'marriage' for marriage guests, or any dynamically created collection name. Use this when the user wants to mark a guest as invited/not invited."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'."
    for g in guests:
        if g["name"] == name and not g.get("deleted", False):
            g["isInvited"] = not g.get("isInvited", False)
            status_text = "invited" if g["isInvited"] else "not invited"
            return f"Guest '{name}' marked as {status_text} in {collection} list."
    active = [g["name"] for g in guests if not g.get("deleted", False)]
    if active:
        return f"Guest '{name}' not found in {collection} list. Available guests: {', '.join(active)}"
    return f"Guest '{name}' not found. The {collection} guest list is empty."


@mcp.tool()
def add_family_members(collection: str, guest_name: str, members: list[dict]) -> str:
    """Add family members to an existing guest entry. Pass 'engagement' or 'marriage' as collection. members should be a list of objects with 'name' and 'relation' keys, e.g. [{"name": "Priya", "relation": "wife"}, {"name": "Aarav", "relation": "son"}]. Duplicate family members (by name) are skipped."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'."
    doc = None
    for g in guests:
        if g["name"] == guest_name and not g.get("deleted", False):
            doc = g
            break
    if not doc:
        active = [g["name"] for g in guests if not g.get("deleted", False)]
        if active:
            return f"Guest '{guest_name}' not found in {collection} list. Available guests: {', '.join(active)}"
        return f"Guest '{guest_name}' not found. The {collection} guest list is empty."
    existing_family = doc.get("family_members", [])
    existing_names = {m["name"] for m in existing_family}
    added = []
    skipped = []
    for m in members:
        if m["name"] in existing_names:
            skipped.append(m["name"])
        else:
            existing_family.append({"name": m["name"], "relation": m.get("relation", "")})
            existing_names.add(m["name"])
            added.append(m["name"])
    doc["family_members"] = existing_family
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if skipped:
        parts.append(f"Skipped (already exist): {', '.join(skipped)}")
    return f"Updated family for '{guest_name}' in {collection} list. {'; '.join(parts)}."


@mcp.tool()
def get_family_members(collection: str, guest_name: str) -> list[dict]:
    """Get family members of a specific guest. Pass 'engagement' or 'marriage' as collection."""
    guests = _get_guest_list(collection)
    if guests is None:
        return [{"error": f"Invalid collection '{collection}'."}]
    for g in guests:
        if g["name"] == guest_name and not g.get("deleted", False):
            return g.get("family_members", [])
    return [{"error": f"Guest '{guest_name}' not found in {collection} list."}]


@mcp.tool()
def record_gift(collection: str, guest_name: str, amount: float, from_guest: str, occasion: str, date: str, note: str = "") -> str:
    """Record a shagun or gift received from a guest. Pass 'engagement' or 'marriage' as collection. guest_name is the person whose entry gets the record. from_guest is who gave the gift. amount is in rupees. occasion is e.g. 'marriage', 'engagement'. date format is YYYY-MM-DD. note is optional description."""
    guests = _get_guest_list(collection)
    if guests is None:
        return f"Invalid collection '{collection}'."
    doc = None
    for g in guests:
        if g["name"] == guest_name and not g.get("deleted", False):
            doc = g
            break
    if not doc:
        active = [g["name"] for g in guests if not g.get("deleted", False)]
        if active:
            return f"Guest '{guest_name}' not found in {collection} list. Available guests: {', '.join(active)}"
        return f"Guest '{guest_name}' not found. The {collection} guest list is empty."
    gift = {"amount": amount, "from_guest": from_guest, "occasion": occasion, "date": date, "note": note}
    doc.setdefault("gifts_received", []).append(gift)
    return f"Recorded Rs.{amount:.0f} shagun from '{from_guest}' for '{guest_name}' on {date} ({occasion})."


@mcp.tool()
def get_gifts(collection: str, guest_name: str = "") -> list[dict]:
    """Get gifts/shagun records. Pass 'engagement' or 'marriage' as collection. Optionally filter by guest_name to get gifts for a specific guest. If no guest_name is provided, returns all gifts across all guests in that collection."""
    guests = _get_guest_list(collection)
    if guests is None:
        return [{"error": f"Invalid collection '{collection}'."}]
    if guest_name:
        for g in guests:
            if g["name"] == guest_name and not g.get("deleted", False):
                gifts = g.get("gifts_received", [])
                for gift in gifts:
                    gift["guest_name"] = guest_name
                return gifts
        return [{"error": f"Guest '{guest_name}' not found in {collection} list."}]
    all_gifts = []
    for g in guests:
        if not g.get("deleted", False):
            for gift in g.get("gifts_received", []):
                gift["guest_name"] = g["name"]
                all_gifts.append(gift)
    return all_gifts


@mcp.tool()
def create_collection(name: str) -> str:
    """Create a new collection for any wedding-related activity (e.g. catering, decorator_payments, venue_bookings). The collection name should be descriptive with underscores. After creation, use it as the collection parameter in other guest tools."""
    clean_name = name.strip()
    if not clean_name:
        return "Collection name cannot be empty."
    if clean_name in guest_collections:
        return f"Collection '{clean_name}' already exists."
    guest_collections[clean_name] = []
    return f"Collection '{clean_name}' created successfully. You can now use '{clean_name}' as the collection parameter in guest tools."


if __name__ == "__main__":
    import sys

    if "--sse" in sys.argv:
        import uvicorn
        app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        import asyncio
        asyncio.run(mcp.run_stdio_async())
