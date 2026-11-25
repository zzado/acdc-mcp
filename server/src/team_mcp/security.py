import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from .db import DomainError, audit, canonical, now


@dataclass(frozen=True)
class Principal:
    agent_id: str
    user_id: str
    credential_id: str
    role: str
    registered: bool


class Security:
    def __init__(self, db, pepper: str):
        if len(pepper) < 32:
            raise ValueError("TEAM_TOKEN_PEPPER must contain at least 32 characters")
        self.db = db
        self.pepper = pepper.encode()

    def sign(self, value: bytes) -> str:
        return hmac.digest(self.pepper, value, hashlib.sha256).hex()

    def issue(self, cx, agent_id: str, expires: int) -> str:
        credential = uuid4().hex
        token = f"tmc_{credential}.{secrets.token_urlsafe(32)}"
        cx.execute("INSERT INTO credentials VALUES (?,?,?,?,NULL,?)",
                   (credential, agent_id, self.sign(token.encode()), expires, now()))
        audit(cx, "credential.issue", actor=agent_id, entity=credential)
        return token

    def authenticate(self, token: str) -> Principal | None:
        prefix, sep, _ = token.partition(".")
        if not sep or not prefix.startswith("tmc_") or len(prefix) != 36 or len(token) > 100:
            return None
        with self.db.transaction() as cx:
            row = cx.execute("""
                SELECT c.token_hash,c.expires_at,c.revoked_at,a.*,u.role,u.disabled_at
                FROM credentials c JOIN agents a ON a.id=c.agent_id JOIN users u ON u.id=a.user_id
                WHERE c.id=?
                """, (prefix[4:],)).fetchone()
        if (not row or row["revoked_at"] is not None or row["disabled_at"] is not None
                or row["expires_at"] <= now() or not hmac.compare_digest(self.sign(token.encode()), row["token_hash"])):
            return None
        if row["name"] and (row["last_seen_at"] or 0) <= now() - 30:
            with self.db.transaction(write=True) as cx:
                cx.execute("UPDATE agents SET last_seen_at=? WHERE id=? AND coalesce(last_seen_at,0)<=?",
                           (now(), row["id"], now() - 30))
        return Principal(row["id"], row["user_id"], prefix[4:], row["role"], bool(row["name"]))

    def cursor(self, binding, position):
        payload = base64.urlsafe_b64encode(canonical([binding, position]).encode()).decode().rstrip("=")
        return f"{payload}.{self.sign(payload.encode())}"

    def position(self, cursor, binding):
        if cursor is None:
            return None
        payload, _, signature = cursor.partition(".")
        if len(payload) > 1000 or not hmac.compare_digest(self.sign(payload.encode()), signature):
            raise DomainError("VALIDATION", "Invalid cursor")
        try:
            stored, position = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, binascii.Error):
            raise DomainError("VALIDATION", "Invalid cursor") from None
        if stored != binding:
            raise DomainError("VALIDATION", "Cursor belongs to a different query")
        return position


CHANNEL_VISIBLE = """(
    c.kind='team' OR
    (c.kind='private' AND EXISTS (SELECT 1 FROM channel_members cm WHERE cm.channel_id=c.id AND cm.user_id=:user)) OR
    (c.kind='dm' AND :agent IN (c.dm_first,c.dm_second))
)"""


def require_active_credential(cx, who):
    if not cx.execute("""
        SELECT 1 FROM credentials c JOIN agents a ON a.id=c.agent_id JOIN users u ON u.id=a.user_id
        WHERE c.id=? AND c.agent_id=? AND c.revoked_at IS NULL AND u.disabled_at IS NULL AND c.expires_at>?
        """, (who.credential_id, who.agent_id, now())).fetchone():
        raise DomainError("FORBIDDEN", "This Agent's credential is unavailable")


def accessible_channel(cx, who, channel_id):
    if not who.registered:
        raise DomainError("FORBIDDEN", "Register the Agent first")
    row = cx.execute(f"SELECT c.* FROM channels c WHERE c.id=:channel AND {CHANNEL_VISIBLE}",
                     {"channel": channel_id, "user": who.user_id, "agent": who.agent_id}).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "Channel or object not found")
    return row


class RateLimits:
    def __init__(self):
        self.buckets = {}
        self.lock = threading.Lock()
        self.last_cleanup = 0.0

    def take(self, key, *, rate=2.0, burst=30):
        current = time.monotonic()
        with self.lock:
            if current - self.last_cleanup > 60:
                self.buckets = {k: v for k, v in self.buckets.items() if current - v[1] < 120}
                self.last_cleanup = current
            if key not in self.buckets and len(self.buckets) >= 8192:
                key = ("overflow", key[0])
            tokens, previous = self.buckets.get(key, (burst, current))
            tokens = min(burst, tokens + (current - previous) * rate)
            self.buckets[key] = (tokens - 1 if tokens >= 1 else tokens, current)
        if tokens < 1:
            raise DomainError("RATE_LIMITED", "Request limit reached", retryable=True,
                              retry_after_ms=max(1, int(1000 / rate)))
