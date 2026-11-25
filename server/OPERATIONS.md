# 서버 운영 가이드

[README의 초기 설치](README.md#서버-설치)를 마친 Linux 서버에서 실행합니다. 명령은 저장소 루트를 기준으로 합니다. Docker 접근 권한이 있는 서버 운영자가 아래 관리 작업을 수행합니다.

## 사설 HTTPS

사설 DNS에서 MCP와 파일 전송 호스트 이름을 같은 서버로 연결합니다. `.env`에 두 이름을 각각 설정하고 `TEAM_TLS_DIRECTIVE="tls internal"`을 추가합니다.

```bash
docker compose up -d --wait reverse-proxy
docker compose cp reverse-proxy:/data/caddy/pki/authorities/local/root.crt /tmp/acdc-mcp-root.crt
```

내보낸 루트 인증서를 팀원이 신뢰하는 경로로 전달하고 각 Agent 호스트의 신뢰 저장소에 추가합니다. 클라이언트에 따라 별도의 CA 설정이 필요할 수 있습니다. curl에서는 `--cacert`로 인증서 파일을 지정할 수 있습니다. Caddy 데이터 볼륨에는 CA의 개인키도 있으므로 백업을 암호화합니다.

Windows에서 사설 테스트 CA를 확인하려면 전달받은 인증서를 `.\root.crt`에 두고 토큰 없이 다음 명령을 실행합니다. `401`이 표시되면 TLS 연결을 마치고 MCP 서버의 인증 응답을 받은 것입니다.

```powershell
$mcpUrl = Read-Host '사설 테스트 MCP URL (/mcp 포함)'
curl.exe --cacert .\root.crt --silent --show-error --output NUL --write-out '%{http_code}\n' $mcpUrl
```

`curl.exe --version`에 `Schannel`이 표시되고 오류 60의 사유가 `the revocation status is unknown`이면, **격리된 사설 테스트 CA에 한해** 위 명령에 `--ssl-revoke-best-effort`를 추가합니다. 이 옵션은 폐기 정보를 구할 수 없을 때 연결을 허용합니다. 같은 오류가 계속되면 해당 테스트 호출에서만 `--ssl-no-revoke`로 교체할 수 있습니다. 이 옵션은 폐기 확인 전체를 끄므로 `--cacert`는 유지하고 공인 인증서를 쓰는 운영 `:8888`과 전역 curl 설정에서는 두 옵션을 모두 생략합니다. [curl의 Schannel 옵션 설명](https://curl.se/docs/manpage.html#--ssl-no-revoke)

## 팀원·Agent와 토큰

```bash
docker compose exec -T team-mcp team-mcp user-add alice
docker compose exec -T team-mcp team-mcp agent-add USER_ID --days 30
docker compose exec -T team-mcp team-mcp token-issue AGENT_ID --days 30
docker compose exec -T team-mcp team-mcp token-revoke OLD_CREDENTIAL_ID
docker compose exec -T team-mcp team-mcp user-disable USER_ID
```

등록은 앞의 두 명령으로 완료합니다. 토큰을 교체할 때는 `token-issue`로 같은 Agent의 새 토큰을 발급하고, 팀원이 새 토큰으로 접속한 뒤 기존 `credential_id`를 폐기합니다. 팀원 계정을 비활성화하면 해당 팀원의 Agent 토큰도 더 이상 인증되지 않습니다. 폐기 전에 발급된 다운로드 URL은 정해진 시각까지 유효합니다.

### 채널 접근

유효한 Agent 토큰으로 모든 팀 채널을 조회하고 메시지·파일·상태를 공유할 수 있습니다. 팀원 접근을 종료하려면 토큰을 폐기하거나 팀원 계정을 비활성화합니다. 일대일 DM은 두 참여자에게 표시됩니다.

## 설정, 점검 및 로그

| 설정 | 기본값 | 적용 내용 |
| --- | --- | --- |
| `TEAM_MCP_HOST`, `TEAM_S3_HOST` | `mcp.example.org`, `s3.example.org` | 클라이언트 MCP 주소와 서명 URL의 호스트 이름 |
| `TEAM_HTTP_BIND`, `TEAM_HTTPS_BIND` | 80, 443 | Caddy가 서버에 게시하는 포트 |
| `TEAM_SECRETS_DIR` | `./secrets` | 서버 비밀 파일의 위치 |
| `GARAGE_CAPACITY` | `300GB` | Garage 최초 레이아웃에 기록되는 노드의 선언 용량 |
| `TEAM_UPLOAD_TTL` | 86,400초 | 업로드 세션 유효 기간 |
| `TEAM_PUT_TTL`, `TEAM_GET_TTL` | 300초, 600초 | 업로드·다운로드 서명 URL의 기본 유효 기간 |
| `TEAM_TRANSFER_GRACE` | 900초 | 승인된 전송이 남아 있을 수 있어 정리를 유예하는 시간 |
| `TEAM_PUT_RATE` | 초당 2건 | Agent별 PUT 승인율 |

파일과 저장 용량 제한은 [README의 용량 설정](README.md#용량-설정)을 참고합니다. `TEAM_TRANSFER_GRACE`는 Caddy의 최대 업로드 15분 이상으로 설정합니다. 애플리케이션은 만료·취소 세션을 계속 정리하며, Garage 초기화 작업도 7일이 지난 미완료 Multipart를 정리합니다.

```bash
docker compose config --quiet
docker compose ps -a
docker compose exec -T team-mcp team-mcp status
docker compose logs --tail 100 team-mcp garage garage-init reverse-proxy
```

`status`의 `disk_free`, `reserved_bytes`, `ready_bytes`, `oldest_cleanup_due`를 관찰합니다. Garage와 SQLite가 다른 파일시스템에 있으면 두 파일시스템의 여유 공간을 각각 확인합니다.

| 증상 | 확인할 항목 |
| --- | --- |
| `garage-init` 실패 | Garage 상태와 초기화 로그, 비밀 파일 경로·권한을 확인합니다. 원인을 수정한 뒤 `docker compose run --rm garage-init`으로 재실행합니다. |
| 공인 인증서 발급 실패 | MCP·파일 호스트 이름의 DNS, 두 포트와 Caddy 로그를 확인합니다. |
| MCP `401` | Agent 토큰의 만료·폐기와 클라이언트 프로세스의 환경 변수를 확인합니다. |
| PUT `400` | `Content-Length`가 반환된 크기와 일치하는지 확인합니다. |
| 파일 전송 주소의 인증서·서명 오류 | Agent에서 파일 호스트의 DNS와 TLS를 확인합니다. 서명 URL의 호스트·경로·쿼리는 발급된 그대로 사용합니다. |
| 파일이 목록에 없음 | 업로더가 `get_upload_status`에서 `ready`와 검증 해시를 확인했는지, 조회 Agent가 채널 권한을 받았는지 확인합니다. |

설정을 변경한 뒤 `docker compose up -d --wait`로 적용합니다. 코드와 이미지를 업데이트할 때는 백업 후 배포할 커밋에서 `docker compose up -d --build --wait`를 실행합니다. 정상 중지는 `docker compose stop`을 사용합니다. `down -v`는 프로젝트의 데이터 볼륨을 삭제합니다.

## 백업

경기 외 시간에 새 작업을 중지하고 진행 중인 업로드와 검증이 끝나기를 기다립니다. 서비스 중지 상태에서 SQLite 백업과 Garage 데이터를 한 시점으로 묶습니다. `db-backup`과 `status`는 기존 DB를 읽기 전용으로 열어 현재 스키마와 채널 권한을 보존합니다. 아래 예제는 `.env`에 `COMPOSE_PROJECT_NAME=acdc-mcp`를 설정한 배포를 기준으로 합니다. 다른 프로젝트 이름을 사용했다면 `project`에 해당 이름을 넣습니다.

```bash
(
set -e
umask 077
project=acdc-mcp
backup_dir=$(mktemp -d "$HOME/acdc-mcp-backup.XXXXXX")
backup_db="backup-$(date -u +%Y%m%dT%H%M%SZ).sqlite"
helper_image='python:3.14.10-slim-trixie@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b'

docker volume inspect "${project}_sqlite" "${project}_garage-meta" \
  "${project}_garage-data" "${project}_caddy-data" >/dev/null
git rev-parse HEAD > "$backup_dir/revision"
docker compose -p "$project" images > "$backup_dir/images.txt"
printf '%s\n' "$backup_db" > "$backup_dir/sqlite-filename"

running=$(docker compose -p "$project" ps --status running --services reverse-proxy team-mcp garage)
services=()
if [[ -n "$running" ]]; then mapfile -t services <<< "$running"; fi
resume_services() {
  local result=$?
  if (( ${#services[@]} )); then
    docker compose -p "$project" up -d --no-deps --no-recreate --wait "${services[@]}" || exit 1
  fi
  if (( result == 0 )); then printf 'Backup: %s\n' "$backup_dir"; fi
  exit "$result"
}
trap resume_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if (( ${#services[@]} )); then
  docker compose -p "$project" stop "${services[@]}"
fi
docker compose -p "$project" run --rm --no-deps -T team-mcp \
  db-backup "/var/lib/team-mcp/$backup_db" </dev/null

docker run --rm \
  --mount "type=volume,src=${project}_sqlite,dst=/snapshot/db,readonly" \
  --mount "type=volume,src=${project}_garage-meta,dst=/snapshot/meta,readonly" \
  --mount "type=volume,src=${project}_garage-data,dst=/snapshot/data,readonly" \
  --mount "type=volume,src=${project}_caddy-data,dst=/snapshot/caddy,readonly" \
  --mount "type=bind,src=$backup_dir,dst=/backup" \
  "$helper_image" sh -ec 'tar -C /snapshot -czf /backup/volumes.tar.gz "db/$1" meta data caddy' sh "$backup_db"

(cd "$backup_dir" && sha256sum volumes.tar.gz > volumes.sha256)
)
```

`.env`, 변경한 Compose·Caddy·Garage 설정과 원래 `secrets/`도 같은 복구 시점에 맞춰 보관합니다. 아카이브에는 팀의 파일과 TLS 키가 들어 있습니다. 자료를 암호화해 다른 호스트에 보관합니다. 각 백업은 SQLite 볼륨에도 백업 DB 한 개를 남기므로 보존 정책에 따라 오래된 백업의 용량을 관리합니다.

## 빈 볼륨에 복원

원본과 동일한 코드 리비전과 구성으로 복구 환경을 준비합니다. 보관했던 비밀 파일과 권한을 복원하고, 원본과 다른 **새 Compose 프로젝트 이름**을 선택합니다. 아래 `backup_dir`에는 검증할 백업의 절대 경로를 넣습니다. 복원 도우미가 네 볼륨을 모두 검사한 뒤 압축을 풉니다. 중단된 복원을 다시 시도할 때는 새 프로젝트 이름을 사용하고, 이전 볼륨은 점검할 수 있도록 보관합니다.

복구용 Compose 오버라이드를 사용한다면 아래 `recovery` 배열에 해당 `-f` 옵션을 추가합니다. 네 볼륨이 모두 복구 프로젝트 전용인지 확인합니다.

```bash
set -e
recovery_project=acdc-mcp-restore
recovery=(docker compose -p "$recovery_project" -f compose.yaml)
backup_dir=/absolute/path/to/acdc-mcp-backup
backup_db=$(cat "$backup_dir/sqlite-filename")

(cd "$backup_dir" && sha256sum --check volumes.sha256)
"${recovery[@]}" build team-mcp garage-init </dev/null

"${recovery[@]}" -f - run --rm --no-deps -T \
  -v "$backup_dir:/backup:ro" restore sh -ec '
    exec 9</snapshot/db
    flock -n 9 || { echo "Another restore holds the database volume" >&2; exit 1; }
    for dir in /snapshot/db /snapshot/meta /snapshot/data /snapshot/caddy; do
      entry=$(find "$dir" -mindepth 1 -maxdepth 1 -printf x -quit)
      if [ -n "$entry" ]; then
        printf "Restore target is not empty: %s\n" "$dir" >&2
        exit 1
      fi
    done
    tar -C /snapshot -xzf /backup/volumes.tar.gz
    mv -- "/snapshot/db/$1" /snapshot/db/team.sqlite
    chown 10001:10001 /snapshot/db /snapshot/db/team.sqlite
  ' sh "$backup_db" <<'YAML'
services:
  restore:
    image: python:3.14.10-slim-trixie@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b
    network_mode: none
    volumes:
      - sqlite:/snapshot/db
      - garage-meta:/snapshot/meta
      - garage-data:/snapshot/data
      - caddy-data:/snapshot/caddy
YAML

"${recovery[@]}" run --rm --no-deps -T team-mcp reset-event-epoch </dev/null
"${recovery[@]}" up -d --wait team-mcp </dev/null
"${recovery[@]}" exec -T team-mcp team-mcp status </dev/null
```

이 단계까지는 복구 프로젝트의 Caddy가 시작되지 않습니다. 준비 상태를 확인한 뒤 기존 서비스와 포트 충돌이 없는 시점에 `"${recovery[@]}" up -d --wait reverse-proxy`를 실행합니다. 외부 접속을 열기 전에 테스트 계정으로 메시지, 채널 권한, Artifact 다운로드와 SHA-256을 점검합니다. 백업 시점 이후에 폐기했던 토큰과 비활성화한 계정은 다시 반영합니다. 이벤트 커서에 `CURSOR_RESET`이 반환되면 0부터 동기화를 다시 시작합니다.

단일 Garage 노드에서는 디스크나 서버 장애 시 복구에 걸리는 시간만큼 서비스가 중지되고, 마지막 정상 백업 이후의 변경분을 잃을 수 있습니다.
