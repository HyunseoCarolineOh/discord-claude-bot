"""TE Sales daily 자동 트리거.

매일 KST 08:00에 te-sales 채널에 daily 스레드를 만들고
sales_manager 페르소나가 daily를 시작한다.

- last_run.db (sqlite) 로 멱등성 보장 — 하루 1회만 실행
- catch-up: 봇 시동 시 당일 미실행 + 08:00 KST 경과 → 즉시 1회 실행
- Healthchecks.io: HEALTHCHECKS_DAILY_URL 환경변수 설정 시 success/fail 핑
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import tasks

if TYPE_CHECKING:
    from bot import ClaudeBot

log = logging.getLogger("daily_scheduler")

KST = timezone(timedelta(hours=9))
DAILY_TIME_KST = dt_time(hour=8, minute=0, tzinfo=KST)
TE_SALES_CHANNEL_ID = 1502173593382027415
TE_SALES_TEAM_KEY = "te-sales"
DAILY_TITLE_PREFIX = "📊 Daily"
DB_PATH = Path(__file__).parent / "last_run.db"

# 페르소나 호출 체인 (sales_manager → 이 순서대로 → 종료)
PERSONA_CHAIN = ["am", "leadminer", "content", "te_ops"]


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _now_iso_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


class DailyRunStore:
    """daily 실행 이력 sqlite 저장소. 멱등성·catch-up 판단 근거."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _init_schema(self) -> None:
        with self._connect() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_runs (
                    run_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    ran_at TEXT NOT NULL,
                    catch_up INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def has_success(self, date_str: str) -> bool:
        with self._connect() as c:
            row = c.execute(
                "SELECT 1 FROM daily_runs WHERE run_date = ? AND status = 'success'",
                (date_str,),
            ).fetchone()
            return row is not None

    def record(self, date_str: str, status: str, *, catch_up: bool) -> None:
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO daily_runs (run_date, status, ran_at, catch_up)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_date) DO UPDATE SET
                    status = excluded.status,
                    ran_at = excluded.ran_at,
                    catch_up = excluded.catch_up
                """,
                (date_str, status, _now_iso_kst(), 1 if catch_up else 0),
            )

    def last(self) -> tuple[str, str, str, int] | None:
        with self._connect() as c:
            return c.execute(
                "SELECT run_date, status, ran_at, catch_up FROM daily_runs "
                "ORDER BY run_date DESC LIMIT 1"
            ).fetchone()


def build_daily_prompt(today: str, *, catch_up: bool) -> str:
    chain_str = " → ".join(f"@{k}" for k in PERSONA_CHAIN)
    note = "  (어제·오늘 분 catch-up 실행)" if catch_up else ""
    return (
        f"=== 자동 daily 트리거 ==={note}\n"
        f"오늘 날짜: {today} (KST 08:00 자동 실행)\n\n"
        f"당신(Sales Manager)이 daily 스레드를 엽니다. 다음 항목을 짧게 보고:\n"
        f"1) 📊 파이프라인 Stage별 건수 — Qualified·Proposal·Negotiation·closed_won·closed_lost\n"
        f"2) 🚧 병목 Stage + 정체 건수\n"
        f"3) ⏰ Overdue 건수\n"
        f"4) 오늘 우선순위 1줄\n"
        f"5) 마지막 줄에 `@am` 멘션으로 본인 영역 daily 보고를 요청\n\n"
        f"=== 페르소나 호출 체인 ===\n"
        f"sales_manager(당신) → {chain_str} → (te_ops 마지막, 멘션 없이 종료)\n"
        f"각 페르소나는 본인 daily 책임(SOT v1.0) 항목만 3~5줄로 보고하고 "
        f"체인 순서대로 다음 사람을 마지막 줄에 @멘션. 데이터 미수집은 `null`, "
        f"해당 없음은 `0`으로 표기.\n\n"
        f"실데이터 조회 권한이 아직 안 붙었으면 placeholder(`-` 또는 `null`)로 "
        f"채우고 그 사실을 1줄로 명시하세요. 절대 가짜 숫자 만들지 마세요."
    )


class DailyScheduler:
    """`discord.ext.tasks.loop(time=...)` 기반 daily 스케줄러."""

    def __init__(self, bot: "ClaudeBot") -> None:
        self.bot = bot
        self.store = DailyRunStore()
        self.healthcheck_url = (os.getenv("HEALTHCHECKS_DAILY_URL", "") or "").strip() or None
        # `tasks.loop` 데코레이터를 인스턴스 메서드에 동적으로 적용
        self._loop = tasks.loop(time=DAILY_TIME_KST)(self._tick)

    def start(self) -> None:
        if self._loop.is_running():
            return
        self._loop.start()
        log.info(
            "daily_scheduler 시작: 매일 KST 08:00 (channel=%s)",
            TE_SALES_CHANNEL_ID,
        )

    def stop(self) -> None:
        if self._loop.is_running():
            self._loop.cancel()

    async def _tick(self) -> None:
        today = _today_kst()
        if self.store.has_success(today):
            log.info("daily 이미 실행됨: %s — skip", today)
            return
        await self._run(today, catch_up=False)

    async def maybe_catch_up(self) -> None:
        """봇 기동 직후 호출: 당일 미실행 + 08:00 경과면 즉시 1회 발사."""
        now = datetime.now(KST)
        today = now.strftime("%Y-%m-%d")
        if self.store.has_success(today):
            log.info("catch-up 불필요: %s 이미 성공", today)
            return
        if now.time() < dt_time(hour=8, minute=0):
            log.info("catch-up 불필요: 08:00 KST 이전 (now=%s)", now.time().isoformat(timespec="seconds"))
            return
        log.info("catch-up 발동: %s 미실행 + 08:00 경과", today)
        await self._run(today, catch_up=True)

    async def _run(self, today: str, *, catch_up: bool) -> None:
        try:
            await self._do_run(today, catch_up=catch_up)
            self.store.record(today, "success", catch_up=catch_up)
            await self._ping_healthcheck(success=True)
        except Exception as e:  # noqa: BLE001
            log.exception("daily 실행 실패: %s", e)
            self.store.record(today, "fail", catch_up=catch_up)
            await self._ping_healthcheck(success=False)

    async def _do_run(self, today: str, *, catch_up: bool) -> None:
        channel = self.bot.get_channel(TE_SALES_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(TE_SALES_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError(
                f"te-sales 채널이 TextChannel 이 아님: {type(channel).__name__}"
            )
        team = self.bot.cfg.teams.get(TE_SALES_TEAM_KEY)
        if team is None:
            raise RuntimeError(f"team '{TE_SALES_TEAM_KEY}' 미정의")

        title = f"{DAILY_TITLE_PREFIX} {today}"
        existing = self._find_existing_thread(channel, title)
        if existing is not None:
            log.info("이미 같은 제목 스레드 존재: %s — skip 생성", existing.id)
            return

        opening = title + (" (catch-up)" if catch_up else "")
        starter = await channel.send(opening)
        thread = await starter.create_thread(
            name=title,
            auto_archive_duration=1440,
        )
        log.info("daily 스레드 생성: thread=%s title=%r catch_up=%s", thread.id, title, catch_up)

        prompt = build_daily_prompt(today, catch_up=catch_up)
        # bot._single_response 의 chain 자동 dispatch 가 페르소나 체인을 이어준다.
        await self.bot._single_response(
            channel, team, "sales_manager", prompt, thread=thread,
        )

    @staticmethod
    def _find_existing_thread(
        channel: discord.TextChannel, title: str
    ) -> discord.Thread | None:
        for t in channel.threads:
            if t.name == title:
                return t
        return None

    async def _ping_healthcheck(self, *, success: bool) -> None:
        if not self.healthcheck_url:
            return
        url = self.healthcheck_url if success else f"{self.healthcheck_url.rstrip('/')}/fail"
        try:
            import aiohttp  # discord.py 의존성으로 이미 설치되어 있음
        except ImportError:
            log.warning("aiohttp 미설치 — Healthcheck 핑 스킵")
            return
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    log.info("Healthcheck ping: %s -> HTTP %d", url, resp.status)
        except Exception as e:  # noqa: BLE001
            log.warning("Healthcheck ping 실패: %s", e)


async def run_dry_run(bot: "ClaudeBot") -> None:
    """수동 dry-run 트리거 — 멱등 무시하고 강제 1회 실행 (테스트용)."""
    scheduler = DailyScheduler(bot)
    today = _today_kst()
    log.info("DRY-RUN 시작: %s", today)
    await scheduler._do_run(today, catch_up=False)
    log.info("DRY-RUN 완료")
