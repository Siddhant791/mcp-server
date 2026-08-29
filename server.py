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
from auth.models import AuthContext, FamilyMember, FAMILY_DEFAULT_PERMISSIONS, CollectionMetadata, generate_user_id
from auth.oauth import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
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

# Track master users whose family members have been migrated to new defaults
_migrated_masters: set[str] = set()

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


# ---------------------------------------------------------------------------
# Collection metadata repository
# ---------------------------------------------------------------------------
METADATA_COLLECTION = "_mcp_collection_metadata"


async def _save_collection_metadata(metadata: CollectionMetadata) -> None:
    """Save or update collection metadata for a user."""
    if db is None:
        return
    coll = db[METADATA_COLLECTION]
    metadata.updated_at = datetime.now(timezone.utc)
    await coll.update_one(
        {"user_id": metadata.user_id, "collection_name": metadata.collection_name},
        {"$set": metadata.to_dict()},
        upsert=True,
    )


async def _get_collection_metadata(user_id: str, collection_name: str) -> CollectionMetadata | None:
    """Get metadata for a specific collection."""
    if db is None:
        return None
    coll = db[METADATA_COLLECTION]
    doc = await coll.find_one({"user_id": user_id, "collection_name": collection_name})
    if doc:
        return CollectionMetadata.from_dict(doc)
    return None


async def _get_all_user_metadata(user_id: str) -> list[CollectionMetadata]:
    """Get all collection metadata for a user."""
    if db is None:
        return []
    coll = db[METADATA_COLLECTION]
    results = []
    async for doc in coll.find({"user_id": user_id}):
        results.append(CollectionMetadata.from_dict(doc))
    return results


async def _search_collection_metadata(user_id: str, query: str) -> list[dict]:
    """Search collection metadata by text matching against name, description, and categories."""
    all_metadata = await _get_all_user_metadata(user_id)
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
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["openid", "email", "profile"],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    })


async def _openid_configuration(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    })


async def _protected_resource(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resource": base + '/mcp',
        "authorization_servers": [base],
        "scopes_supported": ["openid", "email", "profile"],
        "bearer_methods_supported": ["header"],
    })


async def _authorize(request: Request) -> RedirectResponse:
    chatgpt_redirect = request.query_params.get("redirect_uri", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "S256")
    chatgpt_state = request.query_params.get("state", "")
    resource = request.query_params.get("resource", "")
    client_id = request.query_params.get("client_id", "")
    print(f"[AUTHORIZE] redirect_uri={chatgpt_redirect[:80]}... state={chatgpt_state[:20] if chatgpt_state else 'None'}... resource={resource} client_id={client_id[:40] if client_id else 'None'}", flush=True)
    google_state = secrets.token_urlsafe(32)
    if db is not None:
        await db["_oauth_states"].insert_one({
            "state": google_state,
            "chatgpt_redirect_uri": chatgpt_redirect,
            "chatgpt_state": chatgpt_state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "resource": resource,
            "client_id": client_id,
            "created_at": datetime.now(timezone.utc),
        })
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

    print(f"[CALLBACK] has_code={bool(code)} state_prefix={google_state[:8] if google_state else 'None'}", flush=True)

    if not code or not google_state:
        print("[CALLBACK] ERROR: Missing code or state", flush=True)
        return HTMLResponse("<html><body>Missing code or state</body></html>", status_code=400)

    stored_state = None
    if db is not None:
        stored_state = await db["_oauth_states"].find_one_and_delete({"state": google_state})
    print(f"[CALLBACK] stored_state={stored_state is not None}, chatgpt_redirect={stored_state.get('chatgpt_redirect_uri', 'EMPTY') if stored_state else 'NOT FOUND'}", flush=True)
    if not stored_state:
        return HTMLResponse("<html><body>Invalid or expired state</body></html>", status_code=400)

    chatgpt_redirect = stored_state.get("chatgpt_redirect_uri", "")
    chatgpt_state = stored_state.get("chatgpt_state", "")

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
    server_base = str(request.base_url).rstrip("/")
    jwt_token = create_jwt(user_id, email, name, role, master_user_id, issuer=server_base)

    server_code = secrets.token_urlsafe(32)
    code_challenge = stored_state.get("code_challenge", "")
    code_challenge_method = stored_state.get("code_challenge_method", "S256")
    client_id = stored_state.get("client_id", "")
    resource = stored_state.get("resource", "")
    if db is not None:
        await db["_oauth_codes"].insert_one({
            "code": server_code,
            "jwt_token": jwt_token,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "client_id": client_id,
            "resource": resource,
            "created_at": datetime.now(timezone.utc),
        })

    print(f"[CALLBACK] user={email} role={role} server_code_prefix={server_code[:8]}", flush=True)

    if chatgpt_redirect:
        from urllib.parse import urlencode as _urlencode
        sep = "&" if "?" in chatgpt_redirect else "?"
        params = {"code": server_code, "iss": server_base}
        if chatgpt_state:
            params["state"] = chatgpt_state
        redirect_url = f"{chatgpt_redirect}{sep}{_urlencode(params)}"
        print(f"[CALLBACK] redirect_uri_prefix={chatgpt_redirect[:60]}", flush=True)
        return RedirectResponse(redirect_url)

    print("[CALLBACK] NO chatgpt_redirect — returning HTML fallback", flush=True)
    return HTMLResponse("""<!DOCTYPE html>
<html><head><title>Auth Complete</title></head>
<body><p>Authentication successful. You can close this window.</p></body></html>""")


