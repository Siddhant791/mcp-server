import os
import secrets
from datetime import datetime, timezone
from urllib.parse import quote_plus

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from mcp.server.mcpserver.server import MCPServer

from auth.middleware import AuthMiddleware, get_current_user, require_auth, require_master
from auth.models import AuthContext, FamilyMember, generate_user_id
from auth.oauth import (
    GOOGLE_REDIRECT_URI,
    create_jwt,
    exchange_code_for_token,
    get_google_auth_url,
    get_user_info_from_google,
    verify_token,
)

# ---------------------------------------------------------------------------
# MongoDB connection
# ---------------------------------------------------------------------------
MONGODB_URI = os.environ.get("MONGODB_URI")
DATABASE_NAME = "mcp_server"

client = None
db = None
users_collection = None

if MONGODB_URI:
    from urllib.parse import quote_plus as _qp

    def _encode(uri: str) -> str:
        if not uri:
            return uri
        if "://" in uri:
            proto_end = uri.index("://") + 3
            proto = uri[:proto_end]
            rest = uri[proto_end:]
            at = rest.rfind("@")
            if at != -1:
                userinfo, host = rest[:at], rest[at:]
                colon = userinfo.find(":")
                if colon != -1:
                    u, p = userinfo[:colon], userinfo[colon + 1 :]
                    return f"{proto}{_qp(u)}:{_qp(p)}{host}"
        return uri

    client = AsyncIOMotorClient(_encode(MONGODB_URI))
    db = client[DATABASE_NAME]
    users_collection = db["users"]

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = MCPServer(name="wedding-mcp-server")

# Valid "friendly" collection name → actual MongoDB suffix
VALID_GUEST_COLLECTIONS = {
    "engagement": "Guest_list_engagement",
    "marriage": "Guest_list_marriage",
}

# Per-request dynamic schemas (populated lazily per user)
_user_dynamic_collections: dict[str, set[str]] = {}
_user_schemas: dict[str, dict] = {}

# Default collection schemas created for every new master user
DEFAULT_SCHEMAS = {
    "Guest_list_engagement": {},
    "Guest_list_marriage": {},
}


# ---------------------------------------------------------------------------
# Helpers – user-scoped collection resolution
# ---------------------------------------------------------------------------
async def _get_auth_ctx() -> AuthContext:
    ctx = get_current_user()
    if ctx is None:
        raise PermissionError("Authentication required. Please authenticate via OAuth first.")
    return ctx


def _prefixed(collection_suffix: str, ctx: AuthContext) -> str:
    return f"{ctx.collection_prefix}{collection_suffix}"


async def _ensure_user_collections(ctx: AuthContext) -> None:
    """Create default collections for a brand-new master user."""
    if db is None:
        return
    for suffix in DEFAULT_SCHEMAS:
        coll_name = _prefixed(suffix, ctx)
        coll = db[coll_name]
        count = await coll.estimated_document_count()
        if count == 0:
            pass  # collection is empty – that's fine


async def _load_user_schemas(ctx: AuthContext) -> None:
    key = ctx.master_user_id
    if key in _user_dynamic_collections:
        return
    if db is None:
        return
    schemas_coll = db[f"{key}__collection_schemas"]
    dynamic: set[str] = set()
    schemas: dict = {}
    async for doc in schemas_coll.find({}):
        name = doc["name"]
        dynamic.add(name)
        schemas[name] = doc.get("fields", {})
    _user_dynamic_collections[key] = dynamic
    _user_schemas[key] = schemas


async def _resolve(collection: str, ctx: AuthContext) -> str | None:
    """Resolve a friendly collection name to a fully-prefixed MongoDB name."""
    if collection in VALID_GUEST_COLLECTIONS:
        return _prefixed(VALID_GUEST_COLLECTIONS[collection], ctx)
    await _load_user_schemas(ctx)
    dyn = _user_dynamic_collections.get(ctx.master_user_id, set())
    if collection in dyn:
        return _prefixed(collection, ctx)
    # Also allow passing the full prefixed name directly
    if collection.startswith(ctx.collection_prefix):
        return collection
    return None


