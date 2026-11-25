import asyncio
import hmac
import json
import re
import sqlite3
from uuid import uuid4

from .db import DomainError, canonical, event, idempotent, iso, now
from .security import CHANNEL_VISIBLE, accessible_channel, require_active_credential


# Spell out Unicode space separators so Python and JSON Schema clients agree on blank text.
INVALID_DISPLAY_TEXT = re.compile(
    r"[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028-\u202e\u2066-\u2069]"
    r"|^[ \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+$"
)


def validate_display_text(value):
    if not value or INVALID_DISPLAY_TEXT.search(value):
        raise DomainError("VALIDATION", "Use nonblank, single-line text without control or bidi formatting characters")


def view(row):
    result = dict(row)
    for key, value in result.items():
        if key.endswith("_at"):
            result[key] = iso(value)
    return result


class Collaboration:
    def __init__(self, db, security):
        self.db = db
        self.security = security
        self.waiters = set()

    def page(self, who, name, filters, cursor, limit, position, read, format_item=view):
        binding = [who.agent_id, name, filters]
        rows = read(self.security.position(cursor, binding), limit + 1)
        next_cursor = self.security.cursor(binding, position(rows[limit - 1])) if len(rows) > limit else None
        return {"items": [format_item(row) for row in rows[:limit]], "next_cursor": next_cursor}

    def register_agent(self, who, name, description, runtime):
        validate_display_text(name)
        if runtime is not None:
            validate_display_text(runtime)
            runtime = runtime.strip()
        if description:
            validate_display_text(description)
        with self.db.transaction(write=True) as cx:
            cx.execute("UPDATE agents SET name=?,description=?,runtime=?,last_seen_at=? WHERE id=?",
                       (name.strip(), description, runtime, now(), who.agent_id))
            return view(cx.execute("SELECT * FROM agents WHERE id=?", (who.agent_id,)).fetchone())

    def list_agents(self, who, agent_id, cursor, limit):
        with self.db.transaction() as cx:
            def read(after, count):
                return cx.execute("""
                    SELECT a.*, u.name AS owner_name FROM agents a JOIN users u ON u.id=a.user_id
                    WHERE u.disabled_at IS NULL AND a.id>? AND (? IS NULL OR a.id=?)
                    ORDER BY a.id LIMIT ?
                    """, (after or "", agent_id, agent_id, count)).fetchall()

            def format_agent(row):
                return view(row) | {"name": row["name"] or row["owner_name"],
                                    "active": (row["last_seen_at"] or 0) >= now() - 120}

            return self.page(who, "agents", agent_id, cursor, limit, lambda row: row["id"], read, format_agent)

    def create_channel(self, who, name, key):
        stored_name = name.strip()
        with self.db.transaction(write=True) as cx:
            def create():
                # Previously committed requests can still replay their original receipt.
                validate_display_text(name)
                channel = uuid4().hex
                try:
                    cx.execute("INSERT INTO channels(id,name,kind,creator_id,created_at) VALUES (?,?,'team',?,?)",
                               (channel, stored_name, who.agent_id, now()))
                except sqlite3.IntegrityError:
                    raise DomainError("CONFLICT", "Channel name is already in use") from None
                event(cx, "channel.created", channel, channel)
                return view(cx.execute("SELECT * FROM channels WHERE id=?", (channel,)).fetchone())

            result = idempotent(cx, who.agent_id, "create_channel", key, [stored_name, "team"], create)
            accessible_channel(cx, who.agent_id, result["id"])
            return result

    def list_channels(self, who, cursor, limit):
        with self.db.transaction() as cx:
            def read(after, count):
                return cx.execute(f"""
                    SELECT c.* FROM channels c WHERE c.id>:after AND {CHANNEL_VISIBLE} ORDER BY c.id LIMIT :count
                    """, {"agent": who.agent_id, "after": after or "", "count": count}).fetchall()
            return self.page(who, "channels", None, cursor, limit, lambda row: row["id"], read)

    def send_message(self, who, content, key, channel_id, dm_agent_id, reply_to_id, kind, artifacts):
        if sum(value is not None for value in (channel_id, dm_agent_id, reply_to_id)) != 1:
            raise DomainError("VALIDATION", "Select exactly one message destination")
        with self.db.transaction(write=True) as cx:
            require_active_credential(cx, who)
            reply = None
            new_dm = None
            if reply_to_id is not None:
                reply = cx.execute(f"""
                    SELECT m.* FROM messages m JOIN channels c ON c.id=m.channel_id
                    WHERE m.id=:id AND {CHANNEL_VISIBLE}
                    """, {"id": reply_to_id, "agent": who.agent_id}).fetchone()
                if reply is None:
                    raise DomainError("NOT_FOUND", "Message not found")
                channel_id = reply["channel_id"]
            elif dm_agent_id is not None:
                if dm_agent_id == who.agent_id:
                    raise DomainError("VALIDATION", "Select another Agent for a DM")
                first, second = sorted((who.agent_id, dm_agent_id))
                row = cx.execute("SELECT id FROM channels WHERE dm_first=? AND dm_second=?", (first, second)).fetchone()
                if row:
                    channel_id = row["id"]
                else:
                    channel_id = uuid4().hex
                    new_dm = (first, second)
            if reply is None and new_dm is None:
                accessible_channel(cx, who.agent_id, channel_id)

            def send():
                if dm_agent_id is not None:
                    if not cx.execute("""
                        SELECT 1 FROM agents a JOIN users u ON u.id=a.user_id
                        WHERE a.id=? AND u.disabled_at IS NULL
                        """, (dm_agent_id,)).fetchone():
                        raise DomainError("NOT_FOUND", "Agent not found")
                    if new_dm:
                        cx.execute("INSERT INTO channels VALUES (?,?,?,?,?,?,?)",
                                   (channel_id, "DM", "dm", who.agent_id, *new_dm, now()))
                        event(cx, "channel.created", channel_id, channel_id)
                for artifact_id in artifacts:
                    if not cx.execute("SELECT 1 FROM artifacts WHERE id=? AND channel_id=? AND status='ready'",
                                      (artifact_id, channel_id)).fetchone():
                        raise DomainError("NOT_FOUND", "Attachment must be a ready Artifact in this channel")
                root = (reply["thread_root_id"] or reply["id"]) if reply else None
                message_id = cx.execute("""
                    INSERT INTO messages(channel_id,sender_id,type,content,reply_to_id,thread_root_id,created_at)
                    VALUES (?,?,?,?,?,?,?)
                    """, (channel_id, who.agent_id, kind, content, reply_to_id, root, now())).lastrowid
                cx.executemany("INSERT INTO message_artifacts VALUES (?,?)",
                               [(message_id, artifact) for artifact in artifacts])
                event_id = event(cx, "message.created", message_id, channel_id)
                return self.message(cx, cx.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()) | {
                    "event_id": event_id}

            return idempotent(cx, who.agent_id, "send_message", key,
                              [content, channel_id, dm_agent_id, reply_to_id, kind, artifacts], send)

    @staticmethod
    def message(cx, row):
        return view(row) | {"artifact_ids": [a[0] for a in cx.execute(
            "SELECT artifact_id FROM message_artifacts WHERE message_id=? ORDER BY artifact_id", (row["id"],))]}

    def read_messages(self, who, channel, query, thread, cursor, limit):
        with self.db.transaction() as cx:
            accessible_channel(cx, who.agent_id, channel)
            if thread is not None and not cx.execute(
                    "SELECT 1 FROM messages WHERE id=? AND channel_id=? AND thread_root_id IS NULL",
                    (thread, channel)).fetchone():
                raise DomainError("NOT_FOUND", "Thread not found")

            def read(before, count):
                return cx.execute("""
                   SELECT * FROM messages WHERE channel_id=? AND id<? AND (? IS NULL OR id=? OR thread_root_id=?)
                     AND (? IS NULL OR instr(casefold(content), ?) > 0)
                   ORDER BY id DESC LIMIT ?
                   """, (channel, before if before is not None else 2**63 - 1, thread, thread, thread,
                         query, (query or "").casefold(), count)).fetchall()

            return self.page(who, "messages", [channel, query, thread], cursor, limit, lambda row: row["id"],
                             read, lambda row: self.message(cx, row))

    def read_events(self, who, after, epoch, limit):
        with self.db.transaction() as cx:
            require_active_credential(cx, who)
            generation = cx.execute("SELECT value FROM settings WHERE key='event_epoch'").fetchone()[0]
            water = cx.execute("SELECT COALESCE(max(id),0) FROM events").fetchone()[0]
            if epoch is None:
                valid = after == 0
            else:
                expected = self.security.event_checkpoint(who.agent_id, generation, after)
                valid = epoch.isascii() and hmac.compare_digest(epoch, expected)
            if not valid or after > water:
                raise DomainError("CURSOR_RESET", "Restart event synchronization from event 0")
            rows = cx.execute(f"""
                SELECT e.id,e.kind,e.entity_id,e.channel_id,e.data,e.created_at
                FROM events e LEFT JOIN channels c ON c.id=e.channel_id
                WHERE e.id>:after AND e.id<=:water AND
                  ((e.user_id=:user) OR (e.user_id IS NULL AND c.id IS NOT NULL AND {CHANNEL_VISIBLE}))
                ORDER BY e.id LIMIT :count
                """, {"after": after, "water": water, "user": who.user_id, "agent": who.agent_id,
                      "count": limit + 1}).fetchall()
            more = len(rows) > limit
            next_id = rows[limit - 1]["id"] if more else water
            checkpoint = self.security.event_checkpoint(who.agent_id, generation, next_id)
            return {"events": [view(row) | {"data": json.loads(row["data"])} for row in rows[:limit]],
                    "next_event_id": next_id, "event_epoch": checkpoint, "has_more": more}

    async def wait_events(self, who, after, epoch, wait_ms, limit):
        if wait_ms == 0:
            return await asyncio.to_thread(self.read_events, who, after, epoch, limit)
        if who.agent_id in self.waiters or len(self.waiters) >= 32:
            raise DomainError("RATE_LIMITED", "Event wait capacity reached", retryable=True, retry_after_ms=1000)
        self.waiters.add(who.agent_id)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_ms / 1000
            while True:
                page = await asyncio.to_thread(self.read_events, who, after, epoch, limit)
                remaining = deadline - loop.time()
                if page["events"] or remaining <= 0:
                    return page
                after, epoch = page["next_event_id"], page["event_epoch"]
                await asyncio.sleep(min(0.25, remaining))
        finally:
            self.waiters.discard(who.agent_id)

    @staticmethod
    def state_item(row):
        return view(row) | {"deleted": bool(row["deleted"]), "value": json.loads(row["value"])
                            if row["value"] is not None else None}

    def get_state(self, who, channel, key):
        with self.db.transaction() as cx:
            accessible_channel(cx, who.agent_id, channel)
            row = cx.execute("SELECT * FROM channel_state WHERE channel_id=? AND key=?", (channel, key)).fetchone()
            return self.state_item(row) if row else {"key": key, "value": None, "deleted": True, "revision": 0}

    def list_state(self, who, channel, prefix, cursor, limit):
        with self.db.transaction() as cx:
            accessible_channel(cx, who.agent_id, channel)

            def read(after, count):
                return cx.execute("""
                    SELECT * FROM channel_state WHERE channel_id=? AND deleted=0 AND key>? AND substr(key,1,?)=?
                    ORDER BY key LIMIT ?
                    """, (channel, after or "", len(prefix), prefix, count)).fetchall()

            return self.page(who, "state", [channel, prefix], cursor, limit, lambda row: row["key"],
                             read, self.state_item)

    def write_state(self, who, channel, key, value, revision, request_key, deleted=False):
        text = None if deleted else canonical(value)
        if text is not None and len(text.encode()) > 16 * 1024:
            raise DomainError("VALIDATION", "State value exceeds 16 KiB")
        tool = "delete_state" if deleted else "set_state"
        with self.db.transaction(write=True) as cx:
            accessible_channel(cx, who.agent_id, channel)

            def mutate():
                current = cx.execute("SELECT revision,deleted FROM channel_state WHERE channel_id=? AND key=?",
                                     (channel, key)).fetchone()
                if revision != (current["revision"] if current else 0):
                    raise DomainError("CONFLICT", "State revision changed")
                if deleted and (not current or current["deleted"]):
                    raise DomainError("NOT_FOUND", "State key is already absent")
                cx.execute("""
                    INSERT INTO channel_state VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(channel_id,key) DO UPDATE SET value=excluded.value,revision=excluded.revision,
                    deleted=excluded.deleted,updated_by=excluded.updated_by,updated_at=excluded.updated_at
                    """, (channel, key, text, revision + 1, int(deleted), who.agent_id, now()))
                event(cx, "state.deleted" if deleted else "state.changed", key, channel, data={"revision": revision + 1})
                return {"key": key, "value": value, "deleted": deleted, "revision": revision + 1}

            return idempotent(cx, who.agent_id, tool, request_key, [channel, key, value, revision], mutate)
