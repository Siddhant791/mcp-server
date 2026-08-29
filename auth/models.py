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
    contains_expenses: bool = False
    amount_fields: list[str] = field(default_factory=list)
    date_fields: list[str] = field(default_factory=list)
    searchable_fields: list[str] = field(default_factory=list)
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
            "contains_expenses": self.contains_expenses,
            "amount_fields": self.amount_fields,
            "date_fields": self.date_fields,
            "searchable_fields": self.searchable_fields,
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
            contains_expenses=data.get("contains_expenses", False),
            amount_fields=data.get("amount_fields", []),
            date_fields=data.get("date_fields", []),
            searchable_fields=data.get("searchable_fields", []),
            metadata_status=data.get("metadata_status", "COMPLETE"),
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def generate_from_description(
        user_id: str, collection_name: str, description: str, schema: dict | None = None
    ) -> CollectionMetadata:
        """Generate semantic metadata from a user-provided description and optional schema."""
        desc_lower = description.lower()
        name_lower = collection_name.lower()
        combined = f"{collection_name} {description}".lower()
        words = set()
        for word in desc_lower.split():
            cleaned = word.strip(".,;:!?\"'()-")
            if len(cleaned) > 2:
                words.add(cleaned)

        # Domain detection
        domain = ""
        domain_keywords = {
            "wedding": ["wedding", "ceremony", "bride", "groom", "marriage", "reception", "roka", "sangeet", "mehndi"],
            "finance": ["expense", "cost", "budget", "price", "payment", "money", "investment", "bill", "invoice"],
            "travel": ["travel", "trip", "flight", "hotel", "vacation", "honeymoon", "booking"],
            "food": ["food", "catering", "menu", "meal", "restaurant", "caterer"],
            "gift": ["gift", "shagun", "present", "return gift"],
            "venue": ["venue", "hall", "place", "location", "banquet"],
            "decoration": ["decoration", "decor", "floral", "lighting", "setup"],
            "photography": ["photography", "photo", "video", "album", "cinematography"],
            "home": ["home", "furniture", "appliance", "renovation", "interior"],
        }
        for d, keywords in domain_keywords.items():
            if any(kw in combined or kw in name_lower for kw in keywords):
                domain = d
                break

        # Expense detection
        expense_keywords = [
            "expense", "cost", "price", "amount", "budget", "payment", "bill",
            "total", "paid", "spend", "spent", "fee", "charge", "rate", "deposit",
        ]
        contains_expenses = any(kw in combined for kw in expense_keywords)

        # Amount field detection from schema
        amount_field_names = [
            "price", "amount", "cost", "total", "expense", "budget", "payment",
            "paid", "fee", "rate", "deposit", "value", "sum", "charge",
            "flight", "food", "activities", "hotel", "venue_charge",
        ]
        date_field_names = [
            "date", "created_at", "updated_at", "booking_date", "event_date",
            "start_date", "end_date", "due_date", "payment_date",
        ]
        searchable_field_names = [
            "name", "description", "title", "note", "vendor", "category",
            "type", "status", "location", "comment", "detail",
        ]

        amount_fields = []
        date_fields = []
        searchable_fields = []

        if schema:
            for field_name in schema:
                fn = field_name.lower()
                if fn in amount_field_names or any(kw in fn for kw in ["price", "amount", "cost", "total", "expense", "budget", "payment"]):
                    amount_fields.append(field_name)
                    contains_expenses = True
                if fn in date_field_names or any(kw in fn for kw in ["date", "time"]):
                    date_fields.append(field_name)
                if fn in searchable_field_names or any(kw in fn for kw in ["name", "desc", "title", "note", "vendor"]):
                    searchable_fields.append(field_name)

        # Infer amount fields from description if schema didn't provide any
        if not amount_fields and contains_expenses:
            amount_fields = ["amount"]
            if "price" in combined:
                amount_fields = ["price"]
            elif "cost" in combined:
                amount_fields = ["cost"]
            elif "total" in combined:
                amount_fields = ["total"]

        categories = [w for w in words if len(w) > 3][:10]
        searchable_text = f"{collection_name} {description}".lower()

        return CollectionMetadata(
            user_id=user_id,
            collection_name=collection_name,
            description=description,
            searchable_text=searchable_text,
            categories=categories,
            domain=domain,
            contains_expenses=contains_expenses,
            amount_fields=amount_fields,
            date_fields=date_fields,
            searchable_fields=searchable_fields,
            metadata_status="COMPLETE",
        )
