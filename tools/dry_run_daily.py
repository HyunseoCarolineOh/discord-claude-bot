"""TE Sales daily 트리거 dry-run 유틸리티.

실제 08:00 까지 안 기다리고 강제로 1회 발사 — staging 검증용.
멱등 체크는 우회하지만 last_run.db 에는 기록되지 않음(고립 실행).

사용:
    .venv\\Scripts\\python.exe tools\\dry_run_daily.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# repo root 를 path 에 추가 (이 스크립트는 tools\ 하위에 있음)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from bot import ClaudeBot, setup_logging  # noqa: E402
from config import load_config  # noqa: E402
from daily_scheduler import DailyScheduler, _today_kst  # noqa: E402

log = logging.getLogger("dry_run_daily")


async def _go() -> None:
    cfg = load_config(ROOT / "config.yaml")
    bot = ClaudeBot(cfg)

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN 미설정")

    fired = asyncio.Event()

    @bot.event
    async def on_ready() -> None:  # noqa: ANN001
        log.info("dry-run on_ready: %s", bot.user)
        scheduler = DailyScheduler(bot)
        try:
            await scheduler._do_run(_today_kst(), catch_up=False)
            log.info("DRY-RUN 성공")
        except Exception:
            log.exception("DRY-RUN 실패")
        finally:
            fired.set()
            await bot.close()

    async with bot:
        bot_task = asyncio.create_task(bot.start(token))
        try:
            await asyncio.wait_for(fired.wait(), timeout=300)
        except asyncio.TimeoutError:
            log.error("dry-run timeout — 봇이 ready 못 들어옴")
        finally:
            if not bot_task.done():
                bot_task.cancel()
            try:
                await bot_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def main() -> None:
    load_dotenv()
    setup_logging()
    asyncio.run(_go())


if __name__ == "__main__":
    main()
