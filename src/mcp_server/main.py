from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.mcpserver.server import MCPServer

from auth.middleware import get_current_user, require_master
from auth.models import AuthContext, FamilyMember, FAMILY_DEFAULT_PERMISSIONS, CollectionMetadata, generate_user_id

mcp = MCPServer(name="wedding-mcp-server")

_users: dict[str, dict] = {}
_user_collections: dict[str, dict[str, list[dict]]] = {}
_user_schemas: dict[str, dict[str, dict]] = {}
_user_collection_metadata: dict[str, list[dict]] = {}  # master_user_id -> list of metadata dicts

# Track master users whose family members have been migrated to new defaults
_migrated_masters: set[str] = set()

VALID_GUEST_COLLECTIONS = {"engagement": "Guest_list_engagement", "marriage": "Guest_list_marriage"}
DEFAULT_SCHEMAS = {"Guest_list_engagement": {}, "Guest_list_marriage": {}}


def _get_collections(ctx: AuthContext) -> dict[str, list[dict]]:
    key = ctx.master_user_id
    if key not in _user_collections:
        _user_collections[key] = {s: [] for s in DEFAULT_SCHEMAS}
    return _user_collections[key]


def _resolve(collection: str, ctx: AuthContext) -> str | None:
    colls = _get_collections(ctx)
    if collection in VALID_GUEST_COLLECTIONS:
        actual = VALID_GUEST_COLLECTIONS[collection]
        return actual if actual in colls else None
    if collection in colls:
        return collection
    return None


def _get_schema(collection: str, ctx: AuthContext) -> dict | None:
    return _user_schemas.get(ctx.master_user_id, {}).get(collection)


def _check_perm(ctx: AuthContext, perm: str) -> str | None:
    if ctx.role == "master":
        return None
    master = _users.get(ctx.master_user_id)
    if not master:
        return "Master user not found."

    # Auto-migrate existing family members to new permission defaults (run once per master user)
    if ctx.master_user_id not in _migrated_masters:
        _migrated_masters.add(ctx.master_user_id)
        for m in master.get("family_members", []):
            new_perms = dict(FAMILY_DEFAULT_PERMISSIONS)
            new_perms.update(m.get("permissions", {}))
            new_perms["can_add_family_members"] = False
            m["permissions"] = new_perms

    for m in master.get("family_members", []):
        if m["email"] == ctx.email:
            if not m.get("permissions", {}).get(perm, True):
                return f"Permission denied: {perm} is disabled."
            return None
    return None


# ---------------------------------------------------------------------------
# Collection metadata helpers (in-memory)
# ---------------------------------------------------------------------------
def _save_collection_metadata(metadata: CollectionMetadata) -> None:
    """Save or update collection metadata for a user."""
    user_id = metadata.user_id
    meta_list = _user_collection_metadata.setdefault(user_id, [])
    for i, existing in enumerate(meta_list):
        if existing["collection_name"] == metadata.collection_name:
            meta_list[i] = metadata.to_dict()
            return
    meta_list.append(metadata.to_dict())


def _get_all_user_metadata(user_id: str) -> list[CollectionMetadata]:
    """Get all collection metadata for a user."""
    return [
        CollectionMetadata.from_dict(m)
        for m in _user_collection_metadata.get(user_id, [])
    ]


