"""claude CLI subprocess 래퍼.

- shell=True 의미를 살리되 사용자 메시지를 안전하게 escape 처리
  (Windows: subprocess.list2cmdline / POSIX: shlex.join)
- asyncio.Semaphore(1)로 동시 호출 1개 직렬화
- timeout, JSON 파싱, exit code 모두 명시적으로 처리
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 동시 실행 1개로 직렬화 — 구독 한도와 충돌 방지
_RUN_SEMAPHORE = asyncio.Semaphore(1)


# 페르소나 시스템 프롬프트 뒤에 강제로 덧붙이는 길이·형식 가드.
# 페르소나 .md 본문의 형식 권장(체크리스트, ## 제목 등)과 충돌해도 후순위 지시인 이쪽이 우선.
RESPONSE_LENGTH_GUARD = """

=== 응답 형식 강제 규칙 (위 페르소나 지시보다 우선) ===
- 한 응답은 3~5줄, 한 단락(약 400자) 이내. 절대 넘기지 마세요.
- 결론을 첫 줄에 한 문장으로. 그다음 핵심 근거 1줄, 필요시 액션 1줄.
- 인사·메타 멘트("좋은 질문이네요", "정리해드릴게요" 등) 금지.
- 사용자가 묻지 않은 디테일은 풀어쓰지 말고 결과만. 부연은 후속 질문이 오면 추가.
- ## 제목 금지. 코드 블록·인라인 코드는 사용자가 명시적으로 요청할 때만 출력.
- 불확실하면 추측하지 말고 1줄 질문으로 끝내기.
"""


# 환각·날조 방지 가드. 위의 RESPONSE_LENGTH_GUARD보다도 더 늦게 부착되어 최우선.
# {team_block}은 run_claude에서 팀 메타정보로 채워진다.
RESPONSE_FACTUALITY_GUARD_TEMPLATE = """

=== 사실성 강제 규칙 (모든 위 지시보다 우선, 절대 위반 금지) ===
{team_block}
- 위 "팀 멤버 키 목록"에 **없는 키**를 `@<key>` 형태로 호명하지 마세요. 일반 직무명(AE, SDR, CRO, CSM 등)을 키처럼 쓰는 것도 금지.
- 위 목록에 없는 페르소나·역할을 **새로 만들어** 호명하지 마세요. 필요한 역할이 빠져 있으면 호명 대신 "이 역할은 현재 팀에 없어 보임 — 추가할지 결정 필요" 라고 사용자에게 한 줄 질문으로 끝내세요.
- 팀 구성, 일정, 과거 결정사항, 외부 시스템 상태, 코드 파일 경로·함수명·라이브러리 — 본인이 **직접 확인하지 않은 사실은 단정하지 마세요**. 확신 없으면 "확실하지 않음" 또는 "확인 필요"로 표시.
- 사용자/팀이 한 적 없는 합의·약속·발화를 "지난번에 정한 대로", "이미 합의된 대로" 같은 표현으로 만들어내지 마세요. 토론·대화 컨텍스트(이전 발언 블록)에 없는 내용은 새로 도입한다고 명시.
- `projects/<team>/facts.md` 가 있으면 그 안의 내용만 **확정 사실**로 취급. 그 밖의 사실은 추정으로 표시하거나 사용자에게 확인.
- 모르면 추측하지 말고 1줄 질문으로 끝내세요. 모르는 걸 말하는 것보다 모른다고 인정하는 게 항상 낫습니다.
"""


def _build_team_block(team_context: dict | None) -> str:
    """팀 메타정보를 사실성 가드에 주입할 텍스트 블록으로 변환."""
    if not team_context:
        return "- (팀 컨텍스트 미제공: 어떤 `@<key>` 멘션도 추측해서 만들지 말고, 호명 대신 사용자에게 물어보세요.)"
    members = team_context.get("members") or []
    team_name = team_context.get("display_name") or team_context.get("team_key") or "(팀명 미상)"
    self_key = team_context.get("self_key")
    facts_path = team_context.get("facts_path")
    members_line = ", ".join(f"@{m}" for m in members) if members else "(없음)"
    self_line = f"- 당신 자신의 키: @{self_key} (자기 자신은 호명 금지)" if self_key else ""
    facts_line = (
        f"- 이 팀의 확정 사실은 `{facts_path}` 에 정리되어 있습니다. 거기 없는 것은 추정·미확인으로 표시하세요."
        if facts_path else
        ""
    )
    lines = [
        f"- 당신이 속한 팀: {team_name}",
        f"- 이 팀의 **유효 페르소나 키 목록 (이게 전부)**: {members_line}",
    ]
    if self_line:
        lines.append(self_line)
    if facts_line:
        lines.append(facts_line)
    return "\n".join(lines)


class ClaudeRunnerError(RuntimeError):
    """claude CLI 실행 관련 에러의 베이스."""


class ClaudeTimeout(ClaudeRunnerError):
    """timeout 초과."""


@dataclass(frozen=True)
class ClaudeResult:
    text: str
    duration_ms: int
    cost_usd: float
    session_id: str
    raw: dict


def _build_shell_command(args: list[str]) -> str:
    """플랫폼별 안전한 인자 quoting."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _flatten_for_windows_cmd(text: str) -> str:
    """Windows cmd가 인자 안의 줄바꿈에서 명령을 끊어버려, --output-format 같은
    뒤쪽 인자가 잘려나가는 문제 회피. 줄바꿈을 공백으로 치환해 의미는 보존."""
    return " ".join(text.splitlines())