async def _get_schema(collection: str, ctx: AuthContext) -> dict | None:
    await _load_user_schemas(ctx)
    return _user_schemas.get(ctx.master_user_id, {}).get(collection)


# ---------------------------------------------------------------------------
# OAuth / auth endpoints  (Starlette routes, mounted alongside MCP app)
# ---------------------------------------------------------------------------
_server_auth_codes: dict[str, dict] = {}  # code → {jwt_token, created}
_google_states: dict[str, dict] = {}     # google_state → {chatgpt_redirect_uri, created}
_registered_clients: dict[str, dict] = {}  # client_id → {client_name, client_secret, ...}


async def _well_known(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["openid", "email", "profile"],
    })


async def _protected_resource(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource": str(request.base_url),
        "authorization_servers": [str(request.base_url).rstrip("/")],
    })


async def _authorize(request: Request) -> RedirectResponse:
    chatgpt_redirect = request.query_params.get("redirect_uri", "")
    google_state = secrets.token_urlsafe(32)
    _google_states[google_state] = {
        "chatgpt_redirect_uri": chatgpt_redirect,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    server_callback = str(request.base_url).rstrip("/") + "/auth/callback"
    url = get_google_auth_url(google_state, redirect_uri=server_callback)
    return RedirectResponse(url)


async def _register_user(email: str, name: str) -> tuple[str, str, str]:
    users = users_collection
    existing = await users.find_one({"email": email})

    if existing:
        user_id = existing["user_id"]
        role = existing["role"]
        master_user_id = existing.get("master_user_id", user_id)
        await users.update_one(
            {"_id": existing["_id"]},
            {"$set": {"last_login": datetime.now(timezone.utc)}},
        )
        return user_id, role, master_user_id

    master_doc = await users.find_one({"role": "master", "family_emails": email})
    if master_doc:
        user_id = generate_user_id()
        role = "family"
        master_user_id = master_doc["user_id"]
        await users.insert_one({
            "email": email, "name": name, "role": "family",
            "user_id": user_id, "master_user_id": master_user_id,
            "created_at": datetime.now(timezone.utc),
            "last_login": datetime.now(timezone.utc),
        })
        return user_id, role, master_user_id

    user_id = generate_user_id()
    role = "master"
    master_user_id = user_id
    await users.insert_one({
        "email": email, "name": name, "role": "master",
        "user_id": user_id, "master_user_id": user_id,
        "family_emails": [], "family_members": [],
        "created_at": datetime.now(timezone.utc),
        "last_login": datetime.now(timezone.utc),
    })
    for suffix in DEFAULT_SCHEMAS:
        coll_name = _prefixed(suffix, AuthContext(user_id, email, name, role, master_user_id))
        coll = db[coll_name]
        await coll.insert_one({"_init": True})
        await coll.delete_one({"_init": True})
    schemas_coll = db[f"{user_id}__collection_schemas"]
    for suffix, fields in DEFAULT_SCHEMAS.items():
        await schemas_coll.insert_one({"name": suffix, "fields": fields})
    return user_id, role, master_user_id


async def _callback(request: Request):
    from starlette.responses import HTMLResponse

    code = request.query_params.get("code")
    google_state = request.query_params.get("state")

    if not code or not google_state:
        return HTMLResponse("<html><body>Missing code or state</body></html>", status_code=400)

    stored_state = _google_states.pop(google_state, None)
    if not stored_state:
        return HTMLResponse("<html><body>Invalid or expired state</body></html>", status_code=400)

    chatgpt_redirect = stored_state.get("chatgpt_redirect_uri", "")

    try:
        server_callback = str(request.base_url).rstrip("/") + "/auth/callback"
        token_data = await exchange_code_for_token(code, redirect_uri=server_callback)
        access_token = token_data["access_token"]
        user_info = await get_user_info_from_google(access_token)
    except Exception as e:
        return HTMLResponse(f"<html><body>Google OAuth failed: {e}</body></html>", status_code=500)

    email = user_info.get("email", "")
    name = user_info.get("name", email)
    if not email:
        return HTMLResponse("<html><body>Could not determine email</body></html>", status_code=400)

    if db is None:
        return HTMLResponse("<html><body>MongoDB not configured</body></html>", status_code=500)

    user_id, role, master_user_id = await _register_user(email, name)
    jwt_token = create_jwt(user_id, email, name, role, master_user_id)

    server_code = secrets.token_urlsafe(32)
    _server_auth_codes[server_code] = {
        "jwt_token": jwt_token,
        "created": datetime.now(timezone.utc).isoformat(),
    }

    if chatgpt_redirect:
        from urllib.parse import urlencode as _urlencode
        sep = "&" if "?" in chatgpt_redirect else "?"
        return RedirectResponse(f"{chatgpt_redirect}{sep}{_urlencode({'code': server_code, 'state': google_state})}")

    return HTMLResponse("""<!DOCTYPE html>
<html><head><title>Auth Complete</title></head>
<body><p>Authentication successful. You can close this window.</p></body></html>""")


async def _register(request: Request) -> JSONResponse:
    body = await request.json()
    client_name = body.get("client_name", "mcp-client")
    client_id = "mcp_client_" + secrets.token_hex(8)
    client_secret = secrets.token_hex(24)
    _registered_clients[client_id] = {
        "client_name": client_name,
        "client_secret": client_secret,
        "redirect_uris": body.get("redirect_uris", []),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return JSONResponse({
        "client_id": client_id,
        "client_name": client_name,
        "client_secret": client_secret,
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })


async def _token(request: Request) -> JSONResponse:
    from starlette.responses import HTMLResponse as _HTML
    body = await request.json()
    grant_type = body.get("grant_type")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = body.get("code", "")
    auth_info = _server_auth_codes.pop(code, None)

    if auth_info:
        return JSONResponse({
            "access_token": auth_info["jwt_token"],
            "token_type": "bearer",
            "expires_in": 86400,
        })

    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret", "")

    if client_id and client_id in _registered_clients:
        expected_secret = _registered_clients[client_id].get("client_secret", "")
        if client_secret and client_secret != expected_secret:
            return JSONResponse({"error": "invalid_client"}, status_code=401)

    try:
        server_callback = str(request.base_url).rstrip("/") + "/auth/callback"
        token_data = await exchange_code_for_token(code, client_id=client_id or GOOGLE_CLIENT_ID, client_secret=client_secret or GOOGLE_CLIENT_SECRET, redirect_uri=server_callback)
        access_token = token_data.get("access_token")
        id_token = token_data.get("id_token")

        if id_token:
            google_payload = verify_token(id_token)
            if google_payload:
                email = google_payload.get("email", "")
                name = google_payload.get("name", email)
            else:
                user_info = await get_user_info_from_google(access_token)
                email = user_info.get("email", "")
                name = user_info.get("name", email)
        else:
            user_info = await get_user_info_from_google(access_token)
            email = user_info.get("email", "")
            name = user_info.get("name", email)

        if not email:
            return JSONResponse({"error": "Could not determine email"}, status_code=400)
        if db is None:
            return JSONResponse({"error": "MongoDB not configured"}, status_code=500)

        user_id, role, master_user_id = await _register_user(email, name)
        jwt_token = create_jwt(user_id, email, name, role, master_user_id)
        return JSONResponse({
            "access_token": jwt_token,
            "token_type": "bearer",
            "expires_in": 86400,
        })

    except Exception as e:
        return JSONResponse({"error": f"Token exchange failed: {e}"}, status_code=400)




# ---------------------------------------------------------------------------
# Auth management tools (master only)
# ---------------------------------------------------------------------------
@mcp.tool()
async def add_family_user(email: str, name: str) -> str:
    """Add a family member by Gmail ID. Only the master user can do this. The family member will be able to access the same wedding data with default permissions (read/write guests, todos, gifts; cannot create collections or manage users)."""
    ctx = require_master()
    if db is None:
        return "Error: MongoDB not configured."
    users = users_collection
    master_doc = await users.find_one({"user_id": ctx.user_id})
    if not master_doc:
        return "Master user record not found."
    family_emails = master_doc.get("family_emails", [])
    if email in family_emails:
        return f"'{email}' is already a family member."
    family_members = master_doc.get("family_members", [])
    member = FamilyMember(email=email, name=name)
    family_members.append(member.to_dict())
    family_emails.append(email)
    await users.update_one(
        {"_id": master_doc["_id"]},
        {"$set": {"family_emails": family_emails, "family_members": family_members}},
    )
    return f"Family member '{name}' ({email}) added. They can now authenticate with this MCP server using their Google account."


@mcp.tool()
async def remove_family_user(email: str) -> str:
    """Remove a family member's access. Only the master user can do this."""
    ctx = require_master()
    if db is None:
        return "Error: MongoDB not configured."
    users = users_collection
    master_doc = await users.find_one({"user_id": ctx.user_id})
    if not master_doc:
        return "Master user record not found."
    family_emails = master_doc.get("family_emails", [])
    family_members = master_doc.get("family_members", [])
    if email not in family_emails:
        return f"'{email}' is not a family member."
    family_emails.remove(email)
    family_members = [m for m in family_members if m["email"] != email]
    await users.update_one(
        {"_id": master_doc["_id"]},
        {"$set": {"family_emails": family_emails, "family_members": family_members}},
    )
    # Also remove the family user's session record
    await users.delete_one({"email": email, "role": "family"})
    return f"Family member '{email}' removed."


@mcp.tool()
async def get_family_users() -> list[dict]:
    """List all family members and their permissions. Only the master user can do this."""
    ctx = require_master()
    if db is None:
        return [{"error": "MongoDB not configured."}]
    master_doc = await users_collection.find_one({"user_id": ctx.user_id})
    if not master_doc:
        return [{"error": "Master user record not found."}]
    return master_doc.get("family_members", [])


@mcp.tool()
async def update_family_permissions(email: str, permissions: dict) -> str:
    """Update a family member's permissions. Only the master user can do this. permissions is a dict like {"can_add_guest": true, "can_remove_guest": false}."""
    ctx = require_master()
    if db is None:
        return "Error: MongoDB not configured."
    users = users_collection
    master_doc = await users.find_one({"user_id": ctx.user_id})
    if not master_doc:
        return "Master user record not found."
    family_members = master_doc.get("family_members", [])
    for m in family_members:
        if m["email"] == email:
            m["permissions"].update(permissions)
            await users.update_one(
                {"_id": master_doc["_id"]},
                {"$set": {"family_members": family_members}},
            )
            return f"Permissions updated for '{email}'."
    return f"'{email}' is not a family member."


# ---------------------------------------------------------------------------
# Todo tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_todos() -> list[dict]:
    """Get the current todo list."""
    ctx = await _get_auth_ctx()
    if db is None:
        return [{"error": "MongoDB not configured."}]
    coll = db[_prefixed("todos", ctx)]
    cursor = coll.find({})
    todos = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        todos.append(doc)
    return todos


@mcp.tool()
async def add_todo(title: str) -> str:
    """Add a new todo item to the list."""
    ctx = await _get_auth_ctx()
    if ctx.role == "family":
        master_doc = await users_collection.find_one({"user_id": ctx.master_user_id})
        member = next((m for m in master_doc.get("family_members", []) if m["email"] == ctx.email), None)
        if member and not member.get("permissions", {}).get("can_add_todo", True):
            return "Permission denied: can_add_todo is disabled for your account."
    if db is None:
        return "Error: MongoDB not configured."
    coll = db[_prefixed("todos", ctx)]
    todo = {"title": title, "completed": False}
    result = await coll.insert_one(todo)
    return f"Added todo: {title} (id: {result.inserted_id})"


@mcp.tool()
async def toggle_todo(title: str) -> str:
    """Toggle a todo item's completed status. Use this when the user wants to mark a todo as done/completed/finished, or mark a completed todo as not done/incomplete/pending."""
    ctx = await _get_auth_ctx()
    if ctx.role == "family":
        master_doc = await users_collection.find_one({"user_id": ctx.master_user_id})
        member = next((m for m in master_doc.get("family_members", []) if m["email"] == ctx.email), None)
        if member and not member.get("permissions", {}).get("can_toggle_todo", True):
            return "Permission denied: can_toggle_todo is disabled for your account."
    if db is None:
        return "Error: MongoDB not configured."
    coll = db[_prefixed("todos", ctx)]
    doc = await coll.find_one({"title": title})
    if not doc:
        available = await coll.distinct("title")
        if available:
            return f"Todo '{title}' not found. Available: {', '.join(available)}"
        return f"Todo '{title}' not found. The todo list is empty."
    new_status = not doc.get("completed", False)
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"completed": new_status}})
    status_text = "completed" if new_status else "incomplete"
    return f"Todo '{title}' marked as {status_text}."


