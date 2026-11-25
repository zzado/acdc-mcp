import asyncio
import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastmcp import Client, FastMCP
from pydantic import TypeAdapter

from team_mcp import db as db_module, tools
from team_mcp.collaboration import Collaboration
from team_mcp.db import Database, DomainError, event, now
from team_mcp.security import RateLimits, Security
from team_mcp.storage import Storage
from team_mcp.tools import Cursor, StateKey


@pytest.fixture
def team(tmp_path):
    db = Database(tmp_path / "team.sqlite")
    db.migrate()
    sec = Security(db, "a" * 64)
    tokens = []
    with db.transaction(write=True) as cx:
        for name in ("first", "second", "third"):
            user, agent = uuid4().hex, uuid4().hex
            cx.execute("INSERT INTO users VALUES (?,?,'member',NULL)", (user, name))
            profile = name if name != "third" else None
            cx.execute("INSERT INTO agents(id,user_id,name,created_at) VALUES (?,?,?,?)",
                       (agent, user, profile, now()))
            tokens.append(sec.issue(cx, agent, now() + 60))
    return Collaboration(db, sec), sec, [sec.authenticate(token) for token in tokens], tokens


def test_messages_concurrency_and_idempotency(team):
    collab, _, (first, second, _), _ = team
    channel = collab.create_channel(first, "Round 1", "channel")["id"]

    def send(index):
        return collab.send_message(first if index % 2 else second, f"발견_%{index}", str(index),
                                   channel, None, None, "finding", [])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send, range(20)))
    assert len({m["id"] for m in results}) == 20
    assert send(5) == results[5]
    reply = collab.send_message(second, "verified", "reply", None, None, results[5]["id"], "review_result", [])
    assert reply["thread_root_id"] == results[5]["id"]
    assert len(collab.read_messages(first, channel, None, reply["thread_root_id"], None, 20)["items"]) == 2
    page = collab.read_messages(first, channel, "발견_%", None, None, 10)
    assert len(page["items"]) == 10 and page["next_cursor"]
    assert len(collab.read_messages(first, channel, "발견_%", None, page["next_cursor"], 50)["items"]) == 10
    with pytest.raises(DomainError, match="different query"):
        collab.read_messages(second, channel, "발견_%", None, page["next_cursor"], 50)


def test_message_search_unicode_and_literal_characters(team):
    collab, _, (writer, reader, _), _ = team
    channel = collab.create_channel(writer, "Search", "search")["id"]
    for key, content in enumerate(("Ünïcödé: Straße_%!", "strasseABC!", "other")):
        collab.send_message(writer, content, str(key), channel, None, None, "chat", [])

    for query in ("ünïcödé", "STRASSE_%!", "_%!"):
        rows = collab.read_messages(reader, channel, query, None, None, 20)["items"]
        assert [row["content"] for row in rows] == ["Ünïcödé: Straße_%!"]
    assert collab.read_messages(reader, channel, "unicode", None, None, 20)["items"] == []


def test_team_channels_dm_and_incremental_events(team):
    collab, sec, (first, second, third), tokens = team
    first_channel = collab.create_channel(first, "Problem A", "problem-a")["id"]
    second_channel = collab.create_channel(third, "Problem B", "problem-b")["id"]
    direct = collab.send_message(first, "Direct", "dm", None, second.agent_id, None, "chat", [])
    assert {c["id"] for c in collab.list_channels(third, None, 50)["items"]} == {first_channel, second_channel}
    assert collab.list_agents(first, third.agent_id, None, 20)["items"][0]["name"] == "third"
    start = collab.read_events(third, 0, None, 50)
    assert {e["channel_id"] for e in start["events"]} == {first_channel, second_channel}
    public = collab.send_message(third, "Public", "next", first_channel, None, None, "chat", [])
    changes = collab.read_events(third, start["next_event_id"], start["event_epoch"], 1)
    assert [e["id"] for e in changes["events"]] == [public["event_id"]]
    assert not changes["has_more"]
    assert len(collab.read_messages(second, direct["channel_id"], None, None, None, 20)["items"]) == 1
    with pytest.raises(DomainError, match="not found"):
        collab.read_messages(third, direct["channel_id"], None, None, None, 20)
    with sec.db.transaction(write=True) as cx:
        cx.execute("UPDATE credentials SET revoked_at=? WHERE id=?", (now(), second.credential_id))
        cx.execute("UPDATE users SET disabled_at=? WHERE id=?", (now(), third.user_id))
    assert sec.authenticate(tokens[1]) is None
    assert sec.authenticate(tokens[2]) is None


def test_shared_state_cas_and_recreate(team):
    collab, _, (first, second, _), _ = team
    channel = collab.create_channel(first, "State", "state")["id"]
    assert collab.get_state(first, channel, "round")["revision"] == 0
    new = collab.write_state(first, channel, "round", {"number": 1}, 0, "one")
    assert new["revision"] == 1
    assert collab.write_state(first, channel, "round", {"number": 1}, 0, "one") == new
    with pytest.raises(DomainError, match="revision changed"):
        collab.write_state(second, channel, "round", {"number": 2}, 0, "two")
    assert collab.write_state(second, channel, "round", None, 1, "delete", deleted=True)["revision"] == 2
    assert collab.get_state(first, channel, "round")["deleted"]
    assert collab.write_state(first, channel, "round", {"number": 3}, 2, "three")["revision"] == 3