def _search_collection_metadata(user_id: str, query: str) -> list[dict]:
    """Search collection metadata by text matching against name, description, and categories."""
    all_metadata = _get_all_user_metadata(user_id)
    query_lower = query.lower()
    query_words = set(query_lower.split())

    scored_results = []
    for meta in all_metadata:
        score = 0
        searchable = f"{meta.collection_name} {meta.description} {meta.searchable_text} {' '.join(meta.categories)}".lower()

        for word in query_words:
            if word in meta.collection_name.lower():
                score += 3
            if word in meta.description.lower():
                score += 2
            if word in " ".join(meta.categories).lower():
                score += 1
            if word in searchable:
                score += 1

        # Substring match: entire query appears in name or description
        if query_lower in meta.collection_name.lower():
            score += 5
        if query_lower in meta.description.lower():
            score += 3

        if score > 0:
            scored_results.append({
                "collection_name": meta.collection_name,
                "description": meta.description,
                "score": score,
                "metadata_status": meta.metadata_status,
            })

    scored_results.sort(key=lambda x: x["score"], reverse=True)
    return scored_results


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
@mcp.tool()
def register_user(email: str, name: str) -> str:
    """Register yourself. If your email is not in any family list, you become master. Otherwise you get family access."""
    for uid, u in _users.items():
        if u["email"] == email:
            return f"Already registered as {u['role']}. user_id: {uid}"
    for u in _users.values():
        if u.get("role") == "master" and email in u.get("family_emails", []):
            uid = generate_user_id()
            _users[uid] = {"email": email, "name": name, "role": "family", "user_id": uid, "master_user_id": u["user_id"], "created_at": datetime.now(timezone.utc)}
            return f"Registered as family member. user_id: {uid}. Master: {u['email']}"
    uid = generate_user_id()
    _users[uid] = {"email": email, "name": name, "role": "master", "user_id": uid, "master_user_id": uid, "family_emails": [], "family_members": [], "created_at": datetime.now(timezone.utc)}
    _get_collections(AuthContext(uid, email, name, "master", uid))
    return f"Registered as master user. user_id: {uid}. Your wedding MCP server is ready."


# ---------------------------------------------------------------------------
# Auth management (master only)
# ---------------------------------------------------------------------------
@mcp.tool()
def add_family_user(email: str, name: str) -> str:
    """Add a family member by Gmail ID. Only master can do this."""
    ctx = require_master()
    master = _users.get(ctx.user_id)
    if not master:
        return "Master user record not found."
    if email in master.get("family_emails", []):
        return f"'{email}' is already a family member."
    member = FamilyMember(email=email, name=name)
    master.setdefault("family_emails", []).append(email)
    master.setdefault("family_members", []).append(member.to_dict())
    uid = generate_user_id()
    _users[uid] = {"email": email, "name": name, "role": "family", "user_id": uid, "master_user_id": ctx.user_id, "created_at": datetime.now(timezone.utc)}
    return f"Family member '{name}' ({email}) added."


@mcp.tool()
def remove_family_user(email: str) -> str:
    """Remove a family member. Only master can do this."""
    ctx = require_master()
    master = _users.get(ctx.user_id)
    if not master:
        return "Master user not found."
    if email not in master.get("family_emails", []):
        return f"'{email}' is not a family member."
    master["family_emails"].remove(email)
    master["family_members"] = [m for m in master.get("family_members", []) if m["email"] != email]
    for uid in [u for u, d in _users.items() if d.get("email") == email and d.get("role") == "family"]:
        del _users[uid]
    return f"Family member '{email}' removed."


@mcp.tool()
def get_family_users() -> list[dict]:
    """List all family members and their permissions. Master only."""
    ctx = require_master()
    master = _users.get(ctx.user_id)
    if not master:
        return [{"error": "Master user not found."}]
    return master.get("family_members", [])


@mcp.tool()
def update_family_permissions(email: str, permissions: dict) -> str:
    """Update a family member's permissions. Master only. permissions = {"can_add_guest": true, ...}"""
    ctx = require_master()
    master = _users.get(ctx.user_id)
    if not master:
        return "Master user not found."
    for m in master.get("family_members", []):
        if m["email"] == email:
            m["permissions"].update(permissions)
            return f"Permissions updated for '{email}'."
    return f"'{email}' is not a family member."


# ---------------------------------------------------------------------------
# Todo tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_todos() -> list[dict]:
    """Get the current todo list."""
    ctx = get_current_user()
    if ctx is None:
        return [{"error": "Not authenticated. Call register_user first."}]
    return _get_collections(ctx).get("todos", [])


