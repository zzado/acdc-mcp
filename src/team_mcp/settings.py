import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

MIB = 1024**2
GIB = 1024**3


def secret(name: str) -> str:
    path = os.getenv(f"{name}_FILE")
    return Path(path).read_text(encoding="utf-8").strip() if path else os.getenv(name, "")


class Settings(BaseModel):
    database: Path = Path("/var/lib/team-mcp/team.sqlite")
    mcp_url: str = "https://mcp.example.org/mcp"
    s3_public_url: str = "https://s3.example.org"
    s3_internal_url: str = "http://garage:3900"
    s3_region: str = "garage"
    staging_bucket: str = "team-staging"
    final_bucket: str = "team-final"
    disk_path: Path = Path("/garage-data")
    file_limit: int = Field(50 * GIB, ge=0, le=50 * GIB)
    storage_limit: int = Field(200 * GIB, ge=0)
    disk_floor: int = Field(10 * GIB, ge=0)
    part_size: int = Field(64 * MIB, ge=8 * MIB, le=64 * MIB)
    upload_ttl: int = Field(86400, ge=10, le=604800)
    put_ttl: int = Field(300, ge=1, le=900)
    get_ttl: int = Field(600, ge=1, le=900)
    transfer_grace: int = Field(900, ge=1)
    agent_uploads: int = Field(2, ge=1)
    total_uploads: int = Field(8, ge=1)
    put_rate: int = Field(2, ge=1, le=16)
    garage_backoff: int = Field(3, ge=1)

    @model_validator(mode="after")
    def check_urls(self):
        for value, path in ((self.mcp_url, "/mcp"), (self.s3_public_url, "")):
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.path != path or url.query or url.fragment or url.username:
                raise ValueError("Set canonical HTTPS URLs for MCP and S3")
        if self.staging_bucket == self.final_bucket:
            raise ValueError("Staging and final must use distinct buckets")
        if self.part_size % MIB or self.file_limit > 10_000 * self.part_size:
            raise ValueError("Part size must be whole MiB and cover the file limit within 10,000 parts")
        return self

    @classmethod
    def from_env(cls):
        return cls.model_validate({field: os.environ[f"TEAM_{field.upper()}"] for field in cls.model_fields
                                   if f"TEAM_{field.upper()}" in os.environ})
