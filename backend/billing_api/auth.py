"""Role-based authorization (spec §9.5).

POC ONLY: identity and role are taken from the X-User / X-Role request headers so
the UI can switch roles for demos. In production, replace `current_user` with the
company SSO/JWT dependency; everything downstream only consumes `User`.
"""

from dataclasses import dataclass

from fastapi import Depends, Header

from .errors import ApiError

ROLES = ("VIEWER", "EDITOR", "PUBLISHER", "ADMIN")
_RANK = {role: i for i, role in enumerate(ROLES)}


@dataclass(frozen=True)
class User:
    id: str
    role: str

    def has(self, role: str) -> bool:
        return _RANK[self.role] >= _RANK[role]


def current_user(
    x_user: str | None = Header(default=None),
    x_role: str | None = Header(default=None),
) -> User:
    if not x_user:
        raise ApiError(401, "UNAUTHENTICATED", "Missing X-User header")
    role = (x_role or "VIEWER").upper()
    if role not in _RANK:
        raise ApiError(401, "UNAUTHENTICATED", f"Unknown role {x_role!r}")
    return User(id=x_user, role=role)


def require(role: str):
    def dependency(user: User = Depends(current_user)) -> User:
        if not user.has(role):
            raise ApiError(403, "FORBIDDEN", f"Requires {role} role (you are {user.role})")
        return user

    return dependency
