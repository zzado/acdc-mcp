from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from team_mcp.collaboration import Collaboration
from team_mcp.db import Database, DomainError, now
from team_mcp.security import Security


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
            cx.execute("INSERT INTO agents(id,user_id,name,created_at) VALUES (?,?,?,?)", (agent, user, name, now()))
            tokens.append(sec.issue(cx, agent, now() + 60))
    return Collaboration(db, sec), sec, [sec.authenticate(token) for token in tokens], tokens


def test_messages_concurrency_and_idempotency(team):
    collab, _, (first, second, _), _ = team
    channel = collab.create_channel(first, "Round 1", "team", "channel")["id"]

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


def test_acl_dm_and_incremental_events(team):
    collab, sec, (first, second, third), tokens = team
    private = collab.create_channel(first, "Private", "private", "private")["id"]
    direct = collab.send_message(first, "Direct", "dm", None, second.agent_id, None, "chat", [])
    team_channel = collab.create_channel(first, "Public", "team", "public")["id"]
    start = collab.read_events(third, 0, None, 50)
    assert {e["channel_id"] for e in start["events"]} == {team_channel}
    public = collab.send_message(first, "Public", "next", team_channel, None, None, "chat", [])
    changes = collab.read_events(third, start["next_event_id"], start["event_epoch"], 1)
    assert [e["id"] for e in changes["events"]] == [public["event_id"]]
    assert not changes["has_more"]
    assert len(collab.read_messages(second, direct["channel_id"], None, None, None, 20)["items"]) == 1
    for channel in (private, direct["channel_id"]):
        with pytest.raises(DomainError, match="not found"):
            collab.read_messages(third, channel, None, None, None, 20)
    with sec.db.transaction(write=True) as cx:
        cx.execute("UPDATE credentials SET revoked_at=? WHERE id=?", (now(), second.credential_id))
    assert sec.authenticate(tokens[1]) is None


def test_shared_state_cas_and_recreate(team):
    collab, _, (first, second, _), _ = team
    channel = collab.create_channel(first, "State", "team", "state")["id"]
    assert collab.get_state(first, channel, "round")["revision"] == 0
    new = collab.write_state(first, channel, "round", {"number": 1}, 0, "one")
    assert new["revision"] == 1
    assert collab.write_state(first, channel, "round", {"number": 1}, 0, "one") == new
    with pytest.raises(DomainError, match="revision changed"):
        collab.write_state(second, channel, "round", {"number": 2}, 0, "two")
    assert collab.write_state(second, channel, "round", None, 1, "delete", deleted=True)["revision"] == 2
    assert collab.get_state(first, channel, "round")["deleted"]
    assert collab.write_state(first, channel, "round", {"number": 3}, 2, "three")["revision"] == 3
