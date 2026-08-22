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
dynamic_collections: set[str] = set()
schemas_collection = None

mcp = MCPServer(name="todo-server")


def _resolve_collection(collection: str) -> str | None:
    if collection in VALID_GUEST_COLLECTIONS:
        return VALID_GUEST_COLLECTIONS[collection]
    if collection in dynamic_collections:
        return collection
    return None


async def _get_schema(collection: str) -> dict | None:
    if schemas_collection is None:
        return None
    doc = await schemas_collection.find_one({"name": collection})
    return doc.get("fields", {}) if doc else None


async def _ensure_schemas_loaded():
    global schemas_collection, dynamic_collections
    if db is None:
        return
    if schemas_collection is None:
        schemas_collection = db["_collection_schemas"]
    if not dynamic_collections:
        async for doc in schemas_collection.find({}):
            dynamic_collections.add(doc["name"])


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
    """Get the guest list for a wedding event. Pass 'engagement' for engagement guests or 'marriage' for marriage guests, or any dynamically created collection name. By default only non-deleted (active) guests are returned."""
    if todos_collection is None:
        return [{"error": "MongoDB not configured. Set MONGODB_URI environment variable."}]
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    coll = db[coll_name]
    cursor = coll.find({"deleted": {"$ne": True}})
    guests = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        guests.append(doc)
    return guests


@mcp.tool()
async def add_guest(collection: str, name: str, family_members: list[dict] = []) -> str:
    """Add a new guest to the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests. Optionally include family_members as a list of objects with 'name' and 'relation' keys, e.g. [{"name": "Priya", "relation": "wife"}]. The guest is added with isInvited set to false by default."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    coll = db[coll_name]
    existing = await coll.find_one({"name": name, "deleted": {"$ne": True}})
    if existing:
        return f"Guest '{name}' already exists in {collection} list."
    deleted_doc = await coll.find_one({"name": name, "deleted": True})
    if deleted_doc:
        update: dict = {"deleted": False, "isInvited": False}
        if family_members:
            existing_family = deleted_doc.get("family_members", [])
            existing_names = {m["name"] for m in existing_family}
            for m in family_members:
                if m["name"] not in existing_names:
                    existing_family.append({"name": m["name"], "relation": m.get("relation", "")})
            update["family_members"] = existing_family
        await coll.update_one({"_id": deleted_doc["_id"]}, {"$set": update})
        return f"Guest '{name}' restored in {collection} list (was previously removed)."
    guest: dict = {"name": name, "isInvited": False, "deleted": False, "family_members": [], "gifts_received": []}
    if family_members:
        guest["family_members"] = [{"name": m["name"], "relation": m.get("relation", "")} for m in family_members]
    result = await coll.insert_one(guest)
    return f"Added guest '{name}' to {collection} list (id: {result.inserted_id})."


@mcp.tool()
async def remove_guest(collection: str, name: str) -> str:
    """Soft delete a guest from the list. Pass 'engagement' for engagement guests or 'marriage' for marriage guests, or any dynamically created collection name. The guest is not permanently removed but will not appear in default guest list queries."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
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
    """Toggle a guest's invited status. Pass 'engagement' for engagement guests or 'marriage' for marriage guests, or any dynamically created collection name. Use this when the user wants to mark a guest as invited/not invited."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
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


@mcp.tool()
async def add_family_members(collection: str, guest_name: str, members: list[dict]) -> str:
    """Add family members to an existing guest entry. Pass 'engagement' or 'marriage' as collection. members should be a list of objects with 'name' and 'relation' keys, e.g. [{"name": "Priya", "relation": "wife"}, {"name": "Aarav", "relation": "son"}]. Duplicate family members (by name) are skipped."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
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
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"family_members": existing_family}})
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if skipped:
        parts.append(f"Skipped (already exist): {', '.join(skipped)}")
    return f"Updated family for '{guest_name}' in {collection} list. {'; '.join(parts)}."


@mcp.tool()
async def get_family_members(collection: str, guest_name: str) -> list[dict]:
    """Get family members of a specific guest. Pass 'engagement' or 'marriage' as collection."""
    if todos_collection is None:
        return [{"error": "MongoDB not configured. Set MONGODB_URI environment variable."}]
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    coll = db[coll_name]
    doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
    if not doc:
        return [{"error": f"Guest '{guest_name}' not found in {collection} list."}]
    return doc.get("family_members", [])


