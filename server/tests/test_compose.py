import hashlib
import itertools
import json
import os
import ssl
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration
MIB = 1024**2
REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = ["docker", "compose", "-p", "team-mcp-test", "-f", str(REPO_ROOT / "compose.yaml"),
        "-f", str(REPO_ROOT / "server/tests/compose.test.yaml")]


def compose(*arguments):
    return subprocess.run([*BASE, *arguments], cwd=REPO_ROOT, text=True, capture_output=True, check=True).stdout


class Client:
    def __init__(self, token, tls, *, modern=False):
        self.token = token
        self.tls = tls
        self.ids = itertools.count(1)
        self.modern = modern
        self.version = "2026-07-28" if modern else "2025-11-25"
        self.session = None
        if not modern:
            init = self.rpc("initialize", {"protocolVersion": self.version, "capabilities": {},
                                          "clientInfo": {"name": "team-mcp integration", "version": "1"}})
            assert init["protocolVersion"] == self.version
            self.rpc("notifications/initialized", {}, notification=True)

    def rpc(self, method, params, *, notification=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if method != "initialize":
            headers["MCP-Protocol-Version"] = self.version
        if self.modern:
            headers["Mcp-Method"] = method
            if method == "tools/call":
                headers["Mcp-Name"] = params["name"]
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": self.version,
                               "io.modelcontextprotocol/clientInfo": {"name": "team-mcp integration", "version": "1"},
                               "io.modelcontextprotocol/clientCapabilities": {}}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if not notification:
            message["id"] = next(self.ids)
        request = Request("https://localhost/mcp", data=json.dumps(message).encode(), headers=headers)
        with urlopen(request, context=self.tls, timeout=20) as response:
            self.session = response.headers.get("Mcp-Session-Id") or self.session
            if notification:
                assert response.status == 202
                return None
            body = json.load(response)
            assert "error" not in body, body.get("error")
            return body["result"]

    def tool(self, name, /, *, error=None, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        structured = result["structuredContent"]
        if error:
            assert result.get("isError") and structured["error"]["code"] == error
        else:
            assert not result.get("isError"), structured.get("error")
        return structured


@pytest.fixture(scope="module")
def team():
    if os.getenv("TEAM_TEST_COMPOSE") != "1":
        pytest.skip("Set TEAM_TEST_COMPOSE=1 after starting the test stack")
    tls = ssl.create_default_context(cafile=REPO_ROOT / "test-data/root.crt")
    clients = []
    credentials = []
    for name in ("writer", "reader", "teammate"):
        user = compose("exec", "-T", "team-mcp", "team-mcp", "user-add", f"{name}-{uuid4().hex[:8]}")
        agent = compose("exec", "-T", "team-mcp", "team-mcp", "agent-add", json.loads(user)["user_id"])
        credential = json.loads(agent)
        client = Client(credential["token"], tls)
        clients.append(client)
        credentials.append(credential)
    return *clients, credentials, tls


def wait_for(client, upload_id, state, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.tool("get_upload_status", upload_id=upload_id)
        if result["state"] == state:
            return result
        assert result["state"] not in {"failed", "aborted"}, result
        time.sleep(0.55)
    pytest.fail(f"Upload did not enter {state}")


def put(tls, grant, data, attempts=6):
    request = Request(grant["url"], data=data, method="PUT", headers=grant["required_headers"])
    for attempt in range(attempts):
        try:
            with urlopen(request, context=tls, timeout=90) as response:
                return response.status
        except HTTPError as error:
            if error.code != 429 or attempt == attempts - 1:
                return error.code
            time.sleep(0.55)


def reserve(client, channel, filename, payload, *, sha=None):
    content_hash = sha or hashlib.sha256(payload).hexdigest()
    return client.tool("request_upload", channel_id=channel, filename=filename, size=len(payload),
                       sha256=content_hash, idempotency_key=uuid4().hex)


def channel(client):
    return client.tool("create_channel", name=uuid4().hex, idempotency_key=uuid4().hex)["id"]


def test_messages_auth_and_events(team):
    writer, reader, teammate, credentials, tls = team
    modern = Client(credentials[0]["token"], tls, modern=True)
    assert len(modern.rpc("tools/list", {})["tools"]) == 19
    assert modern.tool("list_agents")["items"]
    created = writer.tool("create_channel", name=uuid4().hex, idempotency_key=uuid4().hex, visibility="team")
    assert created["kind"] == "team"
    shared = created["id"]
    assert shared in {item["id"] for item in teammate.tool("list_channels")["items"]}
    direct = writer.tool("send_message", dm_agent_id=credentials[1]["agent_id"],
                         content="Direct message", idempotency_key=uuid4().hex)
    assert reader.tool("read_messages", channel_id=direct["channel_id"])["items"][0]["content"] == "Direct message"
    teammate.tool("read_messages", channel_id=direct["channel_id"], error="NOT_FOUND")
    checkpoint = reader.tool("read_events")

    def send(index):
        client = reader if index % 2 else writer
        return client.tool("send_message", channel_id=shared, content=f"Collaboration {index}",
                           idempotency_key=str(index) + shared)

    with ThreadPoolExecutor(max_workers=4) as executor:
        messages = list(executor.map(send, range(12)))
    assert len({message["id"] for message in messages}) == 12
    assert messages[1] == send(1)
    with pytest.raises(HTTPError) as rejected:
        Client("not-a-token", tls)
    assert rejected.value.code == 401
    reconnect = Client(credentials[1]["token"], tls)
    new = reconnect.tool("read_events", after_event_id=checkpoint["next_event_id"],
                         event_epoch=checkpoint["event_epoch"], wait_ms=100)
    assert {int(e["entity_id"]) for e in new["events"] if e["channel_id"] == shared} == {m["id"] for m in messages}


def test_upload_download_and_immutability(team):
    writer, reader, teammate, _, tls = team
    shared = channel(writer)
    payload = b"team verification\n" * 1024
    pending = reserve(writer, shared, "candidate.bin", payload)
    artifact, upload = pending["artifact_id"], pending["upload_id"]
    grant = writer.tool("request_upload_urls", upload_id=upload)["transfers"][0]
    assert put(tls, grant, payload) == 200
    writer.tool("complete_upload", upload_id=upload)
    writer.tool("complete_upload", upload_id=upload)
    ready = wait_for(writer, upload, "ready")
    assert ready["verified_sha256"] == hashlib.sha256(payload).hexdigest()
    assert put(tls, grant, b"\x00" * len(payload)) == 403
    assert teammate.tool("get_artifact", artifact_id=artifact)["verified_sha256"] == ready["verified_sha256"]
    assert [item["id"] for item in teammate.tool("list_artifacts", channel_id=shared)["items"]] == [artifact]
    unrelated = channel(teammate)
    assert teammate.tool("list_artifacts", channel_id=unrelated)["items"] == []
    teammate.tool("send_message", channel_id=unrelated, content="Wrong channel", artifact_ids=[artifact],
                  idempotency_key=uuid4().hex, error="NOT_FOUND")
    download = reader.tool("request_download", artifact_id=artifact)
    with urlopen(download["url"], context=tls) as response:
        assert response.headers["Content-Disposition"] == "attachment"
        assert hashlib.sha256(response.read()).hexdigest() == download["sha256"]
    events = reader.tool("read_events", limit=50)["events"]
    assert sum(event["kind"] == "artifact.ready" and event["entity_id"] == artifact for event in events) == 1
    wrong_digest = reserve(writer, shared, "wrong.bin", payload, sha="0" * 64)
    bad_id = wrong_digest["upload_id"]
    assert put(tls, writer.tool("request_upload_urls", upload_id=bad_id)["transfers"][0], payload) == 200
    writer.tool("complete_upload", upload_id=bad_id)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        bad = writer.tool("get_upload_status", upload_id=bad_id)
        if bad["state"] == "failed":
            break
        time.sleep(0.5)
    assert bad["state"] == "failed" and bad["error"] == "CHECKSUM_MISMATCH"
    reader.tool("get_artifact", artifact_id=wrong_digest["artifact_id"], error="NOT_FOUND")


def test_multipart_resume(team):
    writer, reader, _, _, tls = team
    shared = channel(writer)
    data = b"x" * (64 * MIB) + b"last-part"
    pending = reserve(writer, shared, "parts.bin", data)
    upload = pending["upload_id"]
    assert pending["mode"] == "multipart"
    plan = wait_for(writer, upload, "open")
    assert plan["part_count"] == 2
    first, last = writer.tool("request_upload_urls", upload_id=upload, part_numbers=[1, 2])["transfers"]
    assert put(tls, first, data[:first["length"]]) == 200
    writer.tool("complete_upload", upload_id=upload)
    status = wait_for(writer, upload, "open")
    assert status["error"] == "INCOMPLETE_PARTS"
    assert [part["number"] for part in status["parts"]] == [1]
    assert put(tls, last, data[-last["length"]:]) == 200
    writer.tool("complete_upload", upload_id=upload)
    wait_for(writer, upload, "ready")
    download = reader.tool("request_download", artifact_id=pending["artifact_id"])
    with urlopen(download["url"], context=tls) as response:
        assert hashlib.file_digest(response, "sha256").hexdigest() == download["sha256"]


def test_restart_and_cleanup(team):
    writer, reader, _, credentials, tls = team
    shared = channel(writer)
    pending = reserve(writer, shared, "cancel.bin", b"cancel")
    upload = pending["upload_id"]
    grant = writer.tool("request_upload_urls", upload_id=upload)["transfers"][0]
    assert put(tls, grant, b"cancel") == 200
    writer.tool("abort_upload", upload_id=upload)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if writer.tool("get_upload_status", upload_id=upload)["state"] == "aborted":
            break
        time.sleep(0.5)
    else:
        pytest.fail("Upload did not abort")
    assert put(tls, grant, b"cancel") == 403
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        cleaned = compose("exec", "-T", "team-mcp", "python", "-c", """
import sqlite3, sys
with sqlite3.connect('/var/lib/team-mcp/team.sqlite') as cx:
    print(cx.execute('SELECT cleanup_due IS NULL FROM uploads WHERE id=?', (sys.argv[1],)).fetchone()[0])
""", upload)
        if cleaned.strip() == "1":
            break
        time.sleep(0.5)
    else:
        pytest.fail("Cancelled upload still awaiting cleanup")
    payload = b"stored before restart"
    kept = reserve(writer, shared, "kept.bin", payload)
    grant = writer.tool("request_upload_urls", upload_id=kept["upload_id"])["transfers"][0]
    assert put(tls, grant, payload) == 200
    writer.tool("complete_upload", upload_id=kept["upload_id"])
    compose("kill", "-s", "SIGKILL", "team-mcp")
    compose("up", "-d", "--wait", "team-mcp")
    # The proxy can briefly retain an upstream connection from before the restart.
    for _ in range(10):
        try:
            writer = Client(credentials[0]["token"], tls)
            break
        except (URLError, ConnectionError):
            time.sleep(0.5)
    else:
        pytest.fail("Could not reconnect to the restarted MCP server")
    wait_for(writer, kept["upload_id"], "ready")
    download = reader.tool("request_download", artifact_id=kept["artifact_id"])
    with urlopen(download["url"], context=tls) as response:
        assert response.read() == payload


def test_parallel_uploads_size_and_expiry(team):
    writer, reader, _, _, tls = team
    shared = channel(writer)

    def upload_small(client, data):
        pending = reserve(client, shared, "same-name.bin", data)
        upload = pending["upload_id"]
        grant = client.tool("request_upload_urls", upload_id=upload)["transfers"][0]
        invalid = grant | {"required_headers": {"Content-Length": str(len(data) + 1)}}
        assert put(tls, invalid, data + b"x") == 400
        assert put(tls, grant, data) == 200
        client.tool("complete_upload", upload_id=upload)
        wait_for(client, upload, "ready")
        return pending["artifact_id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(upload_small, writer, b"first")
        two = executor.submit(upload_small, reader, b"second")
        assert one.result() != two.result()
    grant = reader.tool("request_download", artifact_id=one.result())
    remaining = datetime.fromisoformat(grant["expires_at"]).timestamp() - time.time()
    if remaining <= 20:
        time.sleep(max(0, remaining) + 1)
        with pytest.raises(HTTPError) as expired:
            urlopen(grant["url"], context=tls).close()
        assert expired.value.code == 400
        assert b"Date is too old" in expired.value.read()


@pytest.mark.large
def test_large_transfers(team):
    if os.getenv("TEAM_TEST_LARGE") != "1":
        pytest.skip("Set TEAM_TEST_LARGE=1 for GiB-size transfers")
    writer, reader, _, _, tls = team
    shared = channel(writer)
    block = b"team-mcp large transfer\n" * (MIB // 24)
    chunk = (block * (64 * MIB // len(block) + 1))[:64 * MIB]
    archives = [1, 5]
    if os.getenv("TEAM_TEST_MAXIMUM") == "1":
        archives.append(50)
    measurements = []
    for gib in archives:
        parts = gib * 16
        sha256 = hashlib.sha256()
        for _ in range(parts):
            sha256.update(chunk)
        compose("exec", "-T", "team-mcp", "python", "-c", "open('/proc/1/clear_refs','w').write('5')")
        pending = writer.tool("request_upload", channel_id=shared, filename=f"{gib}GiB.bin",
                              size=parts * len(chunk), sha256=sha256.hexdigest(), idempotency_key=uuid4().hex)
        upload = pending["upload_id"]
        wait_for(writer, upload, "open")
        with ThreadPoolExecutor(max_workers=4) as executor:
            for start in range(1, parts + 1, 8):
                batch = list(range(start, min(start + 8, parts + 1)))
                grants = writer.tool("request_upload_urls", upload_id=upload, part_numbers=batch)["transfers"]
                for grant in grants:
                    assert grant["length"] == len(chunk)
                assert list(executor.map(lambda grant: put(tls, grant, chunk), grants)) == [200] * len(grants)
                if start % 160 == 153:
                    print(f"{gib} GiB test uploaded {start + 7} of {parts} Parts", flush=True)
        writer.tool("complete_upload", upload_id=upload)
        wait_for(writer, upload, "ready", timeout=600)
        download = reader.tool("request_download", artifact_id=pending["artifact_id"])
        with urlopen(download["url"], context=tls, timeout=120) as response:
            assert hashlib.file_digest(response, "sha256").hexdigest() == sha256.hexdigest()
        peak = compose("exec", "-T", "team-mcp", "python", "-c",
                       "print(next(i.split()[1] for i in open('/proc/1/status') if i.startswith('VmHWM:')))")
        measurements.append(int(peak) * 1024)
        print(f"Verified {gib} GiB with Team MCP peak RSS {measurements[-1] // MIB} MiB", flush=True)
    assert max(measurements) - min(measurements) <= 64 * MIB