@pytest.mark.parametrize("letter", ["한", "𐐀"])
def test_state_cursor_round_trips_maximum_unicode_keys(team, letter):
    collab, _, (writer, reader, _), _ = team
    channel = collab.create_channel(writer, "State", "channel")["id"]
    prefix = letter * 127
    keys = [TypeAdapter(StateKey).validate_python(prefix + suffix) for suffix in ("a", "b")]
    for key, request in zip(keys, ("first", "second")):
        collab.write_state(writer, channel, key, 1, 0, request)
    first = collab.list_state(reader, channel, prefix, None, 1)
    cursor = TypeAdapter(Cursor).validate_python(first["next_cursor"])
    second = collab.list_state(reader, channel, prefix, cursor, 1)
    assert [item["key"] for item in first["items"] + second["items"]] == keys
    assert second["next_cursor"] is None
    with pytest.raises(DomainError, match="different query"):
        collab.list_state(reader, channel, "", cursor, 1)


def test_non_ascii_cursor_is_validation_error(team, monkeypatch):
    collab, security, _, tokens = team
    mcp = FastMCP("cursor test")
    tools.install_tools(mcp, collab, None, security, RateLimits())
    monkeypatch.setattr(tools, "get_access_token", lambda: SimpleNamespace(token=tokens[0]))

    async def read_page():
        async with Client(mcp) as client:
            return await client.call_tool("list_channels", {"cursor": "abc.한"}, raise_on_error=False)

    result = asyncio.run(read_page())
    assert result.is_error
    assert result.structured_content["error"]["code"] == "VALIDATION"
    assert result.structured_content["error"]["retryable"] is False


def test_pre_upgrade_cursor_keeps_query_binding(team):
    _, security, (reader, other, _), _ = team
    binding = [reader.agent_id, "channels", None]
    payload = base64.urlsafe_b64encode(db_module.canonical([binding, "last"]).encode()).decode().rstrip("=")
    legacy = f"{payload}.{security.sign(payload.encode())}"
    assert security.position(legacy, binding) == "last"
    with pytest.raises(DomainError, match="different query"):
        security.position(legacy, [other.agent_id, "channels", None])


def test_migration_shares_existing_private_channel_and_preserves_content(tmp_path):
    db = Database(tmp_path / "legacy.sqlite")
    initial = Path(db_module.__file__).with_name("migrations") / "001_initial.sql"
    with sqlite3.connect(db.path) as cx:
        cx.executescript(initial.read_text(encoding="utf-8") + "\nPRAGMA user_version=1;")
    sec = Security(db, "a" * 64)
    collab = Collaboration(db, sec)
    owner, reader, channel, artifact, epoch = (uuid4().hex for _ in range(5))
    with db.transaction(write=True) as cx:
        cx.executemany("INSERT INTO users VALUES (?,?,'member',NULL)", [(owner, "owner"), (reader, "reader")])
        cx.executemany("INSERT INTO agents(id,user_id,created_at) VALUES (?,?,?)",
                       [(owner, owner, now()), (reader, reader, now())])
        token = sec.issue(cx, reader, now() + 60)
        cx.execute("INSERT INTO settings VALUES ('event_epoch',?)", (epoch,))
        cx.execute("INSERT INTO channels VALUES (?,?,'private',?,NULL,NULL,?)", (channel, "Problem A", owner, now()))
        cx.execute("INSERT INTO channel_members VALUES (?,?)", (channel, owner))
        message = cx.execute("""
            INSERT INTO messages(channel_id,sender_id,type,content,created_at) VALUES (?,?,'chat','Earlier notes',?)
            """, (channel, owner, now())).lastrowid
        cx.execute("""
            INSERT INTO artifacts(id,channel_id,uploader_id,filename,content_type,size,claimed_sha256,object_key,
                                  description,tags,status,created_at)
            VALUES (?,?,?,'old.bin','application/octet-stream',5,?,'existing-object','','[]','ready',?)
            """, (artifact, channel, owner, "a" * 64, now()))
        cx.execute("INSERT INTO message_artifacts VALUES (?,?)", (message, artifact))
        cx.execute("INSERT INTO channel_state VALUES (?,'round','1',1,0,?,?)", (channel, owner, now()))
        event(cx, "message.created", message, channel)
    db.migrate()
    db.migrate()
    who = sec.authenticate(token)
    assert [(row["id"], row["kind"]) for row in collab.list_channels(who, None, 20)["items"]] == [(channel, "team")]
    notes = collab.read_messages(who, channel, None, None, None, 20)["items"][0]
    assert notes["content"] == "Earlier notes" and notes["artifact_ids"] == [artifact]
    assert collab.get_state(who, channel, "round")["value"] == 1
    with pytest.raises(DomainError) as reset:
        collab.read_events(who, 1, epoch, 20)
    assert reset.value.body["code"] == "CURSOR_RESET"
    assert collab.read_events(who, 0, None, 20)["events"][0]["entity_id"] == str(message)
    with db.transaction() as cx:
        assert Storage.get_ready(cx, who, artifact)["object_key"] == "existing-object"
        assert cx.execute("PRAGMA foreign_key_check").fetchone() is None
        assert not cx.execute("SELECT 1 FROM sqlite_schema WHERE name='channel_members'").fetchone()