async def _register(request: Request) -> JSONResponse:
    body = await request.json()
    client_name = body.get("client_name", "mcp-client")
    client_id = "mcp_client_" + secrets.token_hex(8)
    if db is not None:
        await db["_registered_clients"].insert_one({
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": body.get("redirect_uris", []),
            "created_at": datetime.now(timezone.utc),
        })
    else:
        _registered_clients[client_id] = {
            "client_name": client_name,
            "redirect_uris": body.get("redirect_uris", []),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    print(f"[REGISTER] client_id={client_id[:20]}... name={client_name}", flush=True)
    return JSONResponse({
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })


async def _token(request: Request) -> JSONResponse:
    import hashlib, base64
    try:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.json()
        else:
            form = await request.form()
            body = dict(form)
        grant_type = body.get("grant_type", "")
        code = body.get("code", "")
        code_verifier = body.get("code_verifier", "")
        client_id = body.get("client_id", "")
        print(f"[TOKEN] grant_type={grant_type} has_code={bool(code)} has_verifier={bool(code_verifier)} ct={content_type}", flush=True)

        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

        auth_info = None
        if db is not None:
            auth_info = await db["_oauth_codes"].find_one_and_delete({"code": code})

        if auth_info:
            # --- Server-issued code (MCP OAuth flow) ---
            created_at = auth_info.get("created_at")
            if created_at:
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
                if age_seconds > 600:
                    print(f"[TOKEN] Code expired (age={age_seconds:.0f}s)", flush=True)
                    return JSONResponse({"error": "invalid_grant", "detail": "Authorization code expired"}, status_code=400)

            stored_challenge = auth_info.get("code_challenge", "")
            stored_method = auth_info.get("code_challenge_method", "S256")
            stored_client_id = auth_info.get("client_id", "")

            # Require code_verifier when a code_challenge was stored
            if stored_challenge:
                if not code_verifier:
                    print("[TOKEN] PKCE required but code_verifier missing", flush=True)
                    return JSONResponse({"error": "invalid_grant", "detail": "code_verifier is required"}, status_code=400)

                # Only S256 is supported
                if stored_method and stored_method != "S256":
                    print(f"[TOKEN] Unsupported code_challenge_method: {stored_method}", flush=True)
                    return JSONResponse({"error": "invalid_grant", "detail": f"Unsupported code_challenge_method: {stored_method}"}, status_code=400)

                # Verify S256 challenge
                computed = base64.urlsafe_b64encode(
                    hashlib.sha256(code_verifier.encode()).digest()
                ).rstrip(b"=").decode()
                if computed != stored_challenge:
                    print("[TOKEN] PKCE verification failed", flush=True)
                    return JSONResponse({"error": "invalid_grant", "detail": "PKCE verification failed"}, status_code=400)

            # Reject client_id mismatch
            if stored_client_id and client_id and stored_client_id != client_id:
                print(f"[TOKEN] client_id mismatch stored_prefix={stored_client_id[:20]} request_prefix={client_id[:20]}", flush=True)
                return JSONResponse({"error": "invalid_grant", "detail": "client_id mismatch"}, status_code=400)

            print("[TOKEN] Token issued successfully", flush=True)
            return JSONResponse({
                "access_token": auth_info["jwt_token"],
                "token_type": "bearer",
                "expires_in": 86400,
            })

        # --- Google OAuth fallback (direct Google code exchange) ---
        print("[TOKEN] Code not found in _oauth_codes, falling back to Google OAuth", flush=True)
        client_secret = body.get("client_secret", "")

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
        server_base = str(request.base_url).rstrip("/")
        jwt_token = create_jwt(user_id, email, name, role, master_user_id, issuer=server_base)
        print("[TOKEN] Google OAuth token issued successfully", flush=True)
        return JSONResponse({
            "access_token": jwt_token,
            "token_type": "bearer",
            "expires_in": 86400,
        })

    except Exception as e:
        import traceback
        print(f"[TOKEN] ERROR: {e}\n{traceback.format_exc()}", flush=True)
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
    cursor = coll.find({"deleted": {"$ne": True}})
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
    todo = {"title": title, "completed": False, "deleted": False}
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
    doc = await coll.find_one({"title": title, "deleted": {"$ne": True}})
    if not doc:
        available = await coll.distinct("title", {"deleted": {"$ne": True}})
        if available:
            return f"Todo '{title}' not found. Available: {', '.join(available)}"
        return f"Todo '{title}' not found. The todo list is empty."
    new_status = not doc.get("completed", False)
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"completed": new_status}})
    status_text = "completed" if new_status else "incomplete"
    return f"Todo '{title}' marked as {status_text}."


