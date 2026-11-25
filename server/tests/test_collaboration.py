import asyncio
import base64
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastmcp import Client, FastMCP
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from team_mcp import db as db_module, tools
from team_mcp.collaboration import Collaboration
from team_mcp.db import Database, DomainError, event, now
from team_mcp.security import RateLimits, Security
from team_mcp.settings import MIB, Settings
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


def test_dm_receipt_after_recipient_disabled(team):
    collab, security, (sender, recipient, outsider), tokens = team

    def send(who=sender, key="send-once", text="receipt"):
        return collab.send_message(who, text, key, None, recipient.agent_id, None, "chat", [])

    receipt = send()
    with security.db.transaction(write=True) as cx:
        cx.execute("UPDATE users SET disabled_at=? WHERE id=?", (now(), recipient.user_id))
    assert send() == receipt
    for attempt, code in ((lambda: send(key="new"), "NOT_FOUND"),
                          (lambda: send(text="changed"), "CONFLICT"),
                          (lambda: send(outsider), "NOT_FOUND")):
        with pytest.raises(DomainError) as failure:
            attempt()
        assert failure.value.body["code"] == code
    with security.db.transaction(write=True) as cx:
        assert cx.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert cx.execute("SELECT count(*) FROM events WHERE kind='message.created'").fetchone()[0] == 1
        assert cx.execute("SELECT count(*) FROM channels WHERE kind='dm'").fetchone()[0] == 1
        cx.execute("UPDATE credentials SET revoked_at=? WHERE id=?", (now(), sender.credential_id))
    assert security.authenticate(tokens[0]) is None
    with pytest.raises(DomainError) as revoked:
        send()
    assert revoked.value.body["code"] == "FORBIDDEN"


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


def test_event_checkpoint_owner_position_and_upgrade(team):
    collab, security, (reader, peer, other), _ = team
    start = collab.read_events(reader, 0, None, 10)
    dm = collab.send_message(peer, "fixture", "dm", None, reader.agent_id, None, "chat", [])
    foreign = collab.read_events(other, 0, None, 10)
    assert foreign["events"] == [] and foreign["next_event_id"] >= dm["event_id"]
    with security.db.transaction() as cx:
        legacy_epoch = cx.execute("SELECT value FROM settings WHERE key='event_epoch'").fetchone()[0]
    for epoch in (foreign["event_epoch"], start["event_epoch"], legacy_epoch, None):
        with pytest.raises(DomainError) as reset:
            asyncio.run(collab.wait_events(reader, foreign["next_event_id"], epoch, 10, 1))
        assert reset.value.body["code"] == "CURSOR_RESET"

    first = collab.read_events(reader, 0, None, 1)
    assert first["has_more"]
    with security.db.transaction(write=True) as cx:
        token = security.issue(cx, reader.agent_id, now() + 60)
        sibling_id = uuid4().hex
        cx.execute("INSERT INTO agents(id,user_id,created_at) VALUES (?,?,?)", (sibling_id, reader.user_id, now()))
        sibling_token = security.issue(cx, sibling_id, now() + 60)
    sibling = security.authenticate(sibling_token)
    assert collab.read_events(sibling, 0, None, 20)["events"] == []
    with pytest.raises(DomainError) as reset:
        collab.read_events(sibling, first["next_event_id"], first["event_epoch"], 1)
    assert reset.value.body["code"] == "CURSOR_RESET"
    resumed = Collaboration(security.db, security).read_events(
        security.authenticate(token), first["next_event_id"], first["event_epoch"], 1)
    assert [row["id"] for row in resumed["events"]] == [dm["event_id"]]
    with security.db.transaction(write=True) as cx:
        cx.execute("UPDATE settings SET value=? WHERE key='event_epoch'", (uuid4().hex,))
    with pytest.raises(DomainError) as reset:
        collab.read_events(reader, resumed["next_event_id"], resumed["event_epoch"], 1)
    assert reset.value.body["code"] == "CURSOR_RESET"
    assert collab.read_events(other, 0, None, 20)["events"] == []


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


