import os
import re
from motor.motor_asyncio import AsyncIOMotorClient
from mcp.server.mcpserver.server import MCPServer

# MongoDB connection
MONGODB_URI = os.environ.get("MONGODB_URI")
DATABASE_NAME = "mcp_server"
COLLECTION_NAME = "todos"

client = None
db = None
todos_collection = None


def encode_mongo_uri(uri: str) -> str:
    """Encode special characters in MongoDB URI username/password."""
    if not uri:
        return uri

    from urllib.parse import quote_plus

    # Find the authority part after protocol
    if "://" in uri:
        protocol_end = uri.index("://") + 3
        protocol = uri[:protocol_end]
        rest = uri[protocol_end:]

        # Find the @ that separates userinfo from host
        # We need to find the LAST @ before the host part
        at_pos = rest.rfind("@")
        if at_pos != -1:
            userinfo = rest[:at_pos]
            host_part = rest[at_pos:]

            # Split userinfo by first colon
            colon_pos = userinfo.find(":")
            if colon_pos != -1:
                user = userinfo[:colon_pos]
                password = userinfo[colon_pos + 1:]
                encoded_user = quote_plus(user)
                encoded_password = quote_plus(password)
                return f"{protocol}{encoded_user}:{encoded_password}{host_part}"

    return uri


if MONGODB_URI:
    encoded_uri = encode_mongo_uri(MONGODB_URI)
    client = AsyncIOMotorClient(encoded_uri)
    db = client[DATABASE_NAME]
    todos_collection = db[COLLECTION_NAME]

VALID_GUEST_COLLECTIONS = {"engagement": "Guest_list_engagement", "marriage": "Guest_list_marriage"}

mcp = MCPServer(name="todo-server")


@mcp.tool()
async def get_todos() -> list[dict]:
    """Get the current todo list from MongoDB."""
    if todos_collection is None:
        return [{"error": "MongoDB not configured. Set MONGODB_URI environment variable."}]
    cursor = todos_collection.find({})
    todos = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        todos.append(doc)
    return todos


@mcp.tool()
async def add_todo(title: str) -> str:
    """Add a new todo item to the list in MongoDB."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    todo = {"title": title, "completed": False}
    result = await todos_collection.insert_one(todo)
    return f"Added todo: {title} (id: {result.inserted_id})"


@mcp.tool()
async def toggle_todo(title: str) -> str:
    """Toggle a todo item's completed status. Use this when the user wants to mark a todo as done/completed/finished, or mark a completed todo as not done/incomplete/pending. Pass the exact title of the todo item to toggle."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."

    doc = await todos_collection.find_one({"title": title})
    if not doc:
        available = await todos_collection.distinct("title")
        if available:
            return f"Todo '{title}' not found. Available todos: {', '.join(available)}"
        return f"Todo '{title}' not found. The todo list is empty."

    new_status = not doc.get("completed", False)
    await todos_collection.update_one({"_id": doc["_id"]}, {"$set": {"completed": new_status}})
    status_text = "completed" if new_status else "incomplete"
    return f"Todo '{title}' marked as {status_text}."


@mcp.tool()
async def get_guests(collection: str) -> list[dict]:
    """Get the guest list for a wedding event. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. By default only non-deleted (active) guests are returned."""
    if todos_collection is None:
        return [{"error": "MongoDB not configured. Set MONGODB_URI environment variable."}]
    coll_name = VALID_GUEST_COLLECTIONS.get(collection)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."}]
    coll = db[coll_name]
    cursor = coll.find({"deleted": {"$ne": True}})
    guests = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        guests.append(doc)
    return guests


@mcp.tool()
async def add_guest(collection: str, name: str) -> str:
    """Add a new guest to the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. The guest is added with isInvited set to false by default."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    coll_name = VALID_GUEST_COLLECTIONS.get(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."
    coll = db[coll_name]
    existing = await coll.find_one({"name": name, "deleted": {"$ne": True}})
    if existing:
        return f"Guest '{name}' already exists in {collection} list."
    deleted_doc = await coll.find_one({"name": name, "deleted": True})
    if deleted_doc:
        await coll.update_one({"_id": deleted_doc["_id"]}, {"$set": {"deleted": False, "isInvited": False}})
        return f"Guest '{name}' restored in {collection} list (was previously removed)."
    guest = {"name": name, "isInvited": False, "deleted": False}
    result = await coll.insert_one(guest)
    return f"Added guest '{name}' to {collection} list (id: {result.inserted_id})."


@mcp.tool()
async def remove_guest(collection: str, name: str) -> str:
    """Soft delete a guest from the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. The guest is not permanently removed but will not appear in default guest list queries."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    coll_name = VALID_GUEST_COLLECTIONS.get(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
        if active:
            return f"Guest '{name}' not found in {collection} list. Available guests: {', '.join(active)}"
        return f"Guest '{name}' not found. The {collection} guest list is empty."
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"deleted": True}})
    return f"Guest '{name}' removed from {collection} list."


@mcp.tool()
async def toggle_invited(collection: str, name: str) -> str:
    """Toggle a guest's invited status. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. Use this when the user wants to mark a guest as invited/not invited."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    coll_name = VALID_GUEST_COLLECTIONS.get(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'. Use 'engagement' or 'marriage'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
        if active:
            return f"Guest '{name}' not found in {collection} list. Available guests: {', '.join(active)}"
        return f"Guest '{name}' not found. The {collection} guest list is empty."
    new_status = not doc.get("isInvited", False)
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"isInvited": new_status}})
    status_text = "invited" if new_status else "not invited"
    return f"Guest '{name}' marked as {status_text} in {collection} list."


app = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True)
