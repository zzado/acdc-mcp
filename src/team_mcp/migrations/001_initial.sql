CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, role TEXT NOT NULL CHECK(role IN ('admin','member')),
    disabled_at INTEGER
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users, name TEXT, description TEXT, runtime TEXT,
    last_seen_at INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents, token_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL, revoked_at INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('team','private','dm')),
    creator_id TEXT NOT NULL REFERENCES agents, dm_first TEXT REFERENCES agents, dm_second TEXT REFERENCES agents,
    created_at INTEGER NOT NULL, UNIQUE(dm_first,dm_second),
    CHECK((kind='dm' AND dm_first IS NOT NULL AND dm_second IS NOT NULL AND dm_first < dm_second)
       OR (kind<>'dm' AND dm_first IS NULL AND dm_second IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS channels_named ON channels(name COLLATE NOCASE) WHERE kind <> 'dm';
CREATE TABLE IF NOT EXISTS channel_members (
    channel_id TEXT NOT NULL REFERENCES channels, user_id TEXT NOT NULL REFERENCES users,
    PRIMARY KEY(channel_id,user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL REFERENCES channels,
    sender_id TEXT NOT NULL REFERENCES agents, type TEXT NOT NULL, content TEXT NOT NULL,
    reply_to_id INTEGER REFERENCES messages, thread_root_id INTEGER REFERENCES messages, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel_id,id);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_root_id,id);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels, uploader_id TEXT NOT NULL REFERENCES agents,
    filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL CHECK(size >= 0),
    claimed_sha256 TEXT NOT NULL, verified_sha256 TEXT, object_key TEXT UNIQUE,
    description TEXT NOT NULL, tags TEXT NOT NULL, parent_id TEXT REFERENCES artifacts,
    status TEXT NOT NULL CHECK(status IN ('pending','ready','failed','aborted')),
    created_at INTEGER NOT NULL, finalized_at INTEGER
);
CREATE INDEX IF NOT EXISTS artifacts_channel ON artifacts(channel_id,created_at,id);
CREATE TABLE IF NOT EXISTS message_artifacts (
    message_id INTEGER NOT NULL REFERENCES messages, artifact_id TEXT NOT NULL REFERENCES artifacts,
    PRIMARY KEY(message_id,artifact_id)
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts, staging_key TEXT NOT NULL UNIQUE,
    garage_id TEXT, state TEXT NOT NULL CHECK(state IN ('allocating','open','sealing','verifying','ready','aborting','aborted','failed')),
    part_size INTEGER, expires_at INTEGER NOT NULL, reserved INTEGER NOT NULL, admitted_bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT, staged_etag TEXT, parts TEXT, next_run INTEGER NOT NULL DEFAULT 0, cleanup_due INTEGER,
    last_put_at INTEGER, failure_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS uploads_work ON uploads(state,next_run);
CREATE TABLE IF NOT EXISTS upload_grants (
    fingerprint TEXT PRIMARY KEY, upload_id TEXT NOT NULL REFERENCES uploads,
    credential_id TEXT NOT NULL REFERENCES credentials, expected_bytes INTEGER NOT NULL,
    expires_at INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS grants_expiry ON upload_grants(expires_at);
CREATE TABLE IF NOT EXISTS finalization_attempts (
    id TEXT PRIMARY KEY, upload_id TEXT NOT NULL REFERENCES uploads, object_key TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('copying','chosen','orphan','deleted'))
);
CREATE INDEX IF NOT EXISTS attempts_upload ON finalization_attempts(upload_id,status);
CREATE TABLE IF NOT EXISTS channel_state (
    channel_id TEXT NOT NULL REFERENCES channels, key TEXT NOT NULL, value TEXT,
    revision INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT NOT NULL REFERENCES agents, updated_at INTEGER NOT NULL, PRIMARY KEY(channel_id,key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
    channel_id TEXT REFERENCES channels, user_id TEXT REFERENCES users,
    data TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    agent_id TEXT NOT NULL REFERENCES agents, tool TEXT NOT NULL, key TEXT NOT NULL,
    request_hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(agent_id,tool,key)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, actor_id TEXT,
    entity_id TEXT, outcome TEXT NOT NULL, created_at INTEGER NOT NULL
);
