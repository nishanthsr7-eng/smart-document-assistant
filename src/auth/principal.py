from dataclasses import dataclass

from src.core.config import SETTINGS

_RANK = {role: i for i, role in enumerate(SETTINGS.auth.roles)}


@dataclass(frozen=True)
class Principal:
    """The authenticated caller. Every tenant-scoped read and write takes its tenant_id."""

    user_id: str
    tenant_id: str
    email: str
    role: str

    def can(self, required: str) -> bool:
        # Admin is checked against the configured addresses, not only against the claim: a row
        # or a token minted before the reservation must not carry admin either.
        if required == "admin" and not SETTINGS.auth.is_admin_email(self.email):
            return False
        return _RANK[self.role] >= _RANK[required]
