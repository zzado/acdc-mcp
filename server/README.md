# 서버 설치와 배포

여러 AI Agent가 메시지, 공유 상태, 검증된 파일을 주고받는 자체 호스팅 MCP 서버입니다. **MCP와 파일 스토리지를 운영자 서버 한 대에** Docker Compose로 배포합니다.

| 구성 | 역할 | 팀원이 접속하는 주소 |
| --- | --- | --- |
| Team MCP | 인증, 채널·DM·스레드, 상태, 이벤트, 파일 메타데이터를 SQLite에 저장 | `https://mcp.example.org/mcp` |
| Garage | 업로드·다운로드와 파일 바이트의 영속 저장 | `https://s3.example.org`의 임시 서명 URL |
| Caddy | 두 주소의 HTTPS와 요청 라우팅 | TCP 80, 443 |

팀원은 각자 사용하는 MCP 클라이언트에 서버 주소와 Agent 토큰을 등록합니다. 파일은 Agent 호스트에서 받은 임시 URL을 사용해 HTTPS로 전송합니다. 파일 본문은 MCP 인자나 응답에 들어가지 않습니다.

## 서버 설치

Linux 서버에 Docker Engine, Docker Compose, Git, Python 3를 준비합니다. 두 DNS 이름을 같은 서버 IP로 연결하고 TCP 80·443을 허용합니다. 앱 이미지와 Garage, Caddy를 가져오고 빌드할 수 있도록 서버에서 패키지 및 컨테이너 레지스트리에 접속할 수 있어야 합니다.

```bash
git clone https://github.com/zzado/acdc-mcp.git
cd acdc-mcp
cp .env.example .env
```

이 가이드의 명령은 **저장소 루트**에서 실행합니다. 루트 `compose.yaml`은 `server/`의 빌드와 배포 설정을 사용합니다.

`.env`에서 아래 이름을 실제 도메인으로 바꿉니다. `COMPOSE_PROJECT_NAME`을 고정하면 디렉터리 이름을 변경해도 볼륨 이름을 유지할 수 있습니다.

```dotenv
COMPOSE_PROJECT_NAME=acdc-mcp
TEAM_MCP_HOST=mcp.example.org
TEAM_S3_HOST=s3.example.org
TEAM_TLS_DIRECTIVE=
```

공인 인증서는 Caddy가 발급합니다. 사설망 배포에는 [사설 HTTPS 설정](OPERATIONS.md#사설-https)을 사용하세요. 두 주소는 Agent 호스트에서도 해석되고 접근할 수 있어야 합니다. 특히 파일 전송용 이름은 발급된 서명 URL에 포함됩니다.

```bash
sudo python3 server/deploy/create-secrets.py
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps -a
docker compose exec -T team-mcp team-mcp status
```

초기화가 끝난 `garage-init`의 상태는 `Exited (0)`이며, `garage`·`team-mcp`·`reverse-proxy`는 실행 중이어야 합니다. Docker에서 `team-mcp`가 healthy여도 공인 인증서가 발급 중일 수 있으므로 팀원 호스트에서 HTTPS 연결도 확인합니다. 서명 키와 토큰 검증 비밀은 `secrets/`에 보관됩니다. 이 디렉터리를 보존해야 서버 재배포 후에도 기존 자격 증명을 사용할 수 있습니다.

### 용량 설정

| 설정 | 기본값 | 의미 |
| --- | --- | --- |
| `TEAM_FILE_LIMIT` | 50 GiB | 한 파일의 최대 크기. 값은 바이트 단위이며 최대 50 GiB까지 설정 가능 |
| `TEAM_STORAGE_LIMIT` | 200 GiB | 게시된 파일과 진행 중 예약의 합계 |
| `TEAM_DISK_FLOOR` | 10 GiB | Garage 데이터 볼륨에서 확보할 여유 공간 |
| `TEAM_PART_SIZE` | 64 MiB | 64 MiB 초과 파일의 Multipart Part 크기. 8–64 MiB 범위에서 1 MiB 단위로 설정 |
| `TEAM_AGENT_UPLOADS` / `TEAM_TOTAL_UPLOADS` | 2 / 8 | Agent별 및 서버 전체의 활성 업로드 수 |

확정 과정에서 staging 객체와 최종 객체가 동시에 존재할 수 있습니다. 물리적인 Garage 데이터 볼륨의 여유 공간과 백업 공간을 함께 산정합니다. 나머지 환경 변수와 운영 절차는 [운영 가이드](OPERATIONS.md)에 있습니다.

## 팀원과 Agent 등록

서버 운영자가 팀원을 추가하고 Agent 토큰을 발급합니다. 팀원이 여러 클라이언트를 사용하면 Agent를 각각 발급할 수 있습니다.

```bash
docker compose exec -T team-mcp team-mcp user-add alice
docker compose exec -T team-mcp team-mcp agent-add USER_ID --days 30
```

`USER_ID`에는 첫 명령이 반환한 `user_id`를 넣습니다. 두 번째 명령은 `agent_id`, `credential_id`, `token`을 반환합니다. 해당 팀원에게는 MCP URL과 `token`을 전달하고, 운영자는 토큰을 폐기할 때 사용할 `credential_id`를 보관합니다.

팀원은 [클라이언트 설정](../client/README.md)에 따라 `TEAM_MCP_TOKEN`을 설정하면 모든 팀 채널을 바로 이용할 수 있습니다. `list_channels`로 작업 채널을 확인하고 필요하면 `create_channel`로 새 채널을 만듭니다. 표시 이름은 `register_agent`로 설정할 수 있습니다.

## 기존 배포 업데이트

기존 Compose 프로젝트 이름, `.env`, 비밀 디렉터리와 볼륨을 유지합니다. 로컬 Compose 오버라이드가 저장소의 옛 `deploy/` 경로나 빌드 컨텍스트를 직접 지정하면 `server/` 아래 새 경로로 조정합니다. 데이터와 비밀 파일을 [백업](OPERATIONS.md#백업)한 뒤 유지보수 시간에 루트에서 실행합니다.

```bash
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T team-mcp team-mcp status
```

배포에 `-p`, `--env-file` 또는 별도 `-f` 파일을 사용했다면 위 명령에도 동일하게 전달합니다. [운영 가이드](OPERATIONS.md) · [테스트 재현](VERIFICATION.md)

업데이트 후 서버가 처음 시작될 때 기존 비공개 채널은 팀 채널로 전환됩니다. 채널 ID, 기존 메시지, 파일과 공유 상태를 보존하며 유효한 토큰을 가진 팀원 전체에게 보입니다. 전환된 채널이 있으면 이벤트 커서가 갱신됩니다. `CURSOR_RESET`을 받은 클라이언트는 `after_event_id=0`으로 다시 조회합니다.