@mcp.tool()
async def delete_todo(title: str) -> str:
    """Soft delete a todo item from the list."""
    ctx = await _get_auth_ctx()
    if ctx.role == "family":
        master_doc = await users_collection.find_one({"user_id": ctx.master_user_id})
        member = next((m for m in master_doc.get("family_members", []) if m["email"] == ctx.email), None)
        if member and not member.get("permissions", {}).get("can_toggle_todo", True):
            return "Permission denied: can_toggle_todo is disabled for your account."
    if db is None:
        return "Error: MongoDB not configured."
    coll = db[_prefixed("todos", ctx)]
    doc = await coll.find_one({"title": title, "deleted": {"$ne": True}})
    if not doc:
        available = await coll.distinct("title", {"deleted": {"$ne": True}})
        if available:
            return f"Todo '{title}' not found. Available: {', '.join(available)}"
        return f"Todo '{title}' not found. The todo list is empty."
    await coll.update_one({"_id": doc["_id"]}, {"$set": {"deleted": True}})
    return f"Todo '{title}' deleted."


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

    # Auto-migrate existing family members to new permission defaults (run once per master user)
    if ctx.master_user_id not in _migrated_masters:
        _migrated_masters.add(ctx.master_user_id)
        updated_members = []
        for m in master_doc.get("family_members", []):
            new_perms = dict(FAMILY_DEFAULT_PERMISSIONS)
            new_perms.update(m.get("permissions", {}))
            new_perms["can_add_family_members"] = False
            if new_perms != m.get("permissions"):
                m["permissions"] = new_perms
                updated_members.append(m)
        if updated_members:
            await users_collection.update_one(
                {"user_id": ctx.master_user_id},
                {"$set": {"family_members": master_doc.get("family_members", [])}},
            )

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
async def create_collection(name: str, description: str = "", fields: dict = {}) -> str:
    """Create a new collection with a defined schema. A description is required for semantic discovery. After creation use add_record, get_records, update_record, delete_record.

    If you provide only a collection name without a description, the server will ask for clarification about what the collection stores.
    """
    ctx = await _get_auth_ctx()
    if perm_err := await _check_family_perm(ctx, "can_create_collection"):
        return perm_err
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)
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

    metadata = CollectionMetadata.generate_from_description(ctx.master_user_id, clean, desc_clean)
    await _save_collection_metadata(metadata)

    field_list = ", ".join(fields.keys()) if fields else "none"
    return f"Collection '{clean}' created with fields: {field_list}."


