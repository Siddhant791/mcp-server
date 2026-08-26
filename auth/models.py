from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class FamilyMember:
    email: str
    name: str
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    permissions: dict = field(default_factory=lambda: {
        "can_add_guest": True,
        "can_remove_guest": True,
        "can_toggle_invited": True,
        "can_add_family_members": False,
        "can_record_gift": True,
        "can_create_collection": True,
        "can_manage_users": True,
        "can_delete_record": True,
        "can_add_todo": True,
        "can_toggle_todo": True,
    })

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "name": self.name,
            "added_at": self.added_at.isoformat(),
            "permissions": self.permissions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FamilyMember:
        added_at = data.get("added_at")
        if isinstance(added_at, str):
            added_at = datetime.fromisoformat(added_at)
        elif added_at is None:
            added_at = datetime.now(timezone.utc)
        return cls(
            email=data["email"],
            name=data["name"],
            added_at=added_at,
            permissions=data.get("permissions", cls().permissions),
        )


@dataclass
class AuthContext:
    user_id: str
    email: str
    name: str
    role: str  # "master" or "family"
    master_user_id: str  # For family members, points to the master. For master, same as user_id.

    @property
    def collection_prefix(self) -> str:
        return f"{self.master_user_id}_"

    def has_permission(self, permission: str) -> bool:
        if self.role == "master":
            return True
        return False  # Family permissions are checked separately via user doc


FAMILY_DEFAULT_PERMISSIONS: dict = {
    "can_add_guest": True,
    "can_remove_guest": True,
    "can_toggle_invited": True,
    "can_add_family_members": False,
    "can_record_gift": True,
    "can_create_collection": True,
    "can_manage_users": True,
    "can_delete_record": True,
    "can_add_todo": True,
    "can_toggle_todo": True,
}


def generate_user_id() -> str:
    return "usr_" + secrets.token_hex(4)


@dataclass
class CollectionMetadata:
    user_id: str
    collection_name: str
    description: str
    searchable_text: str = ""
    categories: list[str] = field(default_factory=list)
    domain: str = ""
    metadata_status: str = "COMPLETE"  # COMPLETE or NEEDS_DESCRIPTION
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "collection_name": self.collection_name,
            "description": self.description,
            "searchable_text": self.searchable_text,
            "categories": self.categories,
            "domain": self.domain,
            "metadata_status": self.metadata_status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> CollectionMetadata:
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now(timezone.utc)
        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        elif updated_at is None:
            updated_at = datetime.now(timezone.utc)
        return cls(
            user_id=data["user_id"],
            collection_name=data["collection_name"],
            description=data.get("description", ""),
            searchable_text=data.get("searchable_text", ""),
            categories=data.get("categories", []),
            domain=data.get("domain", ""),
            metadata_status=data.get("metadata_status", "COMPLETE"),
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def generate_from_description(
        user_id: str, collection_name: str, description: str
    ) -> CollectionMetadata:
        """Generate semantic metadata from a user-provided description."""
        desc_lower = description.lower()
        words = set()
        for word in desc_lower.split():
            cleaned = word.strip(".,;:!?\"'()-")
            if len(cleaned) > 2:
                words.add(cleaned)

        domain = ""
        domain_keywords = {
            "wedding": ["wedding", "ceremony", "bride", "groom", "marriage", "reception"],
            "finance": ["expense", "cost", "budget", "price", "payment", "money", "investment"],
            "travel": ["travel", "trip", "flight", "hotel", "vacation", "honeymoon"],
            "food": ["food", "catering", "menu", "meal", "restaurant"],
            "gift": ["gift", "shagun", "present", "return gift"],
            "venue": ["venue", "hall", "place", "location"],
        }
        for d, keywords in domain_keywords.items():
            if any(kw in desc_lower for kw in keywords):
                domain = d
                break

        categories = [w for w in words if len(w) > 3][:10]
        searchable_text = f"{collection_name} {description}".lower()

        return CollectionMetadata(
            user_id=user_id,
            collection_name=collection_name,
            description=description,
            searchable_text=searchable_text,
            categories=categories,
            domain=domain,
            metadata_status="COMPLETE",
        )
