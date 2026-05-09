"""채널별 webhook 생성/캐시 + 페르소나 username/avatar로 전송.

- channel당 webhook 1개 (페르소나는 send 시 username/avatar override)
- webhook_cache.json에 channel_id -> webhook_id 저장 → 재시작 시 재사용
- 메모리 캐시(_webhooks)로 매번 fetch 안 하도록
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import discord

log = logging.getLogger(__name__)

WEBHOOK_NAME = "claude-bot"


class WebhookError(RuntimeError):
    """webhook 생성/조회/전송 실패."""


class WebhookManager:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._id_cache: dict[int, int] = {}        # channel_id -> webhook_id (디스크 동기화 대상)
        self._wh_cache: dict[int, discord.Webhook] = {}  # channel_id -> Webhook (메모리만)
        self._lock = asyncio.Lock()
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self._id_cache = {int(k): int(v) for k, v in raw.items()}
            log.info("webhook 캐시 로드: %d개", len(self._id_cache))
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log.warning("webhook 캐시 로드 실패, 비우고 재시작: %s", e)
            self._id_cache = {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(
                json.dumps({str(k): v for k, v in self._id_cache.items()}, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("webhook 캐시 저장 실패: %s", e)

    async def get_or_create(self, channel: discord.TextChannel) -> discord.Webhook:
        """채널의 webhook 객체 반환 (없으면 생성)."""
        async with self._lock:
            wh = self._wh_cache.get(channel.id)
            if wh is not None:
                return wh

            cached_id = self._id_cache.get(channel.id)
            if cached_id is not None:
                wh = await self._lookup_existing(channel, cached_id)
                if wh is not None:
                    self._wh_cache[channel.id] = wh
                    return wh
                log.info("캐시된 webhook %d 가 채널에 없음 → 재생성", cached_id)

            try:
                wh = await channel.create_webhook(name=WEBHOOK_NAME)
            except discord.Forbidden as e:
                raise WebhookError(
                    f"webhook 생성 권한 없음 (#{channel.name}): Manage Webhooks 권한 필요"
                ) from e
            except discord.HTTPException as e:
                raise WebhookError(f"webhook 생성 실패 (#{channel.name}): {e}") from e

            self._wh_cache[channel.id] = wh
            self._id_cache[channel.id] = wh.id
            self._save_cache()
            log.info("webhook 신규 생성: channel=%s webhook=%s", channel.id, wh.id)
            return wh

    async def _lookup_existing(
        self, channel: discord.TextChannel, webhook_id: int
    ) -> discord.Webhook | None:
        try:
            for wh in await channel.webhooks():
                if wh.id == webhook_id:
                    return wh
        except discord.Forbidden as e:
            raise WebhookError(
                f"채널 webhook 목록 조회 권한 없음 (#{channel.name})"
            ) from e
        except discord.HTTPException as e:
            log.warning("webhook 목록 조회 실패: %s", e)
        return None

    async def send(
        self,
        channel: discord.TextChannel,
        *,
        content: str,
        username: str,
        avatar_url: str,
        thread: discord.Thread | None = None,
        files: list[discord.File] | None = None,
    ) -> discord.WebhookMessage:
        """webhook으로 메시지 전송. thread가 주어지면 그 스레드 안으로 보냄."""
        wh = await self.get_or_create(channel)
        kwargs = dict(
            content=content,
            username=username,
            avatar_url=avatar_url,
            wait=True,  # WebhookMessage 객체 받기 위해 필요
        )
        if thread is not None:
            kwargs["thread"] = thread
        if files:
            kwargs["files"] = files
        try:
            return await wh.send(**kwargs)
        except discord.HTTPException as e:
            raise WebhookError(f"webhook 전송 실패: {e}") from e

    async def edit(
        self,
        channel: discord.TextChannel,
        message_id: int,
        *,
        content: str,
        thread: discord.Thread | None = None,
        files: list[discord.File] | None = None,
    ) -> discord.WebhookMessage:
        """webhook 메시지 수정. thread 안의 메시지면 thread 인자 필요.
        files 가 주어지면 그 파일들을 첨부로 추가한다."""
        wh = await self.get_or_create(channel)
        kwargs: dict = {"content": content}
        if thread is not None:
            kwargs["thread"] = thread
        if files:
            kwargs["attachments"] = files
        try:
            return await wh.edit_message(message_id, **kwargs)
        except discord.HTTPException as e:
            raise WebhookError(f"webhook 메시지 수정 실패: {e}") from e

    def is_our_webhook(self, webhook_id: int | None) -> bool:
        """주어진 webhook_id가 우리가 만든 webhook인지 (자기-봇 메시지 식별용)."""
        if webhook_id is None:
            return False
        return webhook_id in self._id_cache.values()
