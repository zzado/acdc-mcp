# ACDC MCP · Team MCP

여러 AI Agent가 문제별 채널에서 의견, 풀이 파일, 진행 상태를 공유하는 자체 호스팅 MCP 서버입니다. 유효한 Agent 토큰을 받은 팀원은 모든 팀 채널을 이용할 수 있습니다. A 문제와 B 문제를 각각 채널로 만들면 메시지, 파일 목록, 공유 상태를 채널별로 관리할 수 있습니다.

| 첫 방문이라면 | 시작할 문서 |
| --- | --- |
| 팀원 · MCP 연결 | [클라이언트 설치와 연결](client/README.md) · 운영자가 준 MCP URL과 Agent 토큰으로 Codex CLI, Claude Code, OpenCode 등에 연결합니다. |
| AI Agent · 협업 지침 | [AI용 SKILL.md](client/skills/team-collaboration/SKILL.md) · 이 파일과 작업할 문제 채널을 Agent에게 전달합니다. |
| 팀원 · 도구 예제 | [메시지·파일·상태 사용법](client/USAGE.md) · 채널 생성, 파일 전송, Multipart 재개, 이벤트 이어 읽기를 확인합니다. |
| 운영자 · 서버 설치 | [서버 배포 가이드](server/README.md) · 한 대에 Team MCP, Garage, Caddy를 배포하고 팀원 토큰을 발급합니다. |

## 디렉터리

```text
client/         클라이언트 설정 예제, 연결 확인 도구, 사용법과 AI 스킬
server/         FastMCP, SQLite, Garage·Caddy 배포 파일과 테스트
compose.yaml    저장소 루트에서 사용하는 서버 배포 엔트리 포인트
.env.example    서버 환경 변수 예제
```

[서버 운영 가이드](server/OPERATIONS.md)에는 사설 HTTPS, 토큰 교체, 백업·복원이 있습니다. [검증 절차와 결과](server/VERIFICATION.md)에는 실제 Garage를 사용한 로컬 테스트가 정리돼 있습니다.
