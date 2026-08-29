"""Tests for collection creation, metadata, and discovery functionality."""
import json
import pytest
import sys
import os
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Add the project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.models import AuthContext, CollectionMetadata


# ---------------------------------------------------------------------------
# Test CollectionMetadata model
# ---------------------------------------------------------------------------
class TestCollectionMetadata:
    def test_generate_from_description(self):
        metadata = CollectionMetadata.generate_from_description(
            user_id="usr_123",
            collection_name="roka_items",
            description="Items and gifts related to my roka ceremony",
        )
        assert metadata.user_id == "usr_123"
        assert metadata.collection_name == "roka_items"
        assert metadata.description == "Items and gifts related to my roka ceremony"
        assert metadata.domain == "wedding"
        assert "roka" in metadata.categories
        assert metadata.metadata_status == "COMPLETE"
        assert "roka_items" in metadata.searchable_text

    def test_generate_from_description_no_domain(self):
        metadata = CollectionMetadata.generate_from_description(
            user_id="usr_123",
            collection_name="abc",
            description="Some random data",
        )
        assert metadata.domain == ""

    def test_to_dict_and_from_dict(self):
        metadata = CollectionMetadata(
            user_id="usr_123",
            collection_name="test_collection",
            description="Test description",
            searchable_text="test collection test description",
            categories=["test", "description"],
            domain="test",
            metadata_status="COMPLETE",
        )
        d = metadata.to_dict()
        restored = CollectionMetadata.from_dict(d)
        assert restored.user_id == metadata.user_id
        assert restored.collection_name == metadata.collection_name
        assert restored.description == metadata.description
        assert restored.categories == metadata.categories
        assert restored.domain == metadata.domain


# ---------------------------------------------------------------------------
# Test in-memory implementation (main.py)
# ---------------------------------------------------------------------------
@pytest.fixture
def setup_main():
    """Set up the in-memory MCP server for testing."""
    import importlib
    main_module = importlib.import_module("src.mcp_server.main")
    from auth.models import generate_user_id

    # Reset state
    main_module._users.clear()
    main_module._user_collections.clear()
    main_module._user_schemas.clear()
    main_module._user_collection_metadata.clear()
    main_module._migrated_masters.clear()

    # Create a master user
    master_id = generate_user_id()
    master_email = "master@example.com"
    main_module._users[master_id] = {
        "email": master_email,
        "name": "Test Master",
        "role": "master",
        "user_id": master_id,
        "master_user_id": master_id,
        "family_emails": [],
        "family_members": [],
        "created_at": datetime.now(timezone.utc),
    }

    ctx = AuthContext(
        user_id=master_id,
        email=master_email,
        name="Test Master",
        role="master",
        master_user_id=master_id,
    )

    return main_module, ctx, master_id