@mcp.tool()
def add_todo(title: str) -> str:
    """Add a new todo item."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_add_todo"):
        return err
    _get_collections(ctx).setdefault("todos", []).append({"title": title, "completed": False})
    return f"Added todo: {title}"


@mcp.tool()
def toggle_todo(title: str) -> str:
    """Toggle a todo's completed status."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_toggle_todo"):
        return err
    for todo in _get_collections(ctx).get("todos", []):
        if todo["title"] == title:
            todo["completed"] = not todo.get("completed", False)
            s = "completed" if todo["completed"] else "incomplete"
            return f"Todo '{title}' marked as {s}."
    return f"Todo '{title}' not found."


@mcp.tool()
def delete_todo(title: str) -> str:
    """Delete a todo item from the list."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_toggle_todo"):
        return err
    todos = _get_collections(ctx).get("todos", [])
    for i, todo in enumerate(todos):
        if todo["title"] == title:
            todos.pop(i)
            return f"Todo '{title}' deleted."
    return f"Todo '{title}' not found."


# ---------------------------------------------------------------------------
# Guest tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_guests(collection: str) -> list[dict]:
    """Get the guest list. Pass 'engagement' or 'marriage'."""
    ctx = get_current_user()
    if ctx is None:
        return [{"error": "Not authenticated. Call register_user first."}]
    actual = _resolve(collection, ctx)
    if not actual:
        return [{"error": f"Invalid collection '{collection}'."}]
    return [g for g in _get_collections(ctx).get(actual, []) if not g.get("deleted", False)]


@mcp.tool()
def add_guest(collection: str, name: str, family_members: list[dict] = []) -> str:
    """Add a guest. Pass 'engagement' or 'marriage'."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_add_guest"):
        return err
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    guests = _get_collections(ctx).setdefault(actual, [])
    for g in guests:
        if g["name"] == name and not g.get("deleted", False):
            return f"Guest '{name}' already exists."
        if g["name"] == name and g.get("deleted", False):
            g["deleted"] = False
            g["isInvited"] = False
            if family_members:
                ef = g.get("family_members", [])
                en = {m["name"] for m in ef}
                for m in family_members:
                    if m["name"] not in en:
                        ef.append({"name": m["name"], "relation": m.get("relation", "")})
                g["family_members"] = ef
            return f"Guest '{name}' restored."
    guests.append({"name": name, "isInvited": False, "deleted": False, "family_members": [{"name": m["name"], "relation": m.get("relation", "")} for m in family_members] if family_members else [], "gifts_received": []})
    return f"Added guest '{name}' to {collection}."


@mcp.tool()
def remove_guest(collection: str, name: str) -> str:
    """Soft delete a guest."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_remove_guest"):
        return err
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    for g in _get_collections(ctx).get(actual, []):
        if g["name"] == name and not g.get("deleted", False):
            g["deleted"] = True
            return f"Guest '{name}' removed."
    return f"Guest '{name}' not found."


@mcp.tool()
def toggle_invited(collection: str, name: str) -> str:
    """Toggle a guest's invited status."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_toggle_invited"):
        return err
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    for g in _get_collections(ctx).get(actual, []):
        if g["name"] == name and not g.get("deleted", False):
            g["isInvited"] = not g.get("isInvited", False)
            s = "invited" if g["isInvited"] else "not invited"
            return f"Guest '{name}' marked as {s}."
    return f"Guest '{name}' not found."


