"""Bearer tokens. A token is shown once at issue; only its SHA-256 digest is stored.

Roles: ``app`` (the HiFiSync app and other clients), ``ha`` (the Home Assistant
integration, which owns a node's room and name) and ``node`` (a measurement node, bound to
its own node id).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from hifisync_acoustics.store.db import Store

ROLES = ("app", "ha", "node")


@dataclass(frozen=True)
class TokenInfo:
    name: str
    role: str
    node_id: str | None = None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(store: Store, name: str, role: str = "app", node_id: str | None = None) -> str:
    """Create a token named ``name`` and return it; it cannot be read back later."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if role == "node" and not node_id:
        raise ValueError("a node token needs a node id")
    if role != "node" and node_id is not None:
        raise ValueError("only a node token takes a node id")
    if not name or not name.strip():
        raise ValueError("a token needs a name")
    token = secrets.token_urlsafe(32)
    store.add_token(name, _digest(token), role, node_id)
    return token


def lookup(store: Store, token: str) -> TokenInfo | None:
    """The active token matching ``token``, compared in constant time against every
    active digest."""
    if not token:
        return None
    digest = _digest(token)
    found: TokenInfo | None = None
    for row in store.active_tokens():
        if hmac.compare_digest(row["digest"], digest):
            found = TokenInfo(row["name"], row["role"], row["node_id"])
    return found


def verify(store: Store, token: str) -> bool:
    return lookup(store, token) is not None


def revoke(store: Store, name: str) -> int:
    """Revoke the active token named ``name``; returns how many were revoked."""
    return store.revoke_token(name)
