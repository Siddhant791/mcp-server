from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.mcpserver.server import MCPServer

from auth.middleware import get_current_user, require_master
from auth.models import AuthContext, FamilyMember, FAMILY_DEFAULT_PERMISSIONS, generate_user_id

mcp = MCPServer(name="wedding-mcp-server")

_users: dict[str, dict] = {}
_user_collections: dict[str, dict[str, list[dict]]] = {}
_user_schemas: dict[str, dict[str, dict]] = {}

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
def create_collection(name: str, fields: dict = {}) -> str:
    """Create a new collection."""
    ctx = get_current_user()
    if ctx is None:
        return "Not authenticated. Call register_user first."
    if err := _check_perm(ctx, "can_create_collection"):
        return err
    clean = name.strip()
    if not clean:
        return "Collection name cannot be empty."
    colls = _get_collections(ctx)
    if clean in set(VALID_GUEST_COLLECTIONS.values()) | set(colls.keys()):
        return f"Collection '{clean}' already exists."
    colls[clean] = []
    _user_schemas.setdefault(ctx.master_user_id, {})[clean] = fields
    field_list = ", ".join(fields.keys()) if fields else "none"
    return f"Collection '{clean}' created with fields: {field_list}."


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
    """Get records with optional filters."""
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


if __name__ == "__main__":
    import sys
    if "--sse" in sys.argv:
        import uvicorn
        app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        import asyncio
        asyncio.run(mcp.run_stdio_async())
