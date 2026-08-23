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