@mcp.tool()
def add_family_members(collection: str, guest_name: str, members: list[dict]) -> str:
    """Add family members to a guest entry."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_add_family_members"):
        return err
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    for g in _get_collections(ctx).get(actual, []):
        if g["name"] == guest_name and not g.get("deleted", False):
            ef = g.get("family_members", [])
            en = {m["name"] for m in ef}
            added, skipped = [], []
            for m in members:
                if m["name"] in en:
                    skipped.append(m["name"])
                else:
                    ef.append({"name": m["name"], "relation": m.get("relation", "")})
                    en.add(m["name"])
                    added.append(m["name"])
            g["family_members"] = ef
            parts = []
            if added:
                parts.append(f"Added: {', '.join(added)}")
            if skipped:
                parts.append(f"Skipped: {', '.join(skipped)}")
            return f"Updated family for '{guest_name}'. {'; '.join(parts)}."
    return f"Guest '{guest_name}' not found."


@mcp.tool()
def get_family_members(collection: str, guest_name: str) -> list[dict]:
    """Get family members of a guest."""
    ctx = get_current_user()
    if ctx is None:
        return [{"error": "Not authenticated."}]
    actual = _resolve(collection, ctx)
    if not actual:
        return [{"error": f"Invalid collection '{collection}'."}]
    for g in _get_collections(ctx).get(actual, []):
        if g["name"] == guest_name and not g.get("deleted", False):
            return g.get("family_members", [])
    return [{"error": f"Guest '{guest_name}' not found."}]


@mcp.tool()
def record_gift(collection: str, guest_name: str, amount: float, from_guest: str, occasion: str, date: str, note: str = "") -> str:
    """Record a shagun/gift."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated."
    if err := _check_perm(ctx, "can_record_gift"):
        return err
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    for g in _get_collections(ctx).get(actual, []):
        if g["name"] == guest_name and not g.get("deleted", False):
            g.setdefault("gifts_received", []).append({"amount": amount, "from_guest": from_guest, "occasion": occasion, "date": date, "note": note})
            return f"Recorded Rs.{amount:.0f} from '{from_guest}' for '{guest_name}'."
    return f"Guest '{guest_name}' not found."


@mcp.tool()
def get_gifts(collection: str, guest_name: str = "") -> list[dict]:
    """Get gifts/shagun records."""
    ctx = get_current_user()
    if ctx is None:
        return [{"error": "Not authenticated."}]
    actual = _resolve(collection, ctx)
    if not actual:
        return [{"error": f"Invalid collection '{collection}'."}]
    if guest_name:
        for g in _get_collections(ctx).get(actual, []):
            if g["name"] == guest_name and not g.get("deleted", False):
                gifts = g.get("gifts_received", [])
                for gift in gifts:
                    gift["guest_name"] = guest_name
                return gifts
        return [{"error": f"Guest '{guest_name}' not found."}]
    all_gifts = []
    for g in _get_collections(ctx).get(actual, []):
        if not g.get("deleted", False):
            for gift in g.get("gifts_received", []):
                gift["guest_name"] = g["name"]
                all_gifts.append(gift)
    return all_gifts


# ---------------------------------------------------------------------------
# Dynamic collection tools
# ---------------------------------------------------------------------------
@mcp.tool()
def create_collection(name: str, description: str = "", fields: dict = {}) -> str:
    """Create a new collection. A description is required for semantic discovery.

    If you provide only a collection name without a description, the server will ask for clarification about what the collection stores.
    """
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_create_collection"):
        return err
    clean = name.strip()
    if not clean:
        return "Collection name cannot be empty."

    desc_clean = description.strip() if description else ""
    if not desc_clean:
        import json
        return json.dumps({
            "status": "NEEDS_CLARIFICATION",
            "collectionName": clean,
            "message": "A description is required so this collection can be discovered later.",
            "question": f"What kind of information will you store in the '{clean}' collection?",
        })

    colls = _get_collections(ctx)
    if clean in set(VALID_GUEST_COLLECTIONS.values()) | set(colls.keys()):
        return f"Collection '{clean}' already exists."

    colls[clean] = []
    _user_schemas.setdefault(ctx.master_user_id, {})[clean] = fields

    metadata = CollectionMetadata.generate_from_description(ctx.master_user_id, clean, desc_clean)
    _save_collection_metadata(metadata)

    field_list = ", ".join(fields.keys()) if fields else "none"
    return f"Collection '{clean}' created with fields: {field_list}."