@pytest.fixture
def tool_server(team, tmp_path, monkeypatch):
    collab, security, _, tokens = team
    rate = RateLimits()
    storage = Storage(Settings(database=security.db.path, disk_path=tmp_path, disk_floor=0),
                      security.db, collab, rate, None, None, Mock())
    mcp = FastMCP("tool tests")
    tools.install_tools(mcp, collab, storage, security, rate)
    monkeypatch.setattr(tools, "get_access_token", lambda: SimpleNamespace(token=tokens[0]))
    return mcp, storage


def test_non_ascii_cursor_is_validation_error(tool_server):
    mcp, _ = tool_server

    async def read_page():
        async with Client(mcp) as client:
            return await client.call_tool("list_channels", {"cursor": "abc.한"}, raise_on_error=False)

    result = asyncio.run(read_page())
    assert result.is_error
    assert result.structured_content["error"]["code"] == "VALIDATION"
    assert result.structured_content["error"]["retryable"] is False


def test_reply_target_errors_are_indistinguishable(tool_server, team):
    mcp, _ = tool_server
    collab, _, (_, sender, peer), _ = team
    dm = collab.send_message(sender, "fixture", "dm", None, peer.agent_id, None, "chat", [])

    async def check():
        async with Client(mcp) as client:
            errors = []
            for target in (dm["id"], dm["id"] + 1):
                args = {"reply_to_id": target, "content": "reply", "idempotency_key": "reply"}
                result = await client.call_tool("send_message", args, raise_on_error=False)
                assert result.is_error and result.structured_content["error"]["code"] == "NOT_FOUND"
                errors.append((result.structured_content, result.content))
            assert errors[0] == errors[1]

    asyncio.run(check())
    args = (peer, "reply", "reply", None, None, dm["id"], "chat", [])
    reply = collab.send_message(*args)
    assert reply["thread_root_id"] == dm["id"] and collab.send_message(*args) == reply


@pytest.mark.parametrize("tool,name", [
    ("register_agent", "A\0B"), ("register_agent", "A\u202eB"),
    ("create_channel", "A\nB"), ("create_channel", "A\n"),
    ("create_channel", "A\u2028B"), ("register_agent", "A\u2067B"),
])
def test_display_name_rejected_before_write(team, tool_server, tool, name):
    collab, _, (owner, _, _), _ = team
    mcp, _ = tool_server

    async def check():
        async with Client(mcp) as client:
            args = {"name": name}
            if tool == "create_channel":
                args["idempotency_key"] = "display"
            result = await client.call_tool(tool, args, raise_on_error=False)
            assert result.is_error and result.structured_content["error"]["code"] == "VALIDATION"

    asyncio.run(check())
    assert collab.list_agents(owner, owner.agent_id, None, 1)["items"][0]["name"] == "first"
    assert collab.list_channels(owner, None, 20)["items"] == []


def test_unicode_channel_names_keep_existing_uniqueness(team):
    collab, _, (owner, _, _), _ = team
    names = ["Room", "É", "é", "e\u0301", "라운드", "مرحلة"]
    created = [collab.create_channel(owner, name, str(i))["id"] for i, name in enumerate(names)]
    assert len(set(created)) == len(names)
    with pytest.raises(DomainError) as duplicate:
        collab.create_channel(owner, "rOOM", "other")
    assert duplicate.value.body["code"] == "CONFLICT"