async def run_claude(
    *,
    message: str,
    system_prompt: str,
    project_dir: Path,
    timeout: int = 300,
    claude_cmd: str = "claude",
    team_context: dict | None = None,
) -> ClaudeResult:
    """claude CLI 단일 호출. Semaphore(1)에 의해 직렬화됨.

    team_context: {team_key, display_name, members: [keys], self_key, facts_path}
    제공되면 사실성 가드에 팀 메타정보를 주입해 페르소나가 없는 키·없는 역할을
    임의로 호명하는 것을 막는다.
    """
    if not project_dir.is_dir():
        raise ClaudeRunnerError(f"project_dir이 디렉토리가 아닙니다: {project_dir}")

    factuality_guard = RESPONSE_FACTUALITY_GUARD_TEMPLATE.format(
        team_block=_build_team_block(team_context),
    )
    combined_system = system_prompt + RESPONSE_LENGTH_GUARD + factuality_guard
    safe_system = (
        _flatten_for_windows_cmd(combined_system)
        if sys.platform == "win32"
        else combined_system
    )
    # message는 stdin으로 보내므로 cmdline 길이 한도(Windows ~32k자) 영향을 받지 않는다
    args = [
        claude_cmd,
        "-p",
        "--append-system-prompt", safe_system,
        "--output-format", "json",
    ]
    cmd_str = _build_shell_command(args)
    log.info(
        "claude 실행 시작 (cwd=%s, prompt_len=%d, timeout=%ds)",
        project_dir, len(message), timeout,
    )

    async with _RUN_SEMAPHORE:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=str(project_dir),
            )
        except OSError as e:
            raise ClaudeRunnerError(f"claude CLI 실행 실패: {e}") from e

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=message.encode("utf-8")), timeout=timeout
            )
        except asyncio.TimeoutError:
            # 자식 프로세스가 살아있을 수 있으니 정리
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise ClaudeTimeout(
                f"claude 응답이 {timeout}초 내에 오지 않았습니다."
            ) from None

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        snippet = stderr.strip() or stdout.strip()[:500]
        raise ClaudeRunnerError(
            f"claude exit code {proc.returncode}: {snippet}"
        )

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ClaudeRunnerError(
            f"claude 출력 JSON 파싱 실패: {e}. stdout 앞부분: {stdout[:500]!r}"
        ) from e

    if data.get("is_error"):
        raise ClaudeRunnerError(
            f"claude is_error=true: {data.get('result') or data}"
        )

    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        raise ClaudeRunnerError(f"claude result 필드가 비어있거나 잘못됨: {data}")

    log.info(
        "claude 응답 완료 (duration_ms=%s, cost_usd=%s)",
        data.get("duration_ms"), data.get("total_cost_usd"),
    )
    return ClaudeResult(
        text=text,
        duration_ms=int(data.get("duration_ms", 0)),
        cost_usd=float(data.get("total_cost_usd", 0.0)),
        session_id=str(data.get("session_id", "")),
        raw=data,
    )


async def _cli_main() -> None:
    """단독 테스트용:
    python claude_runner.py --message "안녕" --cwd .
    """
    import argparse

    parser = argparse.ArgumentParser(description="claude_runner 단독 테스트")
    parser.add_argument("--message", required=True, help="claude에 보낼 프롬프트")
    parser.add_argument(
        "--system",
        default="간결하게 한 문장으로 답하세요.",
        help="append-system-prompt로 붙일 시스템 프롬프트",
    )
    parser.add_argument("--cwd", default=".", help="claude 실행 작업 디렉토리")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    result = await run_claude(
        message=args.message,
        system_prompt=args.system,
        project_dir=Path(args.cwd).resolve(),
        timeout=args.timeout,
    )
    print("=== response ===")
    print(result.text)
    print("=== meta ===")
    print(
        f"duration_ms={result.duration_ms} "
        f"cost_usd={result.cost_usd} "
        f"session={result.session_id}"
    )


if __name__ == "__main__":
    asyncio.run(_cli_main())