@mcp.tool()
def discover_collections(query: str) -> str:
    """Find which user collections contain data relevant to a query. ALWAYS call this first before answering any data question.

    For broad questions like 'total wedding expenses' or 'show me all my data', call this MULTIPLE TIMES with different keywords (e.g. 'expense', 'roka', 'vendor', 'cost', 'payment') to find ALL relevant collections. Then query each one and combine results.

    Returns a list of matching collections with names and descriptions."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."

    # 1. Search metadata
    results = _search_collection_metadata(ctx.master_user_id, query)

    # 2. Also list ALL user collections and do fuzzy name matching
    all_collections = _get_collections(ctx).keys()
    query_lower = query.lower()
    query_words = set(query_lower.split())

    seen = {r["collection_name"] for r in results}
    for coll_name in sorted(all_collections):
        if coll_name in seen:
            continue
        name_lower = coll_name.lower()
        score = 0
        for word in query_words:
            if word in name_lower:
                score += 3
        if query_lower in name_lower:
            score += 5
        for part in name_lower.replace("-", "_").split("_"):
            for word in query_words:
                if part.startswith(word) or word.startswith(part):
                    score += 2
        if score > 0:
            results.append({
                "collection_name": coll_name,
                "description": f"(no description) Collection: {coll_name}",
                "score": score,
                "metadata_status": "no_metadata",
            })
            seen.add(coll_name)

    # 3. Also include fixed/virtual collections that match
    fixed = {
        "guests_engagement": "Engagement guest list",
        "guests_marriage": "Marriage guest list",
        "roka": "Roka ceremony records",
        "todo": "Todo items",
        "family": "Family members",
        "shagun": "Shagun/gift records",
    }
    for coll_name, desc in fixed.items():
        if coll_name in seen:
            continue
        name_lower = coll_name.lower()
        score = 0
        for word in query_words:
            if word in name_lower or word in desc.lower():
                score += 2
        if query_lower in name_lower or query_lower in desc.lower():
            score += 4
        if score > 0:
            results.append({
                "collection_name": coll_name,
                "description": desc,
                "score": score,
                "metadata_status": "fixed",
            })
            seen.add(coll_name)

    results.sort(key=lambda x: x.get("score", 0), reverse=True)

    import json
    if not results:
        return json.dumps({
            "status": "NOT_FOUND",
            "query": query,
            "message": "No relevant collections found for this query.",
            "collections": [],
        })

    return json.dumps({
        "status": "FOUND",
        "query": query,
        "collections": [
            {
                "collectionName": r["collection_name"],
                "description": r["description"],
                "reason": f"Contains data related to: {r['description']}",
            }
            for r in results
        ],
    })


@mcp.tool()
def add_record(collection: str, data: dict) -> str:
    """Add a record to any collection."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated."
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    schema = _get_schema(collection, ctx)
    record = {f: data.get(f, d) for f, d in schema.items()} if schema else dict(data)
    if schema:
        for k, v in data.items():
            if k not in record:
                record[k] = v
    record.setdefault("deleted", False)
    record["_id"] = str(len(_get_collections(ctx).get(actual, [])) + 1)
    _get_collections(ctx).setdefault(actual, []).append(record)
    return f"Record added (id: {record['_id']})."


