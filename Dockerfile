FROM python:3.14.10-trixie@sha256:5d5f5785427bf5e7f806f373885aff02a46b2e989ccb5bd0a99ce2b7d9bf5b31 AS build
WORKDIR /app
RUN curl -fsSLo /tmp/sqlite.tgz https://sqlite.org/2026/sqlite-autoconf-3540100.tar.gz \
    && echo "926ef8d3eab93fbf6e06011901273eec076b25594a4e1cdadd50c987f4f6d64e  /tmp/sqlite.tgz" | sha256sum -c - \
    && mkdir /tmp/sqlite && tar -xzf /tmp/sqlite.tgz --strip-components=1 -C /tmp/sqlite \
    && cd /tmp/sqlite && ./configure --prefix=/opt/sqlite --disable-static \
    && make -j2 && make install
RUN pip install --no-cache-dir uv==0.12.9
COPY pyproject.toml uv.lock ./
COPY src ./src
ARG UV_FLAGS=--no-dev
RUN uv sync --locked --no-editable ${UV_FLAGS}

FROM python:3.14.10-slim-trixie@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b
ENV PATH="/app/.venv/bin:$PATH" LD_LIBRARY_PATH=/opt/sqlite/lib PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /opt/sqlite/lib/ /opt/sqlite/lib/
COPY --from=build /app/.venv/ /app/.venv/
RUN python -c "import sqlite3; assert sqlite3.sqlite_version == '3.54.1', sqlite3.sqlite_version" \
    && mkdir -p /var/lib/team-mcp && chown 10001:10001 /var/lib/team-mcp
USER 10001:10001
ENTRYPOINT ["team-mcp"]
CMD ["serve"]