def test_legacy_display_names_and_committed_receipt(team):
    collab, security, (owner, _, _), _ = team
    receipt = collab.create_channel(owner, "fixture", "legacy")
    name = "old\nname"
    receipt["name"] = name
    with security.db.transaction(write=True) as cx:
        cx.execute("UPDATE channels SET name=? WHERE id=?", (name, receipt["id"]))
        cx.execute("UPDATE agents SET name=? WHERE id=?", (name, owner.agent_id))
        cx.execute("UPDATE idempotency_keys SET request_hash=?, result=? WHERE agent_id=? AND tool=? AND key=?",
                   (db_module.digest([name, "team"]), db_module.canonical(receipt), owner.agent_id,
                    "create_channel", "legacy"))
    assert collab.list_channels(owner, None, 1)["items"][0]["name"] == name
    assert collab.list_agents(owner, owner.agent_id, None, 1)["items"][0]["name"] == name
    assert collab.create_channel(owner, name, "legacy") == receipt
    with pytest.raises(DomainError) as invalid:
        collab.create_channel(owner, name, "fresh")
    assert invalid.value.body["code"] == "VALIDATION"


@pytest.mark.parametrize("field", ["runtime", "description"])
def test_profile_metadata_rejects_controls_and_preserves_legacy(team, tool_server, field):
    collab, security, (owner, reader, _), _ = team
    mcp, _ = tool_server
    with security.db.transaction(write=True) as cx:
        cx.execute("UPDATE agents SET runtime=?, description=? WHERE id=?",
                   ("old\0runtime", "old\u202edesc", owner.agent_id))
    original = collab.list_agents(reader, owner.agent_id, None, 1)["items"][0]
    assert (original["runtime"], original["description"]) == ("old\0runtime", "old\u202edesc")

    async def check():
        async with Client(mcp) as client:
            tool = next(t for t in await client.list_tools() if t.name == "register_agent")
            inputs = Draft202012Validator(tool.input_schema)
            outputs = Draft202012Validator(tool.output_schema)
            for value in ("A\0B", "A\n", "A\rB", "A\u0085B", "A\u2028B", "A\u202eB", "A\u2067B", " \u3000"):
                args = {"name": "new", field: value}
                result = await client.call_tool(tool.name, args, raise_on_error=False)
                outputs.validate(result.structured_content)
                assert result.is_error and result.structured_content["error"]["code"] == "VALIDATION"
                assert result.structured_content["error"]["retryable"] is False
                assert not inputs.is_valid(args)

    asyncio.run(check())
    saved = collab.list_agents(reader, owner.agent_id, None, 1)["items"][0]
    assert (saved["name"], saved["runtime"], saved["description"]) == (
        original["name"], original["runtime"], original["description"])


def test_profile_metadata_valid_values_and_defaults(team, tool_server):
    collab, _, (owner, reader, _), _ = team
    mcp, _ = tool_server

    async def check():
        async with Client(mcp) as client:
            tool = next(t for t in await client.list_tools() if t.name == "register_agent")
            inputs = Draft202012Validator(tool.input_schema)
            assert set(tool.input_schema["required"]) == {"name"}
            for args in ({"name": "실행자", "runtime": "  Codex CLI  ", "description": " العربية · 한글 "},
                         {"name": "실행자", "runtime": "R" * 80, "description": "한" * 512},
                         {"name": "실행자", "runtime": None, "description": ""}, {"name": "실행자"}):
                assert inputs.is_valid(args)
                result = (await client.call_tool(tool.name, args)).structured_content
                listed = collab.list_agents(reader, owner.agent_id, None, 1)["items"][0]
                runtime = args.get("runtime")
                expected_runtime = runtime.strip() if runtime is not None else None
                assert listed["runtime"] == result["runtime"] == expected_runtime
                assert listed["description"] == result["description"] == args.get("description", "")
            for error in ({"runtime": ""}, {"runtime": "R" * 81}, {"description": "한" * 513}):
                args = {"name": "실행자"} | error
                assert not inputs.is_valid(args)
                result = await client.call_tool(tool.name, args, raise_on_error=False)
                assert result.is_error and result.structured_content["error"]["code"] == "VALIDATION"

    asyncio.run(check())


