import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .db import Database, audit, now
from .security import Security
from .settings import Settings, secret


def parser():
    top = argparse.ArgumentParser(prog="team-mcp")
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    user = commands.add_parser("user-add")
    user.add_argument("name")
    user.add_argument("--role", choices=["admin", "member"], default="member")
    user_off = commands.add_parser("user-disable")
    user_off.add_argument("user_id")
    agent = commands.add_parser("agent-add")
    agent.add_argument("user_id")
    agent.add_argument("--days", type=int, default=30)
    rotate = commands.add_parser("token-issue")
    rotate.add_argument("agent_id")
    rotate.add_argument("--days", type=int, default=30)
    revoke = commands.add_parser("token-revoke")
    revoke.add_argument("credential_id")
    snapshot = commands.add_parser("db-backup")
    snapshot.add_argument("output", type=Path)
    commands.add_parser("reset-event-epoch")
    commands.add_parser("status")
    return top


def main():
    top = parser()
    args = top.parse_args()
    cfg = Settings.from_env()
    if args.command == "serve":
        import logging

        import uvicorn

        from .app import JsonLogs, create_app

        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogs())
        logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
        for name in ("uvicorn", "fastmcp", "mcp"):
            logging.getLogger(name).setLevel(logging.WARNING)
        uvicorn.run(create_app(cfg), host="0.0.0.0", port=8000, log_config=None, access_log=False,
                    forwarded_allow_ips="*", timeout_graceful_shutdown=20, limit_concurrency=128)
        return

    db = Database(cfg.database)
    try:
        if args.command in ("db-backup", "status"):
            with closing(sqlite3.connect(db.path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                if args.command == "db-backup":
                    backup_database(source, args.output)
                    return
                source.row_factory = sqlite3.Row
                source.execute("BEGIN")
                output = run_command(source, db, cfg, args)
        else:
            db.migrate()
            with db.transaction(write=True) as cx:
                output = run_command(cx, db, cfg, args)
    except (ValueError, sqlite3.Error, OSError) as error:
        top.error(str(error))
    print(json.dumps(output, ensure_ascii=False))


def backup_database(source, path):
    try:
        path.touch(mode=0o600, exist_ok=False)
    except FileExistsError:
        raise ValueError("Specify a new backup file") from None
    try:
        with closing(sqlite3.connect(path)) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup failed integrity check")
    except BaseException:
        path.unlink()
        raise


def run_command(cx, db, cfg, args):
    match args.command:
        case "user-add":
            if not args.name.strip() or len(args.name) > 80:
                raise ValueError("Specify a user name of up to 80 characters")
            identifier = uuid4().hex
            cx.execute("INSERT INTO users VALUES (?,?,?,NULL)", (identifier, args.name.strip(), args.role))
            audit(cx, "user.add", entity=identifier)
            return {"user_id": identifier}
        case "user-disable":
            if not cx.execute("UPDATE users SET disabled_at=coalesce(disabled_at,?) WHERE id=?",
                              (now(), args.user_id)).rowcount:
                raise ValueError("Unknown user")
            audit(cx, "user.disable", entity=args.user_id)
            return {"user_id": args.user_id, "disabled": True}
        case "agent-add" | "token-issue":
            if not 1 <= args.days <= 365:
                raise ValueError("Credential lifetime must be between 1 and 365 days")
            if args.command == "agent-add":
                if not cx.execute("SELECT 1 FROM users WHERE id=? AND disabled_at IS NULL", (args.user_id,)).fetchone():
                    raise ValueError("Unknown or disabled user")
                identifier = uuid4().hex
                cx.execute("INSERT INTO agents(id,user_id,created_at) VALUES (?,?,?)", (identifier, args.user_id, now()))
                audit(cx, "agent.add", entity=identifier)
            else:
                identifier = args.agent_id
                if not cx.execute("""
                    SELECT 1 FROM agents a JOIN users u ON u.id=a.user_id WHERE a.id=? AND u.disabled_at IS NULL
                    """, (identifier,)).fetchone():
                    raise ValueError("Unknown or disabled Agent")
            token = Security(db, secret("TEAM_TOKEN_PEPPER")).issue(cx, identifier, now() + args.days * 86400)
            return {"agent_id": identifier, "credential_id": token[4:36], "token": token}
        case "token-revoke":
            if not cx.execute("UPDATE credentials SET revoked_at=coalesce(revoked_at,?) WHERE id=?",
                              (now(), args.credential_id)).rowcount:
                raise ValueError("Unknown credential")
            audit(cx, "credential.revoke", entity=args.credential_id)
            return {"credential_id": args.credential_id, "revoked": True}
        case "reset-event-epoch":
            epoch = uuid4().hex
            cx.execute("UPDATE settings SET value=? WHERE key='event_epoch'", (epoch,))
            audit(cx, "event_epoch.reset")
            return {"event_epoch": epoch}
        case "status":
            states = {row["state"]: row["n"] for row in cx.execute(
                "SELECT state,count(*) AS n FROM uploads GROUP BY state")}
            return {"disk_free": shutil.disk_usage(cfg.disk_path).free, "uploads": states,
                    "reserved_bytes": cx.execute("SELECT coalesce(sum(reserved),0) FROM uploads").fetchone()[0],
                    "ready_bytes": cx.execute(
                        "SELECT coalesce(sum(size),0) FROM artifacts WHERE status='ready'").fetchone()[0],
                    "oldest_cleanup_due": cx.execute("SELECT min(cleanup_due) FROM uploads").fetchone()[0],
                    "sqlite_version": sqlite3.sqlite_version}
        case _:
            raise ValueError("Unknown command")


if __name__ == "__main__":
    main()
