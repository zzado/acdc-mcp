import hashlib
import os
from uuid import uuid4

import pytest

from team_mcp.app import docker_key
from team_mcp.collaboration import Collaboration
from team_mcp.db import Database, now
from team_mcp.security import RateLimits, Security
from team_mcp.settings import Settings
from team_mcp.storage import Storage, s3_client

pytestmark = pytest.mark.integration


def test_lost_copy_response_recovers_same_immutable_object(tmp_path, monkeypatch):
    if os.getenv("TEAM_TEST_INTERNAL") != "1":
        pytest.skip("Run in the Compose application container with TEAM_TEST_INTERNAL=1")
    from botocore.exceptions import ReadTimeoutError

    cfg = Settings.from_env()
    db = Database(tmp_path / "recovery.sqlite")
    db.migrate()
    security = Security(db, "synthetic-recovery-test-pepper" * 2)
    collab = Collaboration(db, security)
    internal = s3_client(cfg.s3_internal_url, cfg.s3_region, *docker_key("worker"))
    storage = Storage(cfg, db, collab, RateLimits(), None, None, internal)
    data = b"verify recovery after Garage successfully copied a private object"
    user, agent = uuid4().hex, uuid4().hex
    with db.transaction(write=True) as cx:
        cx.execute("INSERT INTO users VALUES (?,?,'member',NULL)", (user, "recovery-test"))
        cx.execute("INSERT INTO agents(id,user_id,name,created_at) VALUES (?,?,?,?)",
                   (agent, user, "recovery-test", now()))
        token = security.issue(cx, agent, now() + 120)
    who = security.authenticate(token)
    channel = collab.create_channel(who, "recovery", "team", "create")["id"]
    upload = storage.request_upload(who, channel, "recovery.bin", len(data), hashlib.sha256(data).hexdigest(),
                                    "upload", "application/octet-stream", "", [], None)["upload_id"]
    with db.transaction() as cx:
        staged = storage.upload(cx, upload)["staging_key"]
    internal.put_object(Bucket=cfg.staging_bucket, Key=staged, Body=data)
    copy_real = internal.copy_object
    copies = []

    def lost_response(**arguments):
        copy_real(**arguments)
        copies.append(arguments["Key"])
        raise ReadTimeoutError(endpoint_url=cfg.s3_internal_url)

    monkeypatch.setattr(internal, "copy_object", lost_response)
    try:
        storage.complete_upload(who, upload)
        assert storage.tick()
        assert storage.tick()
        assert len(copies) == 1 and storage.head(cfg.final_bucket, copies[0]) is not None
        with db.transaction(write=True) as cx:
            assert storage.upload(cx, upload)["state"] == "verifying"
            cx.execute("UPDATE uploads SET next_run=0 WHERE id=?", (upload,))
        recovered = Storage(cfg, db, collab, RateLimits(), None, None, internal)
        assert recovered.tick()
        status = recovered.get_upload_status(who, upload, 0, 20)
        assert status["state"] == "ready" and status["verified_sha256"] == hashlib.sha256(data).hexdigest()
        assert len(copies) == 1
        assert recovered.complete_upload(who, upload)["state"] == "ready"
        events = collab.read_events(who, 0, None, 50)["events"]
        assert [event["kind"] for event in events].count("artifact.ready") == 1
    finally:
        internal.delete_object(Bucket=cfg.staging_bucket, Key=staged)
        for key in copies:
            internal.delete_object(Bucket=cfg.final_bucket, Key=key)
