import os
from motor.motor_asyncio import AsyncIOMotorClient
from mcp.server.mcpserver.server import MCPServer

# MongoDB connection
MONGODB_URI = os.environ.get(
    "MONGODB_URI",
    "mongodb+srv://Wedding:<db_password>@wedding.emrolht.mongodb.net/?appName=Wedding"
)
DATABASE_NAME = "mcp_server"
COLLECTION_NAME = "todos"

client = AsyncIOMotorClient(MONGODB_URI)
db = client[DATABASE_NAME]
todos_collection = db[COLLECTION_NAME]

mcp = MCPServer(name="todo-server")


@mcp.tool()
async def get_todos() -> list[dict]:
    """Get the current todo list from MongoDB."""
    cursor = todos_collection.find({})
    todos = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        todos.append(doc)
    return todos


@mcp.tool()
async def add_todo(title: str) -> str:
    """Add a new todo item to the list in MongoDB."""
    todo = {"title": title, "completed": False}
    result = await todos_collection.insert_one(todo)
    return f"Added todo: {title} (id: {result.inserted_id})"


app = mcp.sse_app(host="0.0.0.0")
