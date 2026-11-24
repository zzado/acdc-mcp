# Verification

The test stack binds HTTPS to loopback TCP 443. It permits Docker Desktop's
world-readable mount mode for ephemeral Garage test credentials. Production
uses the restrictive secret file permissions.

```powershell
python deploy/create-secrets.py test-data/secrets
$env:TEAM_SECRETS_DIR = './test-data/secrets'
$env:TEAM_MCP_HOST = 'localhost'
$env:TEAM_S3_HOST = '127.0.0.1'
$env:TEAM_TLS_DIRECTIVE = 'tls internal'
$env:TEAM_TRANSFER_GRACE = '3'
$env:TEAM_GET_TTL = '15'
$env:TEAM_UV_FLAGS = '--dev'
docker compose -p team-mcp-test -f compose.yaml -f tests/compose.test.yaml up -d --build --wait
docker compose -p team-mcp-test -f compose.yaml -f tests/compose.test.yaml cp reverse-proxy:/data/caddy/pki/authorities/local/root.crt test-data/root.crt
$env:TEAM_TEST_COMPOSE = '1'
uv run --no-project --with pytest==9.2.0 python -m pytest tests/test_compose.py -m 'not large'
```

Run unit tests in the dev-enabled Team MCP image so they use the pinned
Python and SQLite. Mount the repository read-only at `/src` and run
`python -m pytest -p no:cacheprovider /src/tests/test_collaboration.py`.
Run `test_garage_recovery.py` from the same image on the Compose storage
network with `TEAM_TEST_INTERNAL=1` to exercise a lost Garage copy response.

Use `TEAM_TEST_LARGE=1` and pytest `-m large` for `1 GiB` and `5 GiB`.
`TEAM_TEST_MAXIMUM=1` additionally transfers `50 GiB`. Use the default file
limit, allow enough physical disk space and retain the default GET TTL for
these tests. Set `TEAM_PUT_RATE=12` before starting the loopback stack if you
need to raise the test upload rate. The test compares SHA-256 and reads the
Linux process high-water mark to measure FastMCP memory use across sizes.

The local release check transferred and verified 1, 5 and 50 GiB through
Caddy and Garage 2.6.0. Team MCP peak RSS was 145, 147 and 150 MiB
respectively. The 50 GiB run used 800 Parts. A separate restore drill
verified a SQLite message and the SHA-256 of its Garage Artifact from
restored volumes. Codex CLI, Claude Code and OpenCode connected over the
loopback HTTPS endpoint.

For remote acceptance, use two independently controlled hosts and trusted
HTTPS. Register Codex CLI, Claude Code, OpenCode, Cursor and an Orca-launched
Agent. Record the version and negotiated MCP dialect. Exchange messages and
replies, share and hash a file and resume reading from a saved event cursor.
Run these checks against the same image digests intended for deployment.