class TestInMemoryCollectionCreation:
    def test_create_collection_with_description(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            result = main_module.create_collection(
                name="roka_items",
                description="Items and gifts related to my roka ceremony",
            )
        assert "created" in result.lower()
        assert "roka_items" in result

        meta_list = main_module._user_collection_metadata.get(master_id, [])
        assert len(meta_list) == 1
        assert meta_list[0]["collection_name"] == "roka_items"
        assert meta_list[0]["description"] == "Items and gifts related to my roka ceremony"

    def test_create_collection_without_description(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            result = main_module.create_collection(name="abc")
        parsed = json.loads(result)
        assert parsed["status"] == "NEEDS_CLARIFICATION"
        assert parsed["collectionName"] == "abc"
        assert "question" in parsed

    def test_create_collection_empty_name(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            result = main_module.create_collection(name="", description="test")
        assert "empty" in result.lower() or "error" in result.lower()

    def test_create_collection_already_exists(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(name="test_coll", description="first")
            result = main_module.create_collection(name="test_coll", description="second")
        assert "already exists" in result.lower()


class TestInMemoryDiscovery:
    def test_discover_collections_found(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="expense",
                description="General wedding expenses and budget tracking",
            )
            main_module.create_collection(
                name="roka",
                description="Roka ceremony items and gifts",
            )
            result = main_module.discover_collections("wedding expenses")

        parsed = json.loads(result)
        assert parsed["status"] == "FOUND"
        assert len(parsed["collections"]) >= 1
        names = [c["collectionName"] for c in parsed["collections"]]
        assert "expense" in names

    def test_discover_collections_not_found(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="fitness",
                description="Workout routines and gym logs",
            )
            result = main_module.discover_collections("wedding expenses")

        parsed = json.loads(result)
        assert parsed["status"] == "NOT_FOUND"
        assert len(parsed["collections"]) == 0

    def test_discover_multiple_collections(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="expense",
                description="General wedding expenses",
            )
            main_module.create_collection(
                name="roka",
                description="Roka ceremony expenses and purchases",
            )
            main_module.create_collection(
                name="venue",
                description="Wedding venue booking and costs",
            )
            result = main_module.discover_collections("wedding expenses including roka")

        parsed = json.loads(result)
        assert parsed["status"] == "FOUND"
        names = [c["collectionName"] for c in parsed["collections"]]
        assert "expense" in names
        assert "roka" in names


class TestInMemoryAuthorization:
    def test_user_scoped_metadata(self, setup_main):
        main_module, ctx, master_id = setup_main
        from auth.models import generate_user_id

        # Create second master user
        master2_id = generate_user_id()
        main_module._users[master2_id] = {
            "email": "master2@example.com",
            "name": "Master 2",
            "role": "master",
            "user_id": master2_id,
            "master_user_id": master2_id,
            "family_emails": [],
            "family_members": [],
            "created_at": datetime.now(timezone.utc),
        }
        ctx2 = AuthContext(
            user_id=master2_id,
            email="master2@example.com",
            name="Master 2",
            role="master",
            master_user_id=master2_id,
        )

        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="user1_data",
                description="User 1 personal data",
            )

        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            main_module.create_collection(
                name="user2_data",
                description="User 2 personal data",
            )

        # User 1 should only see their collections
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            result = main_module.discover_collections("personal data")
        parsed = json.loads(result)
        names = [c["collectionName"] for c in parsed.get("collections", [])]
        assert "user1_data" in names
        assert "user2_data" not in names

        # User 2 should only see their collections
        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            result = main_module.discover_collections("personal data")
        parsed = json.loads(result)
        names = [c["collectionName"] for c in parsed.get("collections", [])]
        assert "user2_data" in names
        assert "user1_data" not in names

    def test_family_member_same_access(self, setup_main):
        main_module, ctx, master_id = setup_main
        from auth.models import generate_user_id

        # Add family member
        family_email = "family@example.com"
        main_module._users[master_id]["family_emails"].append(family_email)
        main_module._users[master_id]["family_members"].append({
            "email": family_email,
            "name": "Family Member",
        })

        family_id = generate_user_id()
        main_module._users[family_id] = {
            "email": family_email,
            "name": "Family Member",
            "role": "family",
            "user_id": family_id,
            "master_user_id": master_id,
            "created_at": datetime.now(timezone.utc),
        }
        family_ctx = AuthContext(
            user_id=family_id,
            email=family_email,
            name="Family Member",
            role="family",
            master_user_id=master_id,
        )

        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="shared_data",
                description="Shared family wedding data",
            )

        # Family member should see the same collections
        with patch("src.mcp_server.main.get_current_user", return_value=family_ctx):
            result = main_module.discover_collections("wedding data")
        parsed = json.loads(result)
        names = [c["collectionName"] for c in parsed.get("collections", [])]
        assert "shared_data" in names


class TestBackwardCompatibility:
    def test_existing_tools_still_work(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            # Create collection with description
            result = main_module.create_collection(
                name="test_coll",
                description="Test collection for backward compatibility",
            )
            assert "created" in result.lower()

            # Add a record
            result = main_module.add_record(
                collection="test_coll",
                data={"name": "item1", "value": 100},
            )
            assert "added" in result.lower()

            # Get records
            result = main_module.get_records(collection="test_coll")
            assert len(result) == 1
            assert result[0]["name"] == "item1"

            # Update record
            record_id = result[0]["_id"]
            result = main_module.update_record(
                collection="test_coll",
                record_id=record_id,
                updates={"value": 200},
            )
            assert "updated" in result.lower()

    def test_manage_collection_get(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="test_manage",
                description="Test manage collection",
            )
            main_module.add_record(
                collection="test_manage",
                data={"key": "value"},
            )
            result = main_module.manage_collection(
                collection="test_manage",
                operation="get",
            )
            assert len(result) == 1
            assert result[0]["key"] == "value"

    def test_manage_collection_update(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="test_manage_update",
                description="Test manage collection update",
            )
            main_module.add_record(
                collection="test_manage_update",
                data={"status": "pending"},
            )
            records = main_module.get_records(collection="test_manage_update")
            record_id = records[0]["_id"]
            result = main_module.manage_collection(
                collection="test_manage_update",
                operation="update",
                record_id=record_id,
                updates={"status": "done"},
            )
            assert "updated" in result.lower()


# ---------------------------------------------------------------------------
# Test enhanced CollectionMetadata — domain, expenses, amount fields
# ---------------------------------------------------------------------------
class TestEnhancedMetadata:
    def test_expense_detection_from_description(self):
        meta = CollectionMetadata.generate_from_description(
            user_id="usr_123",
            collection_name="roka",
            description="Roka ceremony expenses and gifts",
        )
        assert meta.contains_expenses is True
        assert meta.domain == "wedding"
        assert len(meta.amount_fields) > 0

    def test_expense_detection_from_schema(self):
        meta = CollectionMetadata.generate_from_description(
            user_id="usr_123",
            collection_name="honeymoon",
            description="Honeymoon trip details",
            schema={"destination": "string", "flight": 0, "hotel": 0, "food": 0},
        )
        assert meta.contains_expenses is True
        assert "flight" in meta.amount_fields
        assert "hotel" in meta.amount_fields
        assert "food" in meta.amount_fields

    def test_no_expenses_when_not_relevant(self):
        meta = CollectionMetadata.generate_from_description(
            user_id="usr_123",
            collection_name="guest_list",
            description="Names and contact details of guests",
        )
        assert meta.contains_expenses is False
        assert len(meta.amount_fields) == 0

    def test_domain_wedding_keywords(self):
        for name, desc in [("roka", "Roka ceremony"), ("sangeet", "Sangeet night"), ("mehndi", "Mehndi function")]:
            meta = CollectionMetadata.generate_from_description("usr_1", name, desc)
            assert meta.domain == "wedding", f"Expected wedding domain for {name}"

    def test_domain_travel_keywords(self):
        meta = CollectionMetadata.generate_from_description("usr_1", "honeymoon", "Honeymoon trip to Bali")
        assert meta.domain == "travel"

    def test_to_dict_includes_new_fields(self):
        meta = CollectionMetadata.generate_from_description(
            user_id="usr_123",
            collection_name="test",
            description="Test expenses with amount field",
            schema={"amount": 0, "date": "2024-01-01"},
        )
        d = meta.to_dict()
        assert "contains_expenses" in d
        assert "amount_fields" in d
        assert "date_fields" in d
        assert "searchable_fields" in d
        assert d["contains_expenses"] is True
        assert "amount" in d["amount_fields"]
        assert "date" in d["date_fields"]

    def test_from_dict_restores_new_fields(self):
        meta = CollectionMetadata.generate_from_description(
            user_id="usr_123",
            collection_name="test",
            description="Test with expenses",
            schema={"price": 0},
        )
        d = meta.to_dict()
        restored = CollectionMetadata.from_dict(d)
        assert restored.contains_expenses is True
        assert "price" in restored.amount_fields


# ---------------------------------------------------------------------------
# Test list_user_collections
# ---------------------------------------------------------------------------
class TestListUserCollections:
    def test_list_includes_created_collections(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="wedding_expense",
                description="Wedding expense tracking with prices",
                fields={"price": 0, "vendor": ""},
            )
            main_module.create_collection(
                name="honeymoon",
                description="Honeymoon trip planning and costs",
                fields={"destination": "", "budget": 0},
            )
            result = main_module.list_user_collections()

        parsed = json.loads(result)
        assert parsed["totalCollections"] >= 2
        names = [c["name"] for c in parsed["collections"]]
        assert "wedding_expense" in names
        assert "honeymoon" in names

    def test_list_includes_metadata_fields(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="expense",
                description="Wedding expense with amount tracking",
                fields={"amount": 0},
            )
            result = main_module.list_user_collections()

        parsed = json.loads(result)
        expense_coll = next(c for c in parsed["collections"] if c["name"] == "expense")
        assert expense_coll["containsExpenses"] is True
        assert "amount" in expense_coll["amountFields"]
        assert expense_coll["domain"] == "wedding"

    def test_list_includes_fixed_collections(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            result = main_module.list_user_collections()

        parsed = json.loads(result)
        names = [c["name"] for c in parsed["collections"]]
        assert "roka" in names
        assert "shagun" in names
        assert "guests_engagement" in names

    def test_list_user_scoped(self, setup_main):
        main_module, ctx, master_id = setup_main
        from auth.models import generate_user_id

        master2_id = generate_user_id()
        main_module._users[master2_id] = {
            "email": "master2@example.com",
            "name": "Master 2",
            "role": "master",
            "user_id": master2_id,
            "master_user_id": master2_id,
            "family_emails": [],
            "family_members": [],
            "created_at": datetime.now(timezone.utc),
        }
        ctx2 = AuthContext(
            user_id=master2_id,
            email="master2@example.com",
            name="Master 2",
            role="master",
            master_user_id=master2_id,
        )

        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(name="user1_secret", description="User 1 private data")

        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            main_module.create_collection(name="user2_secret", description="User 2 private data")

        # User 1 should only see their collection
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            result = main_module.list_user_collections()
        parsed = json.loads(result)
        names = [c["name"] for c in parsed["collections"]]
        assert "user1_secret" in names
        assert "user2_secret" not in names

        # User 2 should only see their collection
        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            result = main_module.list_user_collections()
        parsed = json.loads(result)
        names = [c["name"] for c in parsed["collections"]]
        assert "user2_secret" in names
        assert "user1_secret" not in names


# ---------------------------------------------------------------------------
# Test aggregate_user_collection
# ---------------------------------------------------------------------------
class TestAggregateUserCollection:
    def test_sum_single_collection(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="expense",
                description="Wedding expenses with amounts",
                fields={"amount": 0},
            )
            main_module.add_record(collection="expense", data={"amount": 5000})
            main_module.add_record(collection="expense", data={"amount": 3000})
            main_module.add_record(collection="expense", data={"amount": 2000})

            result = main_module.aggregate_user_collection(
                collections=["expense"],
                operation="sum",
                field="amount",
            )

        parsed = json.loads(result)
        assert parsed["grandTotal"] == 10000
        assert parsed["collections"]["expense"]["sum"] == 10000
        assert parsed["collections"]["expense"]["recordCount"] == 3

    def test_sum_multiple_collections(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="roka_expense",
                description="Roka ceremony expenses",
                fields={"price": 0},
            )
            main_module.create_collection(
                name="wedding_expense",
                description="Wedding expenses",
                fields={"price": 0},
            )
            main_module.add_record(collection="roka_expense", data={"price": 50000})
            main_module.add_record(collection="wedding_expense", data={"price": 200000})

            result = main_module.aggregate_user_collection(
                collections=["roka_expense", "wedding_expense"],
                operation="sum",
                field="price",
            )

        parsed = json.loads(result)
        assert parsed["grandTotal"] == 250000

    def test_count_operation(self, setup_main):
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="items",
                description="Wedding items",
                fields={"name": ""},
            )
            main_module.add_record(collection="items", data={"name": "item1"})
            main_module.add_record(collection="items", data={"name": "item2"})

            result = main_module.aggregate_user_collection(
                collections=["items"],
                operation="count",
            )

        parsed = json.loads(result)
        assert parsed["totalCount"] == 2

    def test_sum_user_scoped(self, setup_main):
        """User B must not see User A's data in aggregation."""
        main_module, ctx, master_id = setup_main
        from auth.models import generate_user_id

        master2_id = generate_user_id()
        main_module._users[master2_id] = {
            "email": "master2@example.com",
            "name": "Master 2",
            "role": "master",
            "user_id": master2_id,
            "master_user_id": master2_id,
            "family_emails": [],
            "family_members": [],
            "created_at": datetime.now(timezone.utc),
        }
        ctx2 = AuthContext(
            user_id=master2_id,
            email="master2@example.com",
            name="Master 2",
            role="master",
            master_user_id=master2_id,
        )

        # User 1 creates expense with amount
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="my_expense",
                description="User 1 expenses",
                fields={"amount": 0},
            )
            main_module.add_record(collection="my_expense", data={"amount": 10000})

        # User 2 creates expense with amount
        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            main_module.create_collection(
                name="my_expense",
                description="User 2 expenses",
                fields={"amount": 0},
            )
            main_module.add_record(collection="my_expense", data={"amount": 99999})

        # User 1 should only see their 10000
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            result = main_module.aggregate_user_collection(
                collections=["my_expense"],
                operation="sum",
                field="amount",
            )
        parsed = json.loads(result)
        assert parsed["grandTotal"] == 10000

        # User 2 should only see their 99999
        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            result = main_module.aggregate_user_collection(
                collections=["my_expense"],
                operation="sum",
                field="amount",
            )
        parsed = json.loads(result)
        assert parsed["grandTotal"] == 99999


# ---------------------------------------------------------------------------
# Test security — collection name manipulation
# ---------------------------------------------------------------------------
class TestSecurity:
    def test_unauthorized_collection_rejected(self, setup_main):
        """User B trying to query User A's collection should fail."""
        main_module, ctx, master_id = setup_main
        from auth.models import generate_user_id

        master2_id = generate_user_id()
        main_module._users[master2_id] = {
            "email": "master2@example.com",
            "name": "Master 2",
            "role": "master",
            "user_id": master2_id,
            "master_user_id": master2_id,
            "family_emails": [],
            "family_members": [],
            "created_at": datetime.now(timezone.utc),
        }
        ctx2 = AuthContext(
            user_id=master2_id,
            email="master2@example.com",
            name="Master 2",
            role="master",
            master_user_id=master2_id,
        )

        # User 1 creates a collection
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="secret_data",
                description="User 1 secret data",
            )
            main_module.add_record(collection="secret_data", data={"secret": "password123"})

        # User 2 tries to access it — should get error/empty, NOT User 1's data
        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            result = main_module.get_records(collection="secret_data")
            # Should be empty or contain error — NOT User 1's data
            if isinstance(result, list) and len(result) > 0:
                # If it's a list with items, they must be errors, not real data
                assert all(isinstance(r, dict) and "error" in r for r in result)

    def test_collection_name_injection_rejected(self, setup_main):
        """Manipulating collection name with prefix should not bypass auth."""
        main_module, ctx, master_id = setup_main
        from auth.models import generate_user_id

        master2_id = generate_user_id()
        main_module._users[master2_id] = {
            "email": "master2@example.com",
            "name": "Master 2",
            "role": "master",
            "user_id": master2_id,
            "master_user_id": master2_id,
            "family_emails": [],
            "family_members": [],
            "created_at": datetime.now(timezone.utc),
        }
        ctx2 = AuthContext(
            user_id=master2_id,
            email="master2@example.com",
            name="Master 2",
            role="master",
            master_user_id=master2_id,
        )

        # User 1 creates a collection
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="private",
                description="User 1 private data",
            )
            main_module.add_record(collection="private", data={"secret": "value"})

        # User 2 tries to access with User 1's prefix — should fail
        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            result = main_module.get_records(collection=f"{master_id}_private")
            # Should NOT return User 1's data
            if isinstance(result, list) and len(result) > 0:
                # If non-empty, must not contain the real secret
                for r in result:
                    assert r.get("secret") != "value"

    def test_dynamic_creation_immediately_discoverable(self, setup_main):
        """After creating a collection, it should appear in list_user_collections immediately."""
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            # Before creation
            result = main_module.list_user_collections()
            parsed = json.loads(result)
            names = [c["name"] for c in parsed["collections"]]
            assert "new_collection" not in names

            # Create
            main_module.create_collection(
                name="new_collection",
                description="Brand new collection",
            )

            # After creation — should appear immediately
            result = main_module.list_user_collections()
            parsed = json.loads(result)
            names = [c["name"] for c in parsed["collections"]]
            assert "new_collection" in names

    def test_metadata_isolation_between_users(self, setup_main):
        """User A cannot retrieve User B's collection metadata."""
        main_module, ctx, master_id = setup_main
        from auth.models import generate_user_id

        master2_id = generate_user_id()
        main_module._users[master2_id] = {
            "email": "master2@example.com",
            "name": "Master 2",
            "role": "master",
            "user_id": master2_id,
            "master_user_id": master2_id,
            "family_emails": [],
            "family_members": [],
            "created_at": datetime.now(timezone.utc),
        }
        ctx2 = AuthContext(
            user_id=master2_id,
            email="master2@example.com",
            name="Master 2",
            role="master",
            master_user_id=master2_id,
        )

        # User 1 creates collection
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            main_module.create_collection(
                name="user1_wedding",
                description="User 1 wedding data",
            )

        # User 2's list_user_collections should NOT include user1's collection
        with patch("src.mcp_server.main.get_current_user", return_value=ctx2):
            result = main_module.list_user_collections()
        parsed = json.loads(result)
        names = [c["name"] for c in parsed["collections"]]
        assert "user1_wedding" not in names