# ---------------------------------------------------------------------------
# Guest tools
# ---------------------------------------------------------------------------
async def _check_family_perm(ctx: AuthContext, perm: str) -> str | None:
    """Returns error message if permission denied, else None."""
    if ctx.role == "master":
        return None
    if ctx.user_id == "guest":
        return f"Permission denied: {perm} requires authentication."
    master_doc = await users_collection.find_one({"user_id": ctx.master_user_id})
    if not master_doc:
        return f"Permission denied: {perm} requires authentication."
    member = next((m for m in master_doc.get("family_members", []) if m["email"] == ctx.email), None)
    if member and not member.get("permissions", {}).get(perm, True):
        return f"Permission denied: {perm} is disabled for your account."
    return None


@mcp.tool()
async def get_guests(collection: str) -> list[dict]:
    """Get the guest list for a wedding event. Pass 'engagement' or 'marriage', or any dynamically created collection name."""
    ctx = await _get_auth_ctx()
    if db is None:
        return [{"error": "MongoDB not configured."}]
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
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
    """Add a new guest to the list. Pass 'engagement' or 'marriage'. Optionally include family_members as [{"name": "Priya", "relation": "wife"}]."""
    ctx = await _get_auth_ctx()
    if perm_err := await _check_family_perm(ctx, "can_add_guest"):
        return perm_err
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
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
            ef = deleted_doc.get("family_members", [])
            en = {m["name"] for m in ef}
            for m in family_members:
                if m["name"] not in en:
                    ef.append({"name": m["name"], "relation": m.get("relation", "")})
            update["family_members"] = ef
        await coll.update_one({"_id": deleted_doc["_id"]}, {"$set": update})
        return f"Guest '{name}' restored in {collection} list (was previously removed)."
    guest: dict = {
        "name": name,
        "isInvited": False,
        "deleted": False,
        "family_members": [{"name": m["name"], "relation": m.get("relation", "")} for m in family_members] if family_members else [],
        "gifts_received": [],
    }
    result = await coll.insert_one(guest)
    return f"Added guest '{name}' to {collection} list (id: {result.inserted_id})."