@mcp.tool()
async def record_gift(collection: str, guest_name: str, amount: float, from_guest: str, occasion: str, date: str, note: str = "") -> str:
    """Record a shagun or gift received from a guest. Pass 'engagement' or 'marriage' as collection. guest_name is the person whose entry gets the record. from_guest is who gave the gift. amount is in rupees. occasion is e.g. 'marriage', 'engagement'. date format is YYYY-MM-DD. note is optional description."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
        if active:
            return f"Guest '{guest_name}' not found in {collection} list. Available guests: {', '.join(active)}"
        return f"Guest '{guest_name}' not found. The {collection} guest list is empty."
    gift = {"amount": amount, "from_guest": from_guest, "occasion": occasion, "date": date, "note": note}
    await coll.update_one({"_id": doc["_id"]}, {"$push": {"gifts_received": gift}})
    return f"Recorded Rs.{amount:.0f} shagun from '{from_guest}' for '{guest_name}' on {date} ({occasion})."


@mcp.tool()
async def get_gifts(collection: str, guest_name: str = "") -> list[dict]:
    """Get gifts/shagun records. Pass 'engagement' or 'marriage' as collection. Optionally filter by guest_name to get gifts for a specific guest. If no guest_name is provided, returns all gifts across all guests in that collection."""
    if todos_collection is None:
        return [{"error": "MongoDB not configured. Set MONGODB_URI environment variable."}]
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    coll = db[coll_name]
    if guest_name:
        doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
        if not doc:
            return [{"error": f"Guest '{guest_name}' not found in {collection} list."}]
        gifts = doc.get("gifts_received", [])
        for g in gifts:
            g["guest_name"] = guest_name
        return gifts
    cursor = coll.find({"deleted": {"$ne": True}})
    all_gifts = []
    async for doc in cursor:
        for g in doc.get("gifts_received", []):
            g["guest_name"] = doc["name"]
            all_gifts.append(g)
    return all_gifts


@mcp.tool()
async def create_collection(name: str, fields: dict = {}) -> str:
    """Create a new collection with a defined schema for any wedding-related activity. name is the collection name (use underscores, e.g. 'vendors', 'catering', 'decorator_payments'). fields is a dict defining the schema with field names as keys and their default values, e.g. {"vendor_name": null, "place": null, "price": null, "description": null, "item": null, "comments": null}. All fields default to null if not provided. After creation, use add_record, get_records, update_record, and delete_record to manage data in this collection."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    clean_name = name.strip()
    if not clean_name:
        return "Collection name cannot be empty."
    all_known = set(VALID_GUEST_COLLECTIONS.values()) | dynamic_collections
    if clean_name in all_known:
        return f"Collection '{clean_name}' already exists."
    coll = db[clean_name]
    await coll.insert_one({"_init": True})
    await coll.delete_one({"_init": True})
    if schemas_collection is not None:
        await schemas_collection.insert_one({"name": clean_name, "fields": fields})
    dynamic_collections.add(clean_name)
    field_list = ", ".join(fields.keys()) if fields else "none (add fields when inserting records)"
    return f"Collection '{clean_name}' created with fields: {field_list}. Use add_record to add data, get_records to query, update_record to modify, and delete_record to remove entries."


@mcp.tool()
async def add_record(collection: str, data: dict) -> str:
    """Add a new record to any collection. Pass the collection name (predefined like 'engagement'/'marriage', or any dynamically created collection like 'vendors'). data is a dict of field-value pairs, e.g. {"vendor_name": "Sharma Caterers", "place": "Delhi", "price": 50000}. Fields not provided will default to null based on the collection schema. For guest collections, use the specialized add_guest tool instead."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    schema = await _get_schema(collection)
    if schema is not None:
        record = {field: data.get(field, default) for field, default in schema.items()}
        for key, value in data.items():
            if key not in record:
                record[key] = value
    else:
        record = dict(data)
    record.setdefault("deleted", False)
    coll = db[coll_name]
    result = await coll.insert_one(record)
    return f"Record added to '{collection}' (id: {result.inserted_id})."


@mcp.tool()
async def get_records(collection: str, filters: dict = {}) -> list[dict]:
    """Get records from any collection. Pass the collection name. Optionally pass filters as key-value pairs to narrow results, e.g. {"place": "Delhi"} or {"price": {"$gte": 10000}}. By default only non-deleted records are returned. For guest collections, use the specialized get_guests tool instead."""
    if todos_collection is None:
        return [{"error": "MongoDB not configured. Set MONGODB_URI environment variable."}]
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    coll = db[coll_name]
    query = {"deleted": {"$ne": True}}
    query.update(filters)
    cursor = coll.find(query)
    records = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        records.append(doc)
    return records


@mcp.tool()
async def update_record(collection: str, record_id: str, updates: dict) -> str:
    """Update fields on a specific record. Pass the collection name, the record's _id (string), and a dict of fields to update, e.g. {"price": 60000, "comments": "Updated quote"}. Only the provided fields are changed. For guest collections, use the specialized tools instead."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    from bson import ObjectId
    coll = db[coll_name]
    try:
        oid = ObjectId(record_id)
    except Exception:
        return f"Invalid record id '{record_id}'."
    result = await coll.update_one({"_id": oid}, {"$set": updates})
    if result.matched_count == 0:
        return f"Record '{record_id}' not found in '{collection}'."
    return f"Record '{record_id}' updated in '{collection}'."


@mcp.tool()
async def delete_record(collection: str, record_id: str) -> str:
    """Soft delete a record from any collection. Pass the collection name and the record's _id (string). The record stays in DB but is excluded from default queries. For guest collections, use the specialized remove_guest tool instead."""
    if todos_collection is None:
        return "Error: MongoDB not configured. Set MONGODB_URI environment variable."
    await _ensure_schemas_loaded()
    coll_name = _resolve_collection(collection)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    from bson import ObjectId
    coll = db[coll_name]
    try:
        oid = ObjectId(record_id)
    except Exception:
        return f"Invalid record id '{record_id}'."
    result = await coll.update_one({"_id": oid}, {"$set": {"deleted": True}})
    if result.matched_count == 0:
        return f"Record '{record_id}' not found in '{collection}'."
    return f"Record '{record_id}' deleted from '{collection}'."


app = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True)