# ---------------------------------------------------------------------------
# Test cross-collection aggregation flow
# ---------------------------------------------------------------------------
class TestCrossCollectionAggregation:
    def test_full_wedding_expense_flow(self, setup_main):
        """Simulate the full flow: create collections, add expenses, aggregate."""
        main_module, ctx, master_id = setup_main
        with patch("src.mcp_server.main.get_current_user", return_value=ctx):
            # Create collections
            main_module.create_collection(
                name="roka",
                description="Roka ceremony expenses and gifts",
                fields={"amount": 0, "description": ""},
            )
            main_module.create_collection(
                name="wedding",
                description="General wedding expenses",
                fields={"amount": 0, "vendor": ""},
            )
            main_module.create_collection(
                name="catering",
                description="Catering and food expenses",
                fields={"amount": 0, "menu": ""},
            )

            # Add records
            main_module.add_record(collection="roka", data={"amount": 50000, "description": "Roka venue"})
            main_module.add_record(collection="wedding", data={"amount": 200000, "vendor": "Decorators"})
            main_module.add_record(collection="catering", data={"amount": 80000, "menu": "Veg+NonVeg"})

            # Discover
            result = main_module.discover_collections("wedding expenses")
            parsed = json.loads(result)
            names = [c["collectionName"] for c in parsed.get("collections", [])]
            assert "roka" in names
            assert "wedding" in names
            assert "catering" in names

            # Aggregate
            result = main_module.aggregate_user_collection(
                collections=["roka", "wedding", "catering"],
                operation="sum",
                field="amount",
            )
            parsed = json.loads(result)
            assert parsed["grandTotal"] == 330000
            assert parsed["collections"]["roka"]["sum"] == 50000
            assert parsed["collections"]["wedding"]["sum"] == 200000
            assert parsed["collections"]["catering"]["sum"] == 80000

            # Count
            result = main_module.aggregate_user_collection(
                collections=["roka", "wedding", "catering"],
                operation="count",
            )
            parsed = json.loads(result)
            assert parsed["totalCount"] == 3
