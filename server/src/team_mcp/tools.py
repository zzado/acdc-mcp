import asyncio
import inspect
import logging
import sqlite3
from functools import cached_property, wraps
from typing import Annotated, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastmcp.exceptions import ValidationError
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import FunctionTool, ToolResult
from jsonschema import Draft202012Validator
from pydantic import Field, JsonValue, StringConstraints

from .db import DomainError

LOG = logging.getLogger(__name__)
Identifier = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
MessageID = Annotated[int, Field(ge=1, lt=2**63)]
Nonnegative = Annotated[int, Field(ge=0, lt=2**63)]
Limit = Annotated[int, Field(ge=1, le=50)]
Cursor = Annotated[str, StringConstraints(max_length=1024)]
Text = Annotated[str, StringConstraints(max_length=512)]
Query = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
DisplayName = Annotated[str, Field(min_length=1, max_length=80,
                                   description="New names must be nonblank, single-line and free of control "
                                               "and bidi formatting characters.")]
Filename = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^/\x00-\x1f\\]+$")]
IdemKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]
# Share ASCII rules across regex engines. Pydantic checks Unicode word characters on the server.
StateKey = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[\w.-]+$"),
                     Field(description="Unicode word characters, dot or hyphen; keys are case-sensitive.",
                           json_schema_extra={"pattern": r"^(?:[A-Za-z0-9._-]|[^\u0000-\u007f])+$"})]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Part = Annotated[int, Field(ge=1, le=10_000)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]

FIELDS = {
    "id": {"type": ["string", "integer"]}, "name": {"type": "string"}, "kind": {"type": "string"},
    "channel_id": {"type": "string"}, "sender_id": {"type": "string"}, "type": {"type": "string"},
    "content": {"type": "string"}, "event_id": {"type": "integer"},
    "items": {"type": "array", "items": {"type": "object"}},
    "next_cursor": {"type": ["string", "null"]}, "events": {"type": "array", "items": {"type": "object"}},
    "next_event_id": {"type": "integer"}, "event_epoch": {"type": "string"}, "has_more": {"type": "boolean"},
    "upload_id": {"type": "string"}, "artifact_id": {"type": "string"}, "state": {"type": "string"},
    "mode": {"enum": ["single", "multipart"]}, "expires_at": {"type": "string"},
    "part_size": {"type": ["integer", "null"]}, "part_count": {"type": ["integer", "null"]},
    "parts": {"type": "array", "items": {"type": "object"}},
    "next_part_cursor": {"type": ["integer", "null"]},
    "transfers": {"type": "array", "items": {"type": "object", "required": ["method", "url", "required_headers",
                  "offset", "length", "expires_at"]}},
    "method": {"const": "GET"}, "url": {"type": "string"}, "size": {"type": "integer"},
    "sha256": {"type": "string"}, "filename": {"type": "string"}, "verified_sha256": {"type": "string"},
    "key": {"type": "string"}, "value": {}, "revision": {"type": "integer"}, "deleted": {"type": "boolean"},
}
PAGE = ("items", "next_cursor")
UPLOAD_STATUS = {
    name: FIELDS[name]
    for name in ("upload_id", "artifact_id", "state", "mode", "size", "part_size", "part_count", "expires_at")
} | {
    "error": {"type": ["string", "null"]},
    "verified_sha256": {"type": ["string", "null"]},
}
TOOL_ERROR = {
    "type": "object",
    "required": ["code", "message", "retryable"],
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
        "retry_after_ms": {"type": "integer"},
    },
}


def output_schema(properties):
    # Upload status carries its own nullable error code. Tool errors use an object.
    success = {"properties": {"error": False} | properties, "required": list(properties)}
    failure = {"properties": {"error": TOOL_ERROR}, "required": ["error"], "additionalProperties": False}
    return {"type": "object", "oneOf": [success, failure]}


class TeamTool(FunctionTool):
    @cached_property
    def input_validator(self):
        return Draft202012Validator(self.parameters)

    async def run(self, arguments):
        try:
            # Check the published JSON types before Pydantic normalizes Python arguments.
            if not self.input_validator.is_valid(arguments):
                raise ValidationError("Arguments do not match the input schema")
            return await super().run(arguments)
        except ValidationError:
            error = DomainError("VALIDATION", f"Arguments do not match the input schema for {self.name}")
            return ToolResult(structured_content={"error": error.body}, is_error=True)