@mcp.tool()
async def discover_collections(query: str) -> list[dict] | str:
    """Find which user collections contain data relevant to a query. ALWAYS call this first before answering any data question.

    For broad questions like 'total wedding expenses' or 'show me all my data', call this MULTIPLE TIMES with different keywords (e.g. 'expense', 'roka', 'vendor', 'cost', 'payment') to find ALL relevant collections. Then query each one and combine results.

    Returns a list of matching collections with names and descriptions."""
    ctx = await _get_auth_ctx()
    if db is None:
        return "Error: MongoDB not configured."

    # 1. Search metadata
    results = await _search_collection_metadata(ctx.master_user_id, query)

    # 2. Also list ALL user collections and do fuzzy name matching
    await _load_user_schemas(ctx)
    all_collections = _user_dynamic_collections.get(ctx.master_user_id, set())
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
        # Substring: entire query in name
        if query_lower in name_lower:
            score += 5
        # Partial: any query word is a prefix of a name part
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
    """Get all records from a collection by its exact name. Requires the actual MongoDB collection name, not a natural-language concept.

    For any broad question (totals, summaries, lists of data), you must first call discover_collections with multiple queries to find ALL relevant collections, then call get_records on EACH one and combine the results. Never assume a single collection has all the data.
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
    """Soft delete a record."""
    ctx = await _get_auth_ctx()
    if perm_err := await _check_family_perm(ctx, "can_delete_record"):
        return perm_err
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
# Unified collection manager
# ---------------------------------------------------------------------------
async def _handle_get_records(ctx: AuthContext, collection: str, **kwargs) -> list[dict]:
    """Retrieve records from a collection with optional filters."""
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return [{"error": f"Invalid collection '{collection}'."}]
    query = {"deleted": {"$ne": True}}
    query.update(kwargs.get("filters", {}))
    cursor = db[coll_name].find(query)
    records = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        records.append(doc)
    return records


async def _handle_update_record(ctx: AuthContext, collection: str, **kwargs) -> str:
    """Update a specific record in a collection."""
    coll_name = await _resolve(collection, ctx)
    if not coll_name:
        return f"Invalid collection '{collection}'."
    record_id = kwargs.get("record_id", "")
    updates = kwargs.get("updates", {})
    if not record_id:
        return "Error: 'record_id' is required for update operation."
    if not updates:
        return "Error: 'updates' dict is required for update operation."
    try:
        oid = ObjectId(record_id)
    except Exception:
        return f"Invalid record id '{record_id}'."
    result = await db[coll_name].update_one({"_id": oid}, {"$set": updates})
    if result.matched_count == 0:
        return f"Record '{record_id}' not found."
    return f"Record '{record_id}' updated."


_COLLECTION_OPERATIONS: dict[str, callable] = {
    "get": _handle_get_records,
    "update": _handle_update_record,
}


@mcp.tool()
async def manage_collection(
    collection: str,
    operation: str,
    filters: dict = {},
    record_id: str = "",
    updates: dict = {},
) -> list[dict] | str:
    """Manage records in a user-specific collection. This tool requires an actual MongoDB collection name, NOT a natural-language concept.

    IMPORTANT: If the user asks a broad semantic question like 'Give me my wedding expenses', do NOT guess a collection name. Instead:
    1. First call discover_collections('wedding expenses') to find relevant collections
    2. Then call manage_collection or get_records on each discovered collection
    3. Combine the results

    Supported operations:
      - get:     Retrieve records. Use 'filters' to narrow results (e.g. {"status": "active"}).
      - update:  Update a record. Provide 'record_id' and 'updates' dict (e.g. {"status": "done"}).

    Collections are user-scoped — you can only access your own collections.
    """
    ctx = await _get_auth_ctx()
    if db is None:
        return "Error: MongoDB not configured."
    await _load_user_schemas(ctx)

    handler = _COLLECTION_OPERATIONS.get(operation)
    if not handler:
        valid = ", ".join(_COLLECTION_OPERATIONS.keys())
        return f"Invalid operation '{operation}'. Valid operations: {valid}"

    return await handler(ctx, collection, filters=filters, record_id=record_id, updates=updates)


# ---------------------------------------------------------------------------
# Build Starlette app with auth routes + MCP + auth middleware
# ---------------------------------------------------------------------------
mcp_app = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True)

# Add auth routes to the MCP Starlette app
mcp_app.routes.insert(0, Route("/.well-known/oauth-authorization-server", _well_known, methods=["GET"]))
mcp_app.routes.insert(1, Route("/.well-known/oauth-protected-resource", _protected_resource, methods=["GET"]))
mcp_app.routes.insert(2, Route("/.well-known/openid-configuration", _openid_configuration, methods=["GET"]))
mcp_app.routes.insert(3, Route("/authorize", _authorize, methods=["GET"]))
mcp_app.routes.insert(4, Route("/auth/callback", _callback, methods=["GET"]))
mcp_app.routes.insert(5, Route("/token", _token, methods=["POST"]))
mcp_app.routes.insert(6, Route("/register", _register, methods=["POST"]))

# Wrap with auth middleware
app = AuthMiddleware(mcp_app)