@mcp.tool()
def get_records(collection: str, filters: dict = {}) -> list[dict]:
    """Get records from a specific collection. This tool requires an actual collection name, NOT a natural-language concept.

    IMPORTANT: If the user asks a broad semantic question like 'Give me my wedding expenses', do NOT guess a collection name. Instead:
    1. First call discover_collections('wedding expenses') to find relevant collections
    2. Then call get_records on each discovered collection
    3. Combine the results

    Use this tool when you already know the exact collection name (e.g., 'expense', 'roka')."""
    ctx = get_current_user()
    if ctx is None:
        return [{"error": "Not authenticated."}]
    actual = _resolve(collection, ctx)
    if not actual:
        return [{"error": f"Invalid collection '{collection}'."}]
    return [r for r in _get_collections(ctx).get(actual, []) if not r.get("deleted", False) and all(r.get(k) == v for k, v in filters.items())]


@mcp.tool()
def update_record(collection: str, record_id: str, updates: dict) -> str:
    """Update fields on a record."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated."
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    for rec in _get_collections(ctx).get(actual, []):
        if rec.get("_id") == record_id:
            rec.update(updates)
            return f"Record '{record_id}' updated."
    return f"Record '{record_id}' not found."


@mcp.tool()
def delete_record(collection: str, record_id: str) -> str:
    """Soft delete a record."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_delete_record"):
        return err
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    for rec in _get_collections(ctx).get(actual, []):
        if rec.get("_id") == record_id:
            rec["deleted"] = True
            return f"Record '{record_id}' deleted."
    return f"Record '{record_id}' not found."


# ---------------------------------------------------------------------------
# Unified collection manager
# ---------------------------------------------------------------------------
def _handle_get_records(ctx: AuthContext, collection: str, **kwargs) -> list[dict]:
    """Retrieve records from a collection with optional filters."""
    actual = _resolve(collection, ctx)
    if not actual:
        return [{"error": f"Invalid collection '{collection}'."}]
    filters = kwargs.get("filters", {})
    return [
        r for r in _get_collections(ctx).get(actual, [])
        if not r.get("deleted", False) and all(r.get(k) == v for k, v in filters.items())
    ]


def _handle_update_record(ctx: AuthContext, collection: str, **kwargs) -> str:
    """Update a specific record in a collection."""
    actual = _resolve(collection, ctx)
    if not actual:
        return f"Invalid collection '{collection}'."
    record_id = kwargs.get("record_id", "")
    updates = kwargs.get("updates", {})
    if not record_id:
        return "Error: 'record_id' is required for update operation."
    if not updates:
        return "Error: 'updates' dict is required for update operation."
    for rec in _get_collections(ctx).get(actual, []):
        if rec.get("_id") == record_id:
            rec.update(updates)
            return f"Record '{record_id}' updated."
    return f"Record '{record_id}' not found."


_COLLECTION_OPERATIONS: dict[str, callable] = {
    "get": _handle_get_records,
    "update": _handle_update_record,
}


@mcp.tool()
def manage_collection(
    collection: str,
    operation: str,
    filters: dict = {},
    record_id: str = "",
    updates: dict = {},
) -> list[dict] | str:
    """Manage records in a user-specific collection. This tool requires an actual collection name, NOT a natural-language concept.

    IMPORTANT: If the user asks a broad semantic question like 'Give me my wedding expenses', do NOT guess a collection name. Instead:
    1. First call discover_collections('wedding expenses') to find relevant collections
    2. Then call manage_collection or get_records on each discovered collection
    3. Combine the results

    Supported operations:
      - get:     Retrieve records. Use 'filters' to narrow results (e.g. {"status": "active"}).
      - update:  Update a record. Provide 'record_id' and 'updates' dict (e.g. {"status": "done"}).

    Collections are user-scoped — you can only access your own collections.
    """
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."

    handler = _COLLECTION_OPERATIONS.get(operation)
    if not handler:
        valid = ", ".join(_COLLECTION_OPERATIONS.keys())
        return f"Invalid operation '{operation}'. Valid operations: {valid}"

    return handler(ctx, collection, filters=filters, record_id=record_id, updates=updates)


if __name__ == "__main__":
    import sys
    if "--sse" in sys.argv:
        import uvicorn
        app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        import asyncio
        asyncio.run(mcp.run_stdio_async())
