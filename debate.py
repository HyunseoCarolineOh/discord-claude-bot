"""페르소나 스레드 토론 세션 상태 관리.

DebateSession: 한 토론 스레드의 상태 (참여자, 발화 횟수, 종료 여부).
DebateRegistry: 봇 프로세스 내 활성 세션 보관소 (thread_id 기준).

봇 재시작하면 메모리상 세션은 사라짐 — 1차 구현은 in-memory only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


class DebateError(RuntimeError):
    """토론 세션 관련 에러."""


@dataclass
class DebateSession:
    thread_id: int
    topic: str
    parent_channel_id: int
    current_speaker: str  # 페르소나 키. 봇이 호출 직전 갱신
    round_counts: dict[str, int] = field(default_factory=dict)  # 페르소나별 발화 횟수
    ended: bool = False
    end_reason: str | None = None  # "user_command" | "concluded" | "max_total_speeches" | "config_error"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def record_speech(self, persona_key: str) -> int:
        """발화 1회 기록. 새 카운트 반환."""
        new_count = self.round_counts.get(persona_key, 0) + 1
        self.round_counts[persona_key] = new_count
        return new_count

    @property
    def total_speeches(self) -> int:
        return sum(self.round_counts.values())

    def is_at_safety_limit(self, max_total_speeches: int) -> bool:
        return self.total_speeches >= max_total_speeches

    def end(self, reason: str) -> None:
        self.ended = True
        self.end_reason = reason


class DebateRegistry:
    """thread_id -> DebateSession 인메모리 저장소."""

    def __init__(self) -> None:
        self._sessions: dict[int, DebateSession] = {}

    def start(
        self,
        *,
        thread_id: int,
        topic: str,
        parent_channel_id: int,
        first_speaker: str,
    ) -> DebateSession:
        if thread_id in self._sessions:
            raise DebateError(f"thread_id={thread_id} 에 이미 세션이 있습니다.")
        session = DebateSession(
            thread_id=thread_id,
            topic=topic,
            parent_channel_id=parent_channel_id,
            current_speaker=first_speaker,
        )
        self._sessions[thread_id] = session
        return session

    def get(self, thread_id: int) -> DebateSession | None:
        return self._sessions.get(thread_id)

    def remove(self, thread_id: int) -> None:
        self._sessions.pop(thread_id, None)

    def __contains__(self, thread_id: int) -> bool:
        return thread_id in self._sessions

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if not s.ended)


def parse_next_speaker(
    response_text: str, available_personas: list[str]
) -> str | None:
    """페르소나 응답 본문에서 다음 발화자 멘션을 파싱.

    응답의 마지막 줄부터 거슬러 올라가며 @key 형태를 찾음.
    여러 페르소나가 멘션됐으면 가장 마지막 줄의 멘션을 우선.
    available_personas에 없는 키워드는 무시.
    결론 마커 줄은 건너뜀 (마커 + 멘션 동시 존재 시 마커 우선이지만
    혹시 마커 줄에 @멘션이 섞여 있어도 멘션 파싱이 그걸 잡지 않도록).
    """
    if not response_text:
        return None
    lines = response_text.splitlines()
    for line in reversed(lines):
        for key in available_personas:
            if f"@{key}" in line:
                return key
    return None


def has_conclusion_marker(response_text: str, marker: str) -> bool:
    """응답의 마지막 비공백 줄이 marker로 시작하면 True.

    마커 위치를 마지막 줄로 한정해서, 일반 대화 중 인용·예시로
    [결론] 같은 텍스트가 등장해도 오탐하지 않도록 함.
    """
    if not response_text or not marker:
        return False
    for line in reversed(response_text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith(marker)
    return False