@mcp.tool()
async def remove_guest(collection: str, name: str) -> str:
    """Soft delete a guest from the list. Pass 'engagement' or 'marriage'."""
    ctx = await _get_auth_ctx()
    if perm_err := await _check_family_perm(ctx, "can_remove_guest"):
        return perm_err
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
        if active:
            return f"Guest '{name}' not found. Available: {', '.join(active)}"
        return f"Guest '{name}' not found. The {collection} guest list is empty."
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"deleted": True}})
    return f"Guest '{name}' removed from {collection} list."


@mcp.tool()
async def toggle_invited(collection: str, name: str) -> str:
    """Toggle a guest's invited status."""
    ctx = await _get_auth_ctx()
    if perm_err := await _check_family_perm(ctx, "can_toggle_invited"):
        return perm_err
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
        if active:
            return f"Guest '{name}' not found. Available: {', '.join(active)}"
        return f"Guest '{name}' not found. The {collection} guest list is empty."
    new_status = not doc.get("isInvited", False)
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"isInvited": new_status}})
    status_text = "invited" if new_status else "not invited"
    return f"Guest '{name}' marked as {status_text} in {collection} list."


@mcp.tool()
async def add_family_members(collection: str, guest_name: str, members: list[dict]) -> str:
    """Add family members to a guest entry. members = [{"name": "Priya", "relation": "wife"}]."""
    ctx = await _get_auth_ctx()
    if perm_err := await _check_family_perm(ctx, "can_add_family_members"):
        return perm_err
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
        if active:
            return f"Guest '{guest_name}' not found. Available: {', '.join(active)}"
        return f"Guest '{guest_name}' not found."
    ef = doc.get("family_members", [])
    en = {m["name"] for m in ef}
    added, skipped = [], []
    for m in members:
        if m["name"] in en:
            skipped.append(m["name"])
        else:
            ef.append({"name": m["name"], "relation": m.get("relation", "")})
            en.add(m["name"])
            added.append(m["name"])
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"family_members": ef}})
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if skipped:
        parts.append(f"Skipped (already exist): {', '.join(skipped)}")
    return f"Updated family for '{guest_name}'. {'; '.join(parts)}."


