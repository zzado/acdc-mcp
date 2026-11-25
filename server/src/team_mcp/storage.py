import hashlib
import json
import logging
import math
import shutil
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .collaboration import contains, view
from .db import DomainError, audit, canonical, event, idempotent, iso, now
from .security import accessible_channel
from .settings import MIB

LOG = logging.getLogger(__name__)
UPLOAD = """
SELECT u.*,a.channel_id,a.uploader_id,a.size,a.claimed_sha256,a.status AS artifact_status,
       a.object_key,a.verified_sha256 FROM uploads u JOIN artifacts a ON a.id=u.artifact_id
"""


def s3_client(url, region, access, password):
    return boto3.client(
        "s3", endpoint_url=url, region_name=region, aws_access_key_id=access, aws_secret_access_key=password,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=4,
                      read_timeout=60, retries={"total_max_attempts": 1}, request_checksum_calculation="when_required",
                      response_checksum_validation="when_required", ignore_configured_endpoint_urls=True),
    )


def s3_error(error, *codes):
    return error.response["Error"]["Code"] in codes


def url_fingerprint(uri):
    return hashlib.sha256(uri.encode()).hexdigest()


class Storage:
    def __init__(self, settings, db, collaboration, rate, staging_signer, final_signer, worker):
        self.cfg = settings
        self.db = db
        self.collaboration = collaboration
        self.rate = rate
        self.stage_signer = staging_signer
        self.final_signer = final_signer
        self.s3 = worker

    def ready(self):
        self.s3.head_bucket(Bucket=self.cfg.staging_bucket)
        self.s3.head_bucket(Bucket=self.cfg.final_bucket)

    @staticmethod
    def status(upload):
        return {"upload_id": upload["id"], "artifact_id": upload["artifact_id"], "state": upload["state"],
                "size": upload["size"], "mode": "multipart" if upload["part_size"] else "single",
                "part_size": upload["part_size"], "part_count": math.ceil(upload["size"] / upload["part_size"])
                if upload["part_size"] else None, "expires_at": iso(upload["expires_at"]),
                "error": upload["error"], "verified_sha256": upload["verified_sha256"]}

    @staticmethod
    def artifact(row):
        result = view(row)
        del result["object_key"]
        result["tags"] = json.loads(result["tags"])
        return result

    @staticmethod
    def upload(cx, upload_id, who=None):
        row = cx.execute(f"{UPLOAD} WHERE u.id=?", (upload_id,)).fetchone()
        if not row or (who is not None and who.agent_id != row["uploader_id"]):
            raise DomainError("NOT_FOUND", "Upload not found")
        return row

    def request_upload(self, who, channel, filename, size, sha256, key, media_type, description, tags, parent):
        if size > self.cfg.file_limit:
            raise DomainError("QUOTA_EXCEEDED", "File exceeds configured size limit")
        with self.db.transaction(write=True) as cx:
            accessible_channel(cx, who.agent_id, channel)

            def reserve():
                if parent and not cx.execute("SELECT 1 FROM artifacts WHERE id=? AND channel_id=? AND status='ready'",
                                             (parent, channel)).fetchone():
                    raise DomainError("NOT_FOUND", "Parent Artifact must be ready in this channel")
                reserved, count, own = cx.execute("""
                    SELECT coalesce(sum(reserved),0),
                        count(*) FILTER (WHERE state IN ('allocating','open','sealing','verifying','aborting')),
                        count(*) FILTER (WHERE state IN ('allocating','open','sealing','verifying','aborting')
                                         AND uploader_id=?)
                    FROM uploads JOIN artifacts ON artifacts.id=uploads.artifact_id
                    """, (who.agent_id,)).fetchone()
                committed = cx.execute("SELECT coalesce(sum(size),0) FROM artifacts WHERE status='ready'").fetchone()[0]
                if count >= self.cfg.total_uploads or own >= self.cfg.agent_uploads:
                    raise DomainError("RATE_LIMITED", "Active upload limit reached", retryable=True, retry_after_ms=3000)
                if committed + reserved + size > self.cfg.storage_limit:
                    raise DomainError("QUOTA_EXCEEDED", "Storage quota reached")
                if shutil.disk_usage(self.cfg.disk_path).free - self.cfg.disk_floor < 2 * (reserved + size):
                    raise DomainError("QUOTA_EXCEEDED", "Storage volume has insufficient free space")
                artifact_id, upload_id = uuid4().hex, uuid4().hex
                cx.execute("""
                    INSERT INTO artifacts(id,channel_id,uploader_id,filename,content_type,size,claimed_sha256,
                                          description,tags,parent_id,status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)
                    """, (artifact_id, channel, who.agent_id, filename, media_type, size, sha256, description,
                          canonical(tags), parent, now()))
                part_size = self.cfg.part_size if size > 64 * MIB else None
                cx.execute("""
                    INSERT INTO uploads(id,artifact_id,staging_key,state,part_size,expires_at,reserved)
                    VALUES (?,?,?,?,?,?,?)
                    """, (upload_id, artifact_id, f"staging/{upload_id}", "allocating" if part_size else "open",
                          part_size, now() + self.cfg.upload_ttl, size))
                audit(cx, "upload.reserve", actor=who.agent_id, entity=artifact_id)
                return upload_id

            upload_id = idempotent(cx, who.agent_id, "request_upload", key,
                                   [channel, filename, size, sha256, media_type, description, tags, parent], reserve)
            return self.status(self.upload(cx, upload_id))

    def request_upload_urls(self, who, upload_id, part_numbers):
        with self.db.transaction(write=True) as cx:
            upload = self.upload(cx, upload_id, who)
            accessible_channel(cx, who.agent_id, upload["channel_id"])
            current = now()
            if upload["expires_at"] <= current:
                raise DomainError("UPLOAD_EXPIRED", "Upload expired")
            if upload["state"] != "open":
                raise DomainError("CONFLICT", "Upload is not open", retryable=upload["state"] == "allocating")
            parts = self.status(upload)["part_count"]
            if ((parts and (not part_numbers or any(n > parts for n in part_numbers)))
                    or (not parts and part_numbers)):
                raise DomainError("VALIDATION", "Select valid Part numbers for a Multipart upload")
            transfers = []
            ttl = min(self.cfg.put_ttl, upload["expires_at"] - current)
            for part in dict.fromkeys(part_numbers or [None]):
                offset = (part - 1) * upload["part_size"] if part is not None else 0
                length = min(upload["part_size"], upload["size"] - offset) if part is not None else upload["size"]
                params = {"Bucket": self.cfg.staging_bucket, "Key": upload["staging_key"], "ContentLength": length}
                if part is not None:
                    params.update(UploadId=upload["garage_id"], PartNumber=part)
                url = self.stage_signer.generate_presigned_url("upload_part" if part else "put_object", Params=params,
                                                              ExpiresIn=ttl, HttpMethod="PUT")
                uri = urlsplit(url).path + "?" + urlsplit(url).query
                cx.execute("""
                    INSERT OR IGNORE INTO upload_grants(fingerprint,upload_id,credential_id,expected_bytes,expires_at)
                    VALUES (?,?,?,?,?)
                    """, (url_fingerprint(uri), upload_id, who.credential_id, length, current + ttl))
                grant = cx.execute("SELECT credential_id FROM upload_grants WHERE fingerprint=?",
                                   (url_fingerprint(uri),)).fetchone()
                if grant["credential_id"] != who.credential_id:
                    raise DomainError("CONFLICT", "Wait one second before requesting this URL again",
                                      retryable=True, retry_after_ms=1000)
                transfers.append({"method": "PUT", "url": url, "required_headers": {"Content-Length": str(length)},
                                  "part_number": part, "offset": offset, "length": length,
                                  "expires_at": iso(current + ttl)})
            return {"transfers": transfers}

    def authorize_put(self, uri, length, address):
        self.rate.take(("put-ip", address), rate=4 * self.cfg.put_rate, burst=max(32, 4 * self.cfg.put_rate))
        if length is None or not length.isascii() or not length.isdecimal() or len(length) > 9:
            raise DomainError("VALIDATION", "A valid Content-Length is required")
        length = int(length)
        if not uri or len(uri) > 4096:
            raise DomainError("FORBIDDEN", "Upload authorization not available")
        with self.db.transaction(write=True) as cx:
            grant = cx.execute("""
                SELECT g.*,u.state,u.expires_at AS upload_expiry,u.admitted_bytes,a.size,a.uploader_id,a.channel_id,
                       c.expires_at AS token_expiry,c.revoked_at,us.disabled_at
                FROM upload_grants g JOIN uploads u ON u.id=g.upload_id JOIN artifacts a ON a.id=u.artifact_id
                JOIN credentials c ON c.id=g.credential_id JOIN agents ag ON ag.id=a.uploader_id
                JOIN users us ON us.id=ag.user_id WHERE g.fingerprint=?
                """, (url_fingerprint(uri),)).fetchone()
            current = now()
            if (not grant or grant["state"] != "open" or grant["revoked_at"] is not None
                    or grant["disabled_at"] is not None or min(grant["expires_at"], grant["token_expiry"],
                                                              grant["upload_expiry"]) <= current):
                raise DomainError("FORBIDDEN", "Upload authorization not available")
            accessible_channel(cx, grant["uploader_id"], grant["channel_id"])
            if length != grant["expected_bytes"]:
                raise DomainError("VALIDATION", "Content-Length differs from the upload grant")
            self.rate.take(("put-agent", grant["uploader_id"]), rate=self.cfg.put_rate, burst=max(12, self.cfg.put_rate))
            if grant["admitted_bytes"] + max(length, 1) > 4 * max(grant["size"], 1):
                raise DomainError("QUOTA_EXCEEDED", "Upload retry byte budget reached")
            if shutil.disk_usage(self.cfg.disk_path).free < self.cfg.disk_floor + length:
                raise DomainError("QUOTA_EXCEEDED", "Storage volume has insufficient free space")
            cx.execute("UPDATE upload_grants SET uses=uses+1 WHERE fingerprint=?", (grant["fingerprint"],))
            cx.execute("UPDATE uploads SET admitted_bytes=admitted_bytes+?,last_put_at=? WHERE id=?",
                       (max(length, 1), current, grant["upload_id"]))

    def get_upload_status(self, who, upload_id, marker, limit):
        with self.db.transaction() as cx:
            upload = self.upload(cx, upload_id, who)
            result = self.status(upload) | {"parts": [], "next_part_cursor": None}
        if upload["state"] == "open" and upload["garage_id"]:
            data = self.s3.list_parts(Bucket=self.cfg.staging_bucket, Key=upload["staging_key"],
                                      UploadId=upload["garage_id"], PartNumberMarker=marker, MaxParts=limit)
            result["parts"] = [{"number": p["PartNumber"], "size": p["Size"], "etag": p["ETag"]}
                               for p in data.get("Parts", [])]
            if data.get("IsTruncated"):
                result["next_part_cursor"] = int(data["NextPartNumberMarker"])
        return result

    def complete_upload(self, who, upload_id):
        with self.db.transaction(write=True) as cx:
            upload = self.upload(cx, upload_id, who)
            accessible_channel(cx, who.agent_id, upload["channel_id"])
            if upload["state"] == "open":
                if upload["expires_at"] <= now():
                    raise DomainError("UPLOAD_EXPIRED", "Upload expired")
                cx.execute("UPDATE uploads SET state='sealing',error=NULL,next_run=0 WHERE id=?", (upload_id,))
                upload = self.upload(cx, upload_id)
            return self.status(upload)

    def abort_upload(self, who, upload_id):
        with self.db.transaction(write=True) as cx:
            upload = self.upload(cx, upload_id, who)
            if upload["state"] in {"allocating", "open", "sealing", "verifying"}:
                cx.execute("UPDATE uploads SET state='aborting',error=NULL,next_run=0 WHERE id=?", (upload_id,))
                audit(cx, "upload.abort", actor=who.agent_id, entity=upload["artifact_id"])
            return self.status(self.upload(cx, upload_id))

    @staticmethod
    def get_ready(cx, who, artifact_id):
        artifact = cx.execute("SELECT * FROM artifacts WHERE id=? AND status='ready'", (artifact_id,)).fetchone()
        if not artifact:
            raise DomainError("NOT_FOUND", "Artifact not found")
        accessible_channel(cx, who.agent_id, artifact["channel_id"])
        return artifact

    def get_artifact(self, who, artifact_id):
        with self.db.transaction() as cx:
            return self.artifact(self.get_ready(cx, who, artifact_id))

    def list_artifacts(self, who, channel, query, tag, cursor, limit):
        with self.db.transaction() as cx:
            accessible_channel(cx, who.agent_id, channel)

            def read(after, count):
                timestamp, artifact_id = after or [0, ""]
                return cx.execute("""
                    SELECT * FROM artifacts WHERE channel_id=? AND status='ready' AND (created_at,id)>(?,?)
                      AND (? IS NULL OR filename LIKE ? ESCAPE '!' OR description LIKE ? ESCAPE '!')
                      AND (? IS NULL OR EXISTS (SELECT 1 FROM json_each(tags) WHERE value=?))
                    ORDER BY created_at,id LIMIT ?
                    """, (channel, timestamp, artifact_id, query, contains(query or ""), contains(query or ""),
                          tag, tag, count)).fetchall()

            return self.collaboration.page(who, "artifacts", [channel, query, tag], cursor, limit,
                                           lambda row: [row["created_at"], row["id"]], read, self.artifact)

    def request_download(self, who, artifact_id):
        with self.db.transaction(write=True) as cx:
            row = self.get_ready(cx, who, artifact_id)
            expiry = now() + self.cfg.get_ttl
            url = self.final_signer.generate_presigned_url(
                "get_object", Params={"Bucket": self.cfg.final_bucket, "Key": row["object_key"]},
                ExpiresIn=self.cfg.get_ttl, HttpMethod="GET")
            audit(cx, "download.issue", actor=who.agent_id, entity=artifact_id)
            return {"method": "GET", "url": url, "expires_at": iso(expiry), "size": row["size"],
                    "sha256": row["verified_sha256"]}

    def head(self, bucket, key):
        try:
            return self.s3.head_object(Bucket=bucket, Key=key)
        except ClientError as error:
            if not s3_error(error, "404", "NoSuchKey", "NotFound"):
                raise
            return None

    def active_multiparts(self, key):
        paginator = self.s3.get_paginator("list_multipart_uploads")
        return [item["UploadId"] for page in paginator.paginate(Bucket=self.cfg.staging_bucket, Prefix=key)
                for item in page.get("Uploads", ()) if item["Key"] == key]

    def allocate(self, row):
        key = row["staging_key"]
        existing = sorted(self.active_multiparts(key))
        garage_id = existing[0] if existing else self.s3.create_multipart_upload(
            Bucket=self.cfg.staging_bucket, Key=key, ContentType="application/octet-stream")["UploadId"]
        with self.db.transaction(write=True) as cx:
            cx.execute("""
                UPDATE uploads SET state='open',garage_id=?,failure_count=0,next_run=0 WHERE id=? AND state='allocating'
                """, (garage_id, row["id"]))

    def reopen(self, row, code):
        with self.db.transaction(write=True) as cx:
            cx.execute("""UPDATE uploads SET state='open',error=?,parts=NULL,failure_count=0,next_run=0
                          WHERE id=? AND state='sealing'""", (code, row["id"]))

    def seal(self, row):
        bucket, key = self.cfg.staging_bucket, row["staging_key"]
        staged = self.head(bucket, key)
        if staged is None and row["part_size"]:
            args = {"Bucket": bucket, "Key": key, "UploadId": row["garage_id"]}
            if row["parts"]:
                parts = json.loads(row["parts"])
            else:
                results = [part for page in self.s3.get_paginator("list_parts").paginate(**args)
                           for part in page.get("Parts", ())]
                count = self.status(row)["part_count"]
                if len(results) != count or any(
                        p["PartNumber"] != n or p["Size"] != min(row["part_size"], row["size"] - (n - 1) * row["part_size"])
                        for n, p in enumerate(results, 1)):
                    self.reopen(row, "INCOMPLETE_PARTS")
                    return
                parts = [{"PartNumber": part["PartNumber"], "ETag": part["ETag"]} for part in results]
                with self.db.transaction(write=True) as cx:
                    if not cx.execute("UPDATE uploads SET parts=? WHERE id=? AND state='sealing'",
                                      (canonical(parts), row["id"])).rowcount:
                        return
            try:
                self.s3.complete_multipart_upload(**args, MultipartUpload={"Parts": parts})
            except ClientError as error:
                if s3_error(error, "InvalidPart"):
                    self.reopen(row, "INCOMPLETE_PARTS")
                    return
                if not s3_error(error, "NoSuchUpload") or not self.head(bucket, key):
                    raise
            staged = self.head(bucket, key)
            if staged is None:
                raise DomainError("STORAGE_UNAVAILABLE", "Staged object not yet visible", retryable=True)
        if staged is None:
            self.reopen(row, "OBJECT_MISSING")
            return
        if staged["ContentLength"] != row["size"]:
            self.fail(row, "SIZE_MISMATCH")
            return
        with self.db.transaction(write=True) as cx:
            cx.execute("""UPDATE uploads SET state='verifying',staged_etag=?,failure_count=0,next_run=0
                          WHERE id=? AND state='sealing'""", (staged["ETag"], row["id"]))

    def verify(self, row):
        # A copy intent is committed before invoking S3. Each key has at most one copy invocation.
        with self.db.transaction() as cx:
            previous = cx.execute("""
                SELECT * FROM finalization_attempts WHERE upload_id=? AND status='copying' ORDER BY created_at DESC
                """, (row["id"],)).fetchone()
        head = self.head(self.cfg.final_bucket, previous["object_key"]) if previous else None
        if head is None:
            with self.db.transaction(write=True) as cx:
                if self.upload(cx, row["id"])["state"] != "verifying":
                    return
                if previous:
                    cx.execute("UPDATE finalization_attempts SET status='orphan' WHERE id=?", (previous["id"],))
                attempt = uuid4().hex
                key = f"artifacts/{row['artifact_id']}/{attempt}"
                cx.execute("INSERT INTO finalization_attempts VALUES (?,?,?,?,'copying')",
                           (attempt, row["id"], key, now()))
            try:
                self.s3.copy_object(Bucket=self.cfg.final_bucket, Key=key,
                                    CopySource={"Bucket": self.cfg.staging_bucket, "Key": row["staging_key"]},
                                    CopySourceIfMatch=row["staged_etag"], MetadataDirective="REPLACE",
                                    ContentType="application/octet-stream", ContentDisposition="attachment")
            except ClientError as error:
                if s3_error(error, "412", "PreconditionFailed"):
                    self.fail(row, "STAGING_CHANGED")
                    return
                raise
            head = self.head(self.cfg.final_bucket, key)
        else:
            attempt, key = previous["id"], previous["object_key"]
        if head is None:
            raise DomainError("STORAGE_UNAVAILABLE", "Final object not yet visible", retryable=True)
        if head["ContentLength"] != row["size"]:
            self.fail(row, "SIZE_MISMATCH")
            return
        digest = hashlib.sha256()
        count = 0
        body = self.s3.get_object(Bucket=self.cfg.final_bucket, Key=key)["Body"]
        try:
            for chunk in body.iter_chunks(chunk_size=MIB):
                count += len(chunk)
                digest.update(chunk)
        finally:
            body.close()
        checksum = digest.hexdigest()
        if count != row["size"] or checksum != row["claimed_sha256"]:
            self.fail(row, "CHECKSUM_MISMATCH")
            return
        with self.db.transaction(write=True) as cx:
            if self.upload(cx, row["id"])["state"] != "verifying":
                return
            owner = cx.execute("""
                SELECT 1 FROM agents a JOIN users u ON u.id=a.user_id
                WHERE a.id=? AND u.disabled_at IS NULL
                """, (row["uploader_id"],)).fetchone()
            try:
                if not owner:
                    raise DomainError("FORBIDDEN", "Uploader was disabled")
                accessible_channel(cx, row["uploader_id"], row["channel_id"])
            except DomainError:
                self.mark_failed(cx, row, "FORBIDDEN")
                return
            cx.execute("""
                UPDATE artifacts SET status='ready',verified_sha256=?,object_key=?,finalized_at=? WHERE id=?
                """, (checksum, key, now(), row["artifact_id"]))
            cx.execute("UPDATE uploads SET state='ready',reserved=0,cleanup_due=? WHERE id=?",
                       (self.cleanup_time(row), row["id"]))
            cx.execute("UPDATE finalization_attempts SET status='chosen' WHERE id=?", (attempt,))
            event(cx, "artifact.ready", row["artifact_id"], row["channel_id"])
            audit(cx, "upload.finalize", actor=row["uploader_id"], entity=row["artifact_id"])

    def cleanup_time(self, row):
        return max(now(), (row["last_put_at"] or 0) + self.cfg.transfer_grace)

    def mark_failed(self, cx, row, code):
        if cx.execute("""
            UPDATE uploads SET state='failed',error=?,cleanup_due=?,next_run=0
            WHERE id=? AND state IN ('allocating','sealing','verifying')
            """, (code, self.cleanup_time(row), row["id"])).rowcount:
            cx.execute("UPDATE artifacts SET status='failed' WHERE id=?", (row["artifact_id"],))
            audit(cx, "upload.finalize", actor=row["uploader_id"], entity=row["artifact_id"], outcome=code)

    def fail(self, row, code):
        with self.db.transaction(write=True) as cx:
            self.mark_failed(cx, self.upload(cx, row["id"]), code)

    def clean(self, row):
        key = row["staging_key"]
        for garage_id in self.active_multiparts(key) if row["part_size"] else ():
            try:
                self.s3.abort_multipart_upload(Bucket=self.cfg.staging_bucket, Key=key, UploadId=garage_id)
            except ClientError as error:
                if not s3_error(error, "NoSuchUpload", "404"):
                    raise
        self.s3.delete_object(Bucket=self.cfg.staging_bucket, Key=key)
        with self.db.transaction() as cx:
            attempts = cx.execute("""
                SELECT * FROM finalization_attempts WHERE upload_id=? AND status IN ('orphan','copying')
                """, (row["id"],)).fetchall()
        for attempt in attempts:
            # Allow an ambiguous, earlier S3 copy to finish before deleting its private key.
            if attempt["created_at"] + self.cfg.transfer_grace > now():
                with self.db.transaction(write=True) as cx:
                    cx.execute("UPDATE uploads SET cleanup_due=? WHERE id=?",
                               (attempt["created_at"] + self.cfg.transfer_grace, row["id"]))
                return
            self.s3.delete_object(Bucket=self.cfg.final_bucket, Key=attempt["object_key"])
            with self.db.transaction(write=True) as cx:
                cx.execute("UPDATE finalization_attempts SET status='deleted' WHERE id=?", (attempt["id"],))
        with self.db.transaction(write=True) as cx:
            cx.execute("UPDATE uploads SET reserved=0,cleanup_due=NULL WHERE id=?", (row["id"],))
            cx.execute("DELETE FROM upload_grants WHERE upload_id=?", (row["id"],))

    def tick(self):
        current = now()
        with self.db.transaction(write=True) as cx:
            cx.execute("""UPDATE uploads SET state='aborting',error='UPLOAD_EXPIRED',next_run=0
                          WHERE state IN ('allocating','open') AND expires_at<=?""", (current,))
            row = cx.execute(f"""
                {UPLOAD}
                WHERE u.next_run<=? AND (u.state IN ('allocating','sealing','verifying','aborting')
                       OR u.cleanup_due<=?)
                ORDER BY u.next_run,u.rowid LIMIT 1
                """, (current, current)).fetchone()
        if not row:
            return False
        try:
            match row["state"]:
                case "allocating":
                    self.allocate(row)
                case "sealing":
                    self.seal(row)
                case "verifying":
                    self.verify(row)
                case "aborting":
                    with self.db.transaction(write=True) as cx:
                        latest = self.upload(cx, row["id"])
                        cx.execute("UPDATE uploads SET state='aborted',cleanup_due=? WHERE id=?",
                                   (self.cleanup_time(latest), latest["id"]))
                        cx.execute("UPDATE artifacts SET status='aborted' WHERE id=?", (latest["artifact_id"],))
                case _:
                    self.clean(row)
        except (BotoCoreError, ClientError, DomainError) as error:
            LOG.warning("storage_step_failed", extra={"upload_id": row["id"], "phase": row["state"],
                                                       "error_class": type(error).__name__})
            with self.db.transaction(write=True) as cx:
                latest = self.upload(cx, row["id"])
                if latest["state"] == row["state"]:
                    failures = latest["failure_count"] + 1
                    if failures >= 8 and latest["state"] in {"allocating", "sealing", "verifying"}:
                        self.mark_failed(cx, latest, "STORAGE_UNAVAILABLE")
                    else:
                        cx.execute("UPDATE uploads SET failure_count=?,next_run=? WHERE id=?",
                                   (failures, current + min(60, self.cfg.garage_backoff * failures), row["id"]))
        return True
