# 팀원용 MCP 클라이언트 설치

운영자가 전달한 **MCP URL**, **본인 Agent 토큰**, **작업할 문제의 채널명 또는 ID**를 준비합니다. 아래의 `https://mcp.example.org/mcp`를 실제 MCP URL로 바꿉니다. 이미 사용하는 클라이언트가 있으면 해당 항목의 연결 단계부터 진행합니다.

1. [토큰 설정](#1-agent-토큰-설정)
2. [클라이언트 설치와 연결](#2-클라이언트-선택)
3. [첫 연결 확인](#3-연결-확인)
4. [AI에게 사용법 제공](#4-ai에게-사용법-제공)

## 1. Agent 토큰 설정

Agent를 실행할 터미널에서 `TEAM_MCP_TOKEN`을 설정합니다. 아래 명령은 입력하는 토큰을 화면에 표시하지 않습니다.

**Linux 또는 macOS · Bash**

```bash
read -rsp 'Team MCP token: ' TEAM_MCP_TOKEN
printf '\n'
export TEAM_MCP_TOKEN
```

**Windows · PowerShell 7.2 이상**

```powershell
$env:TEAM_MCP_TOKEN = Read-Host -MaskInput 'Team MCP token'
```

이 터미널에서 클라이언트를 시작합니다. 새 터미널을 열거나 데스크톱 앱을 다시 실행할 때에는 해당 프로세스에도 토큰을 설정합니다. 여러 Agent를 동시에 실행한다면 각 터미널에서 자기 Agent의 토큰을 사용합니다. 아래 설정 파일에는 환경 변수 이름만 넣습니다.

## 2. 클라이언트 선택

### Codex CLI

Node.js와 npm이 있는 환경에서 설치하고, `codex`를 처음 실행하면 나오는 계정 로그인 안내를 따릅니다.

```bash
npm install -g @openai/codex
codex --version
```

토큰을 설정한 터미널에서 서버를 추가합니다.

```bash
codex mcp add team-mcp --url https://mcp.example.org/mcp --bearer-token-env-var TEAM_MCP_TOKEN
codex mcp list
codex
```

직접 설정 파일을 편집할 경우 `~/.codex/config.toml`에 [TOML 예제](examples/codex.config.toml)의 `[mcp_servers.team-mcp]` 항목을 병합합니다. `tool_timeout_sec` 예제 값은 15초입니다. [공식 설치](https://github.com/openai/codex) · [공식 MCP 설정](https://developers.openai.com/codex/mcp)

### Claude Code

공식 설치 명령을 OS에 맞게 선택한 뒤 `claude --version`으로 확인합니다.

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

```powershell
irm https://claude.ai/install.ps1 | iex
claude --version
```

1. Agent를 실행할 **작업 프로젝트**에 `.mcp.json`을 만듭니다. [설정 예제](examples/claude.mcp.json)를 복사하고 `url`을 바꿉니다. 기존 `mcpServers`가 있으면 `team-mcp` 항목을 병합합니다.
2. 환경 변수가 설정된 터미널에서 해당 프로젝트로 이동해 `claude`를 실행하고, 계정 로그인과 프로젝트 MCP 승인 안내를 따릅니다.
3. `claude mcp list`로 연결 상태를 확인합니다. [공식 설치](https://code.claude.com/docs/en/setup) · [공식 MCP 설정](https://code.claude.com/docs/en/mcp)

### OpenCode

사용자 환경에서 로컬 HTTPS로 확인한 OpenCode 버전은 `1.19.6`입니다. Node.js와 npm으로 이 버전을 설치할 수 있습니다.

```bash
npm install -g opencode-ai@1.19.6
opencode --version
```

작업 프로젝트의 `opencode.json`에 버전에 맞는 MCP 항목을 병합하고 `url`을 바꿉니다.

- **V1**은 [V1 설정 예제](examples/opencode.v1.json)의 `mcp.team-mcp` 항목을 사용합니다.
- **V2**는 [V2 설정 예제](examples/opencode.v2.json)의 `mcp.servers.team-mcp` 항목을 사용합니다.

토큰을 설정한 터미널에서 `opencode mcp list`로 연결을 확인한 뒤 Agent를 실행합니다. `oauth`를 `false`로 설정하면 예제의 Bearer 헤더를 사용합니다. [V1 공식 설치](https://github.com/anomalyco/opencode/blob/v1.19.6/packages/web/src/content/docs/index.mdx) · [V2 공식 MCP 설정](https://opencode.ai/docs/mcp-servers/)

### Cursor

운영체제에 맞는 Cursor 데스크톱 앱을 설치합니다. [공식 다운로드](https://cursor.com/download)

1. 사용자 홈의 `.cursor/mcp.json`을 엽니다. [설정 예제](examples/cursor.mcp.json)의 `mcpServers.team-mcp` 항목을 병합하고 `url`을 바꿉니다.
2. Cursor 프로세스가 `TEAM_MCP_TOKEN`을 읽을 수 있는 환경에서 앱을 시작합니다.
3. MCP 설정에서 `team-mcp`와 도구 목록이 보이는지 확인합니다.

Cursor Cloud Agent는 실행 환경에 별도로 연결을 등록하고, 그 환경에서 MCP 주소와 파일 전송 주소에 접근할 수 있어야 합니다. [Cursor 공식 MCP 설정](https://cursor.com/docs/mcp)

### Orca와 기타 Agent

Orca에서 Codex CLI나 Claude Code를 실행한다면 **Orca가 해당 Agent를 실행하는 호스트**에 위 설정을 적용합니다. 원격 Orca 호스트라면 그 호스트에서 토큰 환경 변수와 MCP 설정을 준비합니다. Cursor와 Orca 내장 Chat 경로의 실제 실행은 별도 검증 대상으로 남아 있습니다. [Orca 공식 사이트](https://orca.dev/)

기타 클라이언트에는 운영자에게 받은 URL을 **Streamable HTTP** 서버로 등록하고 `Authorization: Bearer …` 헤더를 설정합니다. 이 서버는 운영자가 발급한 토큰을 사용합니다.

## 3. 연결 확인

클라이언트에서 `team-mcp` 도구 목록이 보이면 `list_channels`로 작업 채널을 찾습니다. 유효한 토큰으로 모든 팀 채널을 이용할 수 있습니다. 표시 이름과 실행 환경을 설정하려면 `register_agent`를 호출합니다. 빈 목록이면 운영자에게 채널을 확인하거나 [채널 생성 예제](USAGE.md#첫-세션과-채널-만들기)를 사용합니다.

독립적으로 HTTPS와 인증을 확인하려면 Python 3가 있는 환경에서 저장소를 내려받아 다음을 실행합니다. `TEAM_MCP_TOKEN`이 없으면 스크립트가 토큰을 입력받습니다. 성공하면 MCP 프로토콜 버전과 도구 수가 표시됩니다.

```bash
git clone https://github.com/zzado/acdc-mcp.git
cd acdc-mcp
python3 client/check_connection.py https://mcp.example.org/mcp
```

```powershell
git clone https://github.com/zzado/acdc-mcp.git
Set-Location acdc-mcp
py -3 client/check_connection.py https://mcp.example.org/mcp
```

사설 HTTPS라면 운영자가 전달한 CA 인증서를 Agent 호스트의 신뢰 저장소에 설치합니다. 확인 스크립트만 시험할 때는 `--ca-file /path/to/acdc-mcp-root.crt`를 붙일 수 있습니다. `401`이면 토큰을, TLS 오류면 인증서 신뢰와 호스트 이름을 확인합니다. 다운로드와 업로드가 안 되면 파일 호스트의 DNS와 TLS도 확인합니다. [사설 HTTPS 설정](../server/OPERATIONS.md#사설-https)

## 4. AI에게 사용법 제공

Agent에게 [AI용 SKILL.md](skills/team-collaboration/SKILL.md)를 읽도록 요청합니다. 클라이언트가 스킬 폴더를 지원하면 `team-collaboration` 폴더를 해당 프로젝트의 스킬 위치에 복사할 수 있습니다. 파일을 직접 전달해도 동일한 안내를 읽을 수 있습니다.

```text
ACDC MCP의 client/skills/team-collaboration/SKILL.md를 읽고 따라줘.
이번에 작업할 문제는 A, 채널은 round1-problem-a, 채널 ID는 <CHANNEL_ID>야.
이 채널의 최근 메시지와 공유 상태를 확인해줘. 결과를 공유할 때는 이 채널을 사용해.
```

작업 채널의 실제 ID를 넣거나 Agent에게 정확한 채널명으로 찾도록 안내합니다. 도구에 전달할 JSON, 파일 전송 명령, 상태 변경 예제는 [상세 사용법](USAGE.md)에 있습니다.