@mcp.tool()
async def get_family_members(collection: str, guest_name: str) -> list[dict]:
    """Get family members of a specific guest."""
    ctx = await _get_auth_ctx()
    if db is None:
        return [{"error": "MongoDB not configured."}]
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    coll = db[coll_name]
    doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
    if not doc:
        return [{"error": f"Guest '{guest_name}' not found."}]
    return doc.get("family_members", [])


@mcp.tool()
async def record_gift(collection: str, guest_name: str, amount: float, from_guest: str, occasion: str, date: str, note: str = "") -> str:
    """Record a shagun/gift. amount in rupees, occasion e.g. 'marriage', date format YYYY-MM-DD."""
    ctx = await _get_auth_ctx()
    if perm_err := await _check_family_perm(ctx, "can_record_gift"):
        return perm_err
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    coll = db[coll_name]
    doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
    if not doc:
        active = await coll.distinct("name", {"deleted": {"$ne": True}})
        if active:
            return f"Guest '{guest_name}' not found. Available: {', '.join(active)}"
        return f"Guest '{guest_name}' not found."
    gift = {"amount": amount, "from_guest": from_guest, "occasion": occasion, "date": date, "note": note}
    await coll.update_one({"_id": doc["_id"]}, {"$push": {"gifts_received": gift}})
    return f"Recorded Rs.{amount:.0f} shagun from '{from_guest}' for '{guest_name}' on {date} ({occasion})."


