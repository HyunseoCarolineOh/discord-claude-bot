# discord-claude-bot

Discord 채널마다 다른 페르소나(PM / Backend / Frontend …)를 가진 Claude 에이전트를 운영하는 봇입니다.
Anthropic API가 아니라 **로컬에 설치된 `claude` CLI를 subprocess로 호출**해서 사용자의 Claude 구독(Teams 등)을 그대로 사용합니다.

## 동작 개요

1. 봇이 Discord에서 메시지를 감지 (`@봇` 멘션 시)
2. 채널 ID로 페르소나 + 프로젝트 디렉토리 매핑 조회
3. 해당 페르소나 webhook으로 "🤔 생각 중..." placeholder 전송
4. `claude -p "<메시지>" --append-system-prompt "<페르소나 프롬프트>" --output-format json` 을 프로젝트 디렉토리(`cwd`)에서 실행
5. 응답을 placeholder에 edit (길면 분할 전송)

채널마다 페르소나(이름·아바타·시스템 프롬프트)와 프로젝트 디렉토리가 다르므로,
- `#backend` 채널 → "Claude (Backend)"가 백엔드 코드 위에서 동작
- `#frontend` 채널 → "Claude (Frontend)"가 프론트엔드 코드 위에서 동작

같은 식으로 한 봇 프로세스가 여러 페르소나를 동시에 운영합니다.

## 사전 준비

### 1. claude CLI 설치 + 로그인

```powershell
# 이미 설치돼있다면 스킵
npm install -g @anthropic-ai/claude-code

# OS 레벨 로그인 (Teams/Pro/Max 구독 사용)
claude login
```

봇은 `claude` 명령이 PATH에 있고 이미 로그인되어 있다고 가정합니다.

확인:
```powershell
claude --version
```

### 2. Discord 봇 생성 + 권한

1. https://discord.com/developers/applications → **New Application**
2. 좌측 **Bot** 메뉴 → **Reset Token** 으로 토큰 발급 → `.env`의 `DISCORD_BOT_TOKEN`에 복사
3. **Privileged Gateway Intents** 에서 **MESSAGE CONTENT INTENT** 활성화 (필수)
4. 좌측 **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `View Channels`, `Send Messages`, `Read Message History`, **`Manage Webhooks`** (필수 — 페르소나 username/avatar 사용)
5. 생성된 URL로 봇을 본인 서버에 초대

### 3. Python 환경

```powershell
cd C:\Projects\discord-claude-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 설정

### `.env`

```powershell
Copy-Item .env.example .env
notepad .env  # DISCORD_BOT_TOKEN 입력
```

### `config.yaml`

```powershell
Copy-Item config.yaml.example config.yaml
notepad config.yaml  # 채널 ID, 페르소나, 프로젝트 디렉토리 편집
```

채널 ID는 Discord 클라이언트 **사용자 설정 → 고급 → 개발자 모드 활성화** 후
채널 우클릭 → **Copy Channel ID** 로 얻습니다.

`project_dir` 디렉토리는 미리 존재해야 합니다 (없으면 봇이 시작 시 에러).
```powershell
New-Item -ItemType Directory -Path projects\myapp-backend
```

## 실행

```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

정상 시 로그:
```
2026-... [INFO] bot: 로그인 완료: claude-bot#0001 (id=...) | 매핑된 채널 3개 | trigger=mention
```

## 페르소나 추가/수정

`personas/<key>.md` 만들고 `config.yaml`의 `personas:` 와 `channels:` 에 추가합니다.
프롬프트 파일은 봇 호출 시마다 다시 읽으므로 봇 재시작 없이 수정 사항이 반영됩니다.

## trigger_mode

`config.yaml`의 `bot.trigger_mode`로 응답 조건을 바꿀 수 있습니다.

- `mention` (기본): `@봇` 멘션이 있을 때만 응답
- `prefix`: 메시지가 `bot.prefix` (기본 `!claude`)로 시작할 때만 응답
- `all_messages`: 매핑된 채널의 모든 사용자 메시지에 응답 (구독 한도 빨리 닳음, 주의)

## 동시성

`claude` CLI 동시 실행은 `asyncio.Semaphore(1)`로 직렬화됩니다.
즉 채널 A의 요청이 처리 중이면 채널 B의 요청은 큐에서 대기합니다.
처리 중에도 placeholder("🤔 생각 중...")는 즉시 전송되므로 사용자는 봇이 동작 중인지 인지할 수 있습니다.

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 봇이 메시지에 반응하지 않음 | 채널 ID가 `config.yaml`에 매핑됐는지 + Message Content Intent 활성화 + `trigger_mode` 조건 충족 확인 |
| `webhook 생성 권한 없음 (#xxx): Manage Webhooks 권한 필요` | OAuth2 URL 재생성 시 Manage Webhooks 권한 포함 → 봇 재초대 |
| `claude exit code N: ...` | `claude login` 으로 재로그인. 또는 `claude --version` 으로 CLI 자체 확인 |
| `claude 응답 JSON 파싱 실패` | `claude` CLI 버전이 너무 낮을 수 있음 — 최신으로 업데이트 |
| `claude 응답이 N초 내에 오지 않았어요` | `config.yaml`의 `bot.claude_timeout` 늘리기 (기본 300초) |
| PowerShell에서 한글이 `���`로 깨짐 | 봇 동작에는 영향 없음. 콘솔에서 깔끔하게 보고 싶으면 `chcp 65001` 후 재실행 |
| webhook 캐시가 꼬인 듯 | `webhook_cache.json` 삭제 → 다음 호출 시 webhook 자동 재생성 |
| `DISCORD_BOT_TOKEN 환경변수가 설정되지 않았습니다` | `.env` 파일이 봇과 같은 디렉토리에 있는지, 키 이름 정확한지 |
| `personas.X.prompt_file 파일이 없습니다` | `config.yaml`의 prompt_file 경로와 실제 파일 일치 확인 |

## 파일 구조

```
discord-claude-bot/
├── README.md
├── requirements.txt
├── .env.example          # DISCORD_BOT_TOKEN
├── .env                  # (gitignore) 실제 토큰
├── .gitignore
├── config.yaml.example   # 페르소나/채널 매핑 예시
├── config.yaml           # (gitignore) 실제 설정
├── webhook_cache.json    # (gitignore, 자동 생성) channel_id -> webhook_id
├── bot.py                # 메인 entry point
├── claude_runner.py      # claude CLI subprocess 래퍼
├── webhook_manager.py    # 채널별 webhook 생성/캐시
├── config.py             # config.yaml 로드/검증
├── personas/             # 페르소나 시스템 프롬프트
│   ├── pm.md
│   ├── backend.md
│   └── frontend.md
└── projects/             # 프로젝트별 작업 디렉토리(gitignore)
```

## 단독 테스트

`claude_runner.py`만 따로 동작 확인 가능:
```powershell
python claude_runner.py --message "안녕" --cwd .
```

## 보안 메모

- `.env` 와 `config.yaml`, `webhook_cache.json`, `projects/` 는 `.gitignore`에 포함되어 있습니다
- Anthropic API 키는 사용하지 않습니다 (OS에 로그인된 `claude` CLI 사용)
- Discord 봇 토큰이 유출되면 즉시 Developer Portal에서 **Reset Token**
