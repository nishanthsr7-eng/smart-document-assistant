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
        return _RANK[self.role] >= _RANK[required]
