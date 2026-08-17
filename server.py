import os
from motor.motor_asyncio import AsyncIOMotorClient
from mcp.server.mcpserver.server import MCPServer

# MongoDB connection
MONGODB_URI = os.environ.get("MONGODB_URI")
DATABASE_NAME = "mcp_server"
COLLECTION_NAME = "todos"

client = None
db = None
todos_collection = None

if MONGODB_URI:
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client[DATABASE_NAME]
    todos_collection = db[COLLECTION_NAME]

mcp = MCPServer(name="todo-server")


@mcp.tool()
async def get_todos() -> list[dict]:
    """Get the current todo list from MongoDB."""
    if not todos_collection:
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
    if not todos_collection:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    todo = {"title": title, "completed": False}
    result = await todos_collection.insert_one(todo)
    return f"Added todo: {title} (id: {result.inserted_id})"


app = mcp.sse_app()