def install_tools(mcp, collaboration, storage, security, rate):
    def tool(*fields, **extra_properties):
        def decorate(function):
            @wraps(function)
            async def call(*args, **kwargs):
                try:
                    token = get_access_token()
                    who = await asyncio.to_thread(security.authenticate, token.token) if token else None
                    if not who:
                        raise DomainError("FORBIDDEN", "An active Agent token is required")
                    rate.take(("tool", who.agent_id))
                    if asyncio.iscoroutinefunction(function):
                        result = await function(who, *args, **kwargs)
                    else:
                        result = await asyncio.to_thread(function, who, *args, **kwargs)
                    return ToolResult(structured_content=result)
                except DomainError as error:
                    return ToolResult(structured_content={"error": error.body}, is_error=True)
                except (ClientError, BotoCoreError) as error:
                    LOG.warning("tool_storage_failure", extra={"operation": function.__name__,
                                                              "error_class": type(error).__name__})
                    return ToolResult(structured_content={"error": {"code": "STORAGE_UNAVAILABLE",
                                      "message": "Storage operation failed", "retryable": True}}, is_error=True)
                except sqlite3.OperationalError as error:
                    if getattr(error, "sqlite_errorcode", 0) & 0xff == sqlite3.SQLITE_BUSY:
                        return ToolResult(structured_content={"error": {"code": "RATE_LIMITED",
                                          "message": "Database is busy", "retryable": True,
                                          "retry_after_ms": 250}}, is_error=True)
                    LOG.error("tool_database_failure", extra={"operation": function.__name__})
                    return ToolResult(structured_content={"error": {"code": "INTERNAL",
                                      "message": "Database operation failed", "retryable": False}}, is_error=True)
                except Exception as error:
                    LOG.error("tool_failed", extra={"operation": function.__name__,
                                                   "error_class": type(error).__name__})
                    return ToolResult(structured_content={"error": {"code": "INTERNAL",
                                      "message": "Operation failed", "retryable": False}}, is_error=True)

            # The injected Principal is private and must not be part of the tool schema.
            signature = inspect.signature(function)
            call.__signature__ = signature.replace(parameters=list(signature.parameters.values())[1:],
                                                   return_annotation=ToolResult)
            properties = {name: FIELDS[name] for name in fields} | extra_properties
            return mcp.add_tool(TeamTool.from_function(call, output_schema=output_schema(properties)))
        return decorate

    @tool("id", "name")
    def register_agent(who, name: DisplayName, description: Text = "", runtime: Name | None = None):
        """Set your Agent profile on the identity assigned to this token."""
        return collaboration.register_agent(who, name, description, runtime)

    @tool(*PAGE)
    def list_agents(who, agent_id: Identifier | None = None, cursor: Cursor | None = None, limit: Limit = 20):
        """List team Agents and their recent activity or inspect one Agent by ID."""
        return collaboration.list_agents(who, agent_id, cursor, limit)

    @tool("id", "name", "kind")
    def create_channel(who, name: DisplayName, idempotency_key: IdemKey,
                       visibility: Literal["team"] = "team"):
        """Create a channel shared by every team member."""
        # Keep the explicit visibility argument accepted by earlier clients.
        return collaboration.create_channel(who, name, idempotency_key)

    @tool(*PAGE)
    def list_channels(who, cursor: Cursor | None = None, limit: Limit = 20):
        """List all team channels and the Agent's own DMs."""
        return collaboration.list_channels(who, cursor, limit)

    @tool("id", "channel_id", "sender_id", "type", "content", "event_id")
    def send_message(who, content: Annotated[str, StringConstraints(min_length=1, max_length=8192)],
                     idempotency_key: IdemKey, channel_id: Identifier | None = None, dm_agent_id: Identifier | None = None,
                     reply_to_id: MessageID | None = None,
                     type: Literal["chat", "finding", "candidate", "review_request", "review_result"] = "chat",
                     artifact_ids: Annotated[list[Identifier], Field(max_length=8)] | None = None):
        """Send a message to exactly one channel, Agent DM, or reply target. Attach ready Artifacts by ID."""
        if len(content.encode()) > 8192:
            raise DomainError("VALIDATION", "Message exceeds 8 KiB")
        return collaboration.send_message(who, content, idempotency_key, channel_id, dm_agent_id, reply_to_id,
                                          type, list(dict.fromkeys(artifact_ids or [])))

    @tool(*PAGE)
    def read_messages(who, channel_id: Identifier, query: Query | None = None, thread_root_id: MessageID | None = None,
                      cursor: Cursor | None = None, limit: Limit = 20):
        """Read or search messages in a channel or thread, newest first."""
        return collaboration.read_messages(who, channel_id, query, thread_root_id, cursor, limit)

    @tool("events", "next_event_id", "event_epoch", "has_more")
    async def read_events(who, after_event_id: Nonnegative = 0, event_epoch: Identifier | None = None,
                          wait_ms: Annotated[int, Field(ge=0, le=2000)] = 0, limit: Limit = 20):
        """Resume with this Agent's paired event ID and epoch. On CURSOR_RESET start at 0 without an epoch."""
        return await collaboration.wait_events(who, after_event_id, event_epoch, wait_ms, limit)

    @tool(**UPLOAD_STATUS)
    def request_upload(who, channel_id: Identifier, filename: Filename, size: Nonnegative, sha256: Sha256,
                       idempotency_key: IdemKey, content_type: Name = "application/octet-stream",
                       description: Text = "", tags: Annotated[list[Tag], Field(max_length=16)] | None = None,
                       parent_artifact_id: Identifier | None = None):
        """Reserve an Artifact using the original file size and SHA-256. Transfer the file separately over HTTPS."""
        if filename in {".", ".."}:
            raise DomainError("VALIDATION", "Specify an original filename")
        return storage.request_upload(who, channel_id, filename, size, sha256.lower(), idempotency_key,
                                      content_type, description, list(dict.fromkeys(tags or [])), parent_artifact_id)

    @tool("transfers")
    def request_upload_urls(who, upload_id: Identifier, part_numbers: Annotated[list[Part], Field(max_length=8)] | None = None):
        """Get short-lived HTTPS PUT URLs and required headers for a file or up to eight Parts."""
        return storage.request_upload_urls(who, upload_id, part_numbers)

    @tool("parts", "next_part_cursor", **UPLOAD_STATUS)
    def get_upload_status(who, upload_id: Identifier, part_cursor: Annotated[int, Field(ge=0, le=10_000)] = 0,
                          limit: Limit = 20):
        """Get upload status and list committed Parts for retry or resume."""
        return storage.get_upload_status(who, upload_id, part_cursor, limit)

    @tool(**UPLOAD_STATUS)
    def complete_upload(who, upload_id: Identifier):
        """Request asynchronous file sealing and SHA-256 verification. Poll upload status for the result."""
        return storage.complete_upload(who, upload_id)

    @tool(**UPLOAD_STATUS)
    def abort_upload(who, upload_id: Identifier):
        """Cancel a pending upload and schedule staging cleanup."""
        return storage.abort_upload(who, upload_id)

    @tool("method", "url", "expires_at", "size", "sha256")
    def request_download(who, artifact_id: Identifier):
        """Get a short-lived HTTPS GET URL, size and verified SHA-256 for a ready Artifact."""
        return storage.request_download(who, artifact_id)

    @tool(*PAGE)
    def list_artifacts(who, channel_id: Identifier, query: Query | None = None, tag: Tag | None = None,
                       cursor: Cursor | None = None, limit: Limit = 20):
        """List or search ready Artifacts in an accessible channel."""
        return storage.list_artifacts(who, channel_id, query, tag, cursor, limit)

    @tool("id", "filename", "channel_id", "size", "verified_sha256")
    def get_artifact(who, artifact_id: Identifier):
        """Get metadata and verification details for a ready Artifact."""
        return storage.get_artifact(who, artifact_id)

    @tool("key", "value", "revision", "deleted")
    def get_state(who, channel_id: Identifier, key: StateKey):
        """Read a channel state key and its revision, including a deletion marker when present."""
        return collaboration.get_state(who, channel_id, key)

    @tool("key", "value", "revision", "deleted")
    def set_state(who, channel_id: Identifier, key: StateKey, value: JsonValue, expected_revision: Nonnegative,
                  idempotency_key: IdemKey):
        """Set a channel state key only if its revision matches. Use revision 0 for a new key."""
        return collaboration.write_state(who, channel_id, key, value, expected_revision, idempotency_key)

    @tool(*PAGE)
    def list_state(who, channel_id: Identifier, prefix: Text = "", cursor: Cursor | None = None, limit: Limit = 20):
        """List live channel state keys, optionally filtered by prefix."""
        return collaboration.list_state(who, channel_id, prefix, cursor, limit)

    @tool("key", "value", "revision", "deleted")
    def delete_state(who, channel_id: Identifier, key: StateKey, expected_revision: Nonnegative, idempotency_key: IdemKey):
        """Delete a channel state value only if its revision matches."""
        return collaboration.write_state(who, channel_id, key, None, expected_revision, idempotency_key, deleted=True)