def test_unicode_state_key_schema_and_storage(tool_server, team):
    mcp, _ = tool_server
    collab, _, (owner, _, _), _ = team
    channel = collab.create_channel(owner, "keys", "room")["id"]

    async def check():
        async with Client(mcp) as client:
            catalog = {t.name: t for t in await client.list_tools()}
            for tool in ("get_state", "set_state", "delete_state"):
                assert r"\w" not in catalog[tool].input_schema["properties"]["key"].get("pattern", "")
            for i, key in enumerate(("라운드.1", "e\u0301", "𐐀-key")):
                target = {"channel_id": channel, "key": key}
                await client.call_tool("set_state", target | {"value": i, "expected_revision": 0,
                                                              "idempotency_key": str(i)})
                result = await client.call_tool("get_state", target)
                assert result.structured_content["value"] == i
            invalid = await client.call_tool("get_state", {"channel_id": channel, "key": "two words"},
                                             raise_on_error=False)
            assert invalid.is_error and invalid.structured_content["error"]["code"] == "VALIDATION"

    asyncio.run(check())


@pytest.mark.parametrize("limit", [True, "1", 1, 1.0, 1.5, 0, 51])
def test_limit_matches_published_schema(tool_server, limit):
    mcp, _ = tool_server

    async def check():
        async with Client(mcp) as client:
            tool = next(tool for tool in await client.list_tools() if tool.name == "list_channels")
            arguments = {"limit": limit}
            valid = Draft202012Validator(tool.input_schema).is_valid(arguments)
            result = await client.call_tool(tool.name, arguments, raise_on_error=False)
            Draft202012Validator(tool.output_schema).validate(result.structured_content)
            assert result.is_error == (not valid)
            if not valid:
                error = result.structured_content["error"]
                assert error["code"] == "VALIDATION" and error["retryable"] is False

    asyncio.run(check())


@pytest.mark.parametrize("transition,code,expected", [
    ("complete_upload", "NoSuchUpload", "sealing"),
    ("abort_upload", "NoSuchUpload", "aborting"),
    (None, "NoSuchUpload", "STORAGE_UNAVAILABLE"),
    ("complete_upload", "ServiceUnavailable", "STORAGE_UNAVAILABLE"),
])
def test_multipart_status_during_transition(team, tool_server, transition, code, expected):
    collab, _, (owner, other, _), _ = team
    mcp, storage = tool_server
    channel = collab.create_channel(owner, "upload", "channel")["id"]
    upload = storage.request_upload(owner, channel, "parts.bin", 64 * MIB + 1, "0" * 64, "reserve",
                                    "application/octet-stream", "", [], None)["upload_id"]
    with storage.db.transaction(write=True) as cx:
        cx.execute("UPDATE uploads SET state='open',garage_id='test-upload' WHERE id=?", (upload,))

    def list_parts(**kwargs):
        if transition:
            getattr(storage, transition)(owner, upload)
        raise ClientError({"Error": {"Code": code}}, "ListParts")

    storage.s3.list_parts.side_effect = list_parts
    with pytest.raises(DomainError) as forbidden:
        storage.get_upload_status(other, upload, 0, 20)
    assert forbidden.value.body["code"] == "NOT_FOUND"
    storage.s3.list_parts.assert_not_called()

    async def check():
        async with Client(mcp) as client:
            tool = next(tool for tool in await client.list_tools() if tool.name == "get_upload_status")
            result = await client.call_tool(tool.name, {"upload_id": upload}, raise_on_error=False)
            data = result.structured_content
            Draft202012Validator(tool.output_schema).validate(data)
            if expected == "STORAGE_UNAVAILABLE":
                assert result.is_error and data["error"]["code"] == expected and data["error"]["retryable"]
            else:
                assert not result.is_error and data["state"] == expected
                assert data["parts"] == [] and data["next_part_cursor"] is None

    asyncio.run(check())
    storage.s3.list_parts.assert_called_once()


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
