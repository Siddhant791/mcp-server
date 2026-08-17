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


app = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True)