@mcp.tool()
async def get_gifts(collection: str, guest_name: str = "") -> list[dict]:
    """Get gifts/shagun records. Optionally filter by guest_name."""
    ctx = await _get_auth_ctx()
    if db is None:
        return [{"error": "MongoDB not configured."}]
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    coll = db[coll_name]
    if guest_name:
        doc = await coll.find_one({"name": guest_name, "deleted": {"$ne": True}})
        if not doc:
            return [{"error": f"Guest '{guest_name}' not found."}]
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


# ---------------------------------------------------------------------------
# Dynamic collection tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def create_collection(name: str, fields: dict = {}) -> str:
    """Create a new collection with a defined schema. Only master can do this. After creation use add_record, get_records, update_record, delete_record."""
    ctx = require_master()
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    clean = name.strip()
    if not clean:
        return "Collection name cannot be empty."
    all_known = set(VALID_GUEST_COLLECTIONS.values()) | _user_dynamic_collections.get(ctx.master_user_id, set())
    if clean in all_known:
        return f"Collection '{clean}' already exists."
    full_name = _prefixed(clean, ctx)
    coll = db[full_name]
    await coll.insert_one({"_init": True})
    await coll.delete_one({"_init": True})
    schemas_coll = db[f"{ctx.master_user_id}__collection_schemas"]
    await schemas_coll.insert_one({"name": clean, "fields": fields})
    _user_dynamic_collections.setdefault(ctx.master_user_id, set()).add(clean)
    _user_schemas.setdefault(ctx.master_user_id, {})[clean] = fields
    field_list = ", ".join(fields.keys()) if fields else "none"
    return f"Collection '{clean}' created with fields: {field_list}."


@mcp.tool()
async def add_record(collection: str, data: dict) -> str:
    """Add a new record to any collection."""
    ctx = await _get_auth_ctx()
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    schema = await _get_schema(collection, ctx)
    if schema:
        record = {f: data.get(f, d) for f, d in schema.items()}
        for k, v in data.items():
            if k not in record:
                record[k] = v
    else:
        record = dict(data)
    record.setdefault("deleted", False)
    result = await db[coll_name].insert_one(record)
    return f"Record added to '{collection}' (id: {result.inserted_id})."


@mcp.tool()
async def get_records(collection: str, filters: dict = {}) -> list[dict]:
    """Get records from any collection with optional filters."""
    ctx = await _get_auth_ctx()
    if db is None:
        return [{"error": "MongoDB not configured."}]
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    query = {"deleted": {"$ne": True}}
    query.update(filters)
    cursor = db[coll_name].find(query)
    records = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        records.append(doc)
    return records


@mcp.tool()
async def update_record(collection: str, record_id: str, updates: dict) -> str:
    """Update fields on a specific record."""
    ctx = await _get_auth_ctx()
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    try:
        oid = ObjectId(record_id)
    except Exception:
        return f"Invalid record id '{record_id}'."
    result = await db[coll_name].update_one({"_id": oid}, {"$set": updates})
    if result.matched_count == 0:
        return f"Record '{record_id}' not found."
    return f"Record '{record_id}' updated."


@mcp.tool()
async def delete_record(collection: str, record_id: str) -> str:
    """Soft delete a record. Only master can do this."""
    ctx = require_master()
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    try:
        oid = ObjectId(record_id)
    except Exception:
        return f"Invalid record id '{record_id}'."
    result = await db[coll_name].update_one({"_id": oid}, {"$set": {"deleted": True}})
    if result.matched_count == 0:
        return f"Record '{record_id}' not found."
    return f"Record '{record_id}' deleted."


# ---------------------------------------------------------------------------
# Build Starlette app with auth routes + MCP + auth middleware
# ---------------------------------------------------------------------------
mcp_app = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True)

# Add auth routes to the MCP Starlette app
mcp_app.routes.insert(0, Route("/.well-known/oauth-authorization-server", _well_known, methods=["GET"]))
mcp_app.routes.insert(1, Route("/.well-known/oauth-protected-resource", _protected_resource, methods=["GET"]))
mcp_app.routes.insert(2, Route("/authorize", _authorize, methods=["GET"]))
mcp_app.routes.insert(3, Route("/auth/callback", _callback, methods=["GET"]))
mcp_app.routes.insert(4, Route("/token", _token, methods=["POST"]))
mcp_app.routes.insert(5, Route("/register", _register, methods=["POST"]))

# Wrap with auth middleware
app = AuthMiddleware(mcp_app)
