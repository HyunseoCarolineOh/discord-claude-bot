"""Discord × Claude 멀티 에이전트 봇 메인 entry point.

구조: 채널 → 팀 → 직원(페르소나) N명. 사용자가 채널에서 봇을 멘션하면:
- 본문에 @<직원키>가 있으면 그 직원이 응답
- 없으면 team.lead 가 응답

!debate <주제> → 스레드 생성, 팀 멤버끼리 핑퐁.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import discord
from dotenv import load_dotenv

from attachments import extract_referenced_files, render_attachments
from claude_runner import ClaudeRunnerError, ClaudeTimeout, run_claude
from config import AppConfig, ConfigError, PersonaConfig, TeamConfig, load_config
from daily_scheduler import DailyScheduler
from debate import (
    DebateRegistry,
    DebateSession,
    has_conclusion_marker,
    parse_next_speaker,
)
from webhook_manager import WebhookError, WebhookManager

log = logging.getLogger("bot")

PLACEHOLDER = "🤔 생각 중..."
ERROR_PREFIX = "❌ "
WEBHOOK_CACHE_PATH = Path(__file__).parent / "webhook_cache.json"
ATTACHMENT_CACHE_DIR = Path(__file__).parent / "attachment_cache"
THREAD_AUTO_ARCHIVE_MINUTES = 1440  # 24h


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def split_message(text: str, max_len: int) -> list[str]:
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < int(max_len * 0.5):
            cut = max_len
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _bot_role_ids(message: discord.Message) -> set[int]:
    """현재 길드에서 봇이 가진 역할 ID들 (자동 생성된 봇 역할 포함)."""
    if message.guild is None or message.guild.me is None:
        return set()
    return {r.id for r in message.guild.me.roles}


def strip_bot_mentions(
    content: str, bot_user_id: int, bot_role_ids: set[int] | None = None
) -> str:
    content = (
        content.replace(f"<@{bot_user_id}>", "")
        .replace(f"<@!{bot_user_id}>", "")
    )
    for rid in (bot_role_ids or set()):
        content = content.replace(f"<@&{rid}>", "")
    return content.strip()


def should_respond_to_mention(
    message: discord.Message, cfg: AppConfig, bot_user_id: int
) -> bool:
    mode = cfg.bot.trigger_mode
    if mode == "mention":
        # user 멘션 OR 봇 role 멘션 (자동완성에서 둘 다 보일 수 있음)
        user_mentioned = any(u.id == bot_user_id for u in message.mentions)
        if user_mentioned:
            return True
        bot_role_ids = _bot_role_ids(message)
        return any(r.id in bot_role_ids for r in message.role_mentions)
    if mode == "prefix":
        return message.content.lstrip().startswith(cfg.bot.prefix)
    return True


def extract_user_text(
    message: discord.Message, cfg: AppConfig, bot_user_id: int
) -> str:
    content = message.content
    if cfg.bot.trigger_mode == "prefix":
        stripped = content.lstrip()
        if stripped.startswith(cfg.bot.prefix):
            content = stripped[len(cfg.bot.prefix):]
    return strip_bot_mentions(content, bot_user_id, _bot_role_ids(message))


def pick_target_persona(
    user_text: str, team: TeamConfig
) -> str:
    """본문에서 @<key> 멘션 파싱. 없으면 team.lead."""
    target = parse_next_speaker(user_text, list(team.members))
    return target or team.lead


class ClaudeBot(discord.Client):
    def __init__(self, cfg: AppConfig):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.cfg = cfg
        self.webhooks = WebhookManager(WEBHOOK_CACHE_PATH)
        self.debates = DebateRegistry()
        self.daily_scheduler: DailyScheduler | None = None

    def _is_clear_command(self, text: str) -> bool:
        cmd = self.cfg.bot.clear_command
        stripped = text.strip()
        return stripped == cmd or stripped.startswith(cmd + " ")

    def _team_context(self, team: TeamConfig, persona_key: str) -> dict:
        """run_claude에 넘길 팀 메타정보. 사실성 가드에서 유효 페르소나 키
        목록·facts.md 위치를 페르소나에게 알려준다."""
        facts_file = team.project_dir / "facts.md"
        return {
            "team_key": team.key,
            "display_name": team.display_name,
            "members": list(team.members),
            "self_key": persona_key,
            "facts_path": str(facts_file) if facts_file.exists() else None,
        }

    def _is_silent_message(self, content: str) -> bool:
        prefix = self.cfg.bot.silent_prefix
        if not prefix:
            return False
        return content.lstrip().startswith(prefix)

    async def _gather_history_context(
        self,
        channel: discord.abc.Messageable,
        current_message_id: int,
        limit: int,
    ) -> str:
        """채널/스레드 history를 시간순 컨텍스트 텍스트로 변환.

        - current_message_id 와 같은 메시지(지금 처리 중)는 제외
        - clear_command 로 시작하는 메시지를 만나면 그 이전은 잘라냄
        - 첨부도 render_attachments 로 함께 복원
        - history_max_chars 초과 시 앞부분부터 잘라냄
        반환값은 빈 문자열이거나 trailing \\n\\n 으로 끝나는 블록.
        """
        clear_cmd = self.cfg.bot.clear_command
        max_chars = self.cfg.bot.history_max_chars

        history_lines: list[str] = []
        truncated = False
        try:
            async for msg in channel.history(limit=limit, oldest_first=False):
                if msg.id == current_message_id:
                    continue
                text = msg.content.strip()
                if text == clear_cmd or text.startswith(clear_cmd + " "):
                    truncated = True
                    break
                attach_block = await render_attachments(msg, ATTACHMENT_CACHE_DIR)
                if not text and not attach_block:
                    continue
                speaker = self._identify_speaker(msg)
                line = f"[{speaker}]: {text}" if text else f"[{speaker}]:"
                if attach_block:
                    line += attach_block
                history_lines.append(line)
        except discord.HTTPException as e:
            log.warning("history 조회 실패: %s", e)
            return ""

        history_lines.reverse()

        total_len = sum(len(line) + 1 for line in history_lines)
        while total_len > max_chars and len(history_lines) > 3:
            removed = history_lines.pop(0)
            total_len -= len(removed) + 1
            truncated = True
        if truncated:
            history_lines.insert(0, "[(이전 발언 일부 생략)]")

        if not history_lines:
            return ""
        block = "\n".join(history_lines)
        return f"=== 이 대화의 이전 메시지 (시간 순) ===\n{block}\n=== 이전 끝 ===\n\n"

    async def on_ready(self) -> None:
        log.info(
            "로그인 완료: %s (id=%s) | 팀 %d개 | 채널 %d개 | trigger=%s",
            self.user, self.user.id if self.user else "?",
            len(self.cfg.teams), len(self.cfg.channels), self.cfg.bot.trigger_mode,
        )
        for tk, t in self.cfg.teams.items():
            log.info(
                "  팀 %s (%s): lead=%s, members=%s",
                tk, t.display_name, t.lead, list(t.members),
            )

        # te-sales daily 자동 트리거: 매일 KST 08:00 + 봇 기동 시 catch-up.
        # 재연결로 on_ready 가 다시 불려도 1회만 셋업.
        if self.daily_scheduler is None:
            self.daily_scheduler = DailyScheduler(self)
            self.daily_scheduler.start()
            # catch-up 은 fire-and-forget — on_ready 를 막지 않음.
            self.loop.create_task(self.daily_scheduler.maybe_catch_up())

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return

        # silent prefix: 이 메시지 하나는 클로드가 응답하지 않음 (history에는 남음)
        if self._is_silent_message(message.content):
            return

        # 스레드 분기
        if isinstance(message.channel, discord.Thread):
            session = self.debates.get(message.channel.id)
            if session is not None and not session.ended:
                # 활성 토론 등록된 스레드 → 토론 흐름
                await self._handle_thread_message(message, session)
                return
            # 일반 스레드(또는 종료된 토론 스레드) → 단발 응답 + !debate 가능
            await self._handle_plain_thread_message(message)
            return

        ch_cfg = self.cfg.channel(message.channel.id)
        if ch_cfg is None:
            return
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return
        team = self.cfg.teams[ch_cfg.team_key]

        text_stripped = message.content.lstrip()

        # !debate <주제>
        if text_stripped.startswith(self.cfg.bot.debate.command_prefix):
            topic = text_stripped[len(self.cfg.bot.debate.command_prefix):].strip()
            if not topic:
                await channel.send(
                    f"{ERROR_PREFIX}토론 주제를 적어주세요. 예: `{self.cfg.bot.debate.command_prefix} 검색 ML 도입`"
                )
                return
            if len(team.members) < 2:
                await channel.send(
                    f"{ERROR_PREFIX}'{team.display_name}' 팀은 멤버가 1명이라 토론할 상대가 없어요."
                )
                return
            await self._start_debate(channel, message, topic, team)
            return

        # 단발 응답
        if not should_respond_to_mention(message, self.cfg, self.user.id):
            return
        user_text = extract_user_text(message, self.cfg, self.user.id)

        # /clear: 컨텍스트 경계 표시. 응답 대신 리액션만.
        if self._is_clear_command(user_text):
            try:
                await message.add_reaction("🧹")
            except discord.HTTPException as e:
                log.warning("clear 리액션 실패: %s", e)
            return

        attach_block = await render_attachments(message, ATTACHMENT_CACHE_DIR)
        combined_text = (user_text + attach_block).strip()
        if not combined_text:
            return

        history_block = await self._gather_history_context(
            channel, message.id, self.cfg.bot.history_limit_channel,
        )
        full_message = (
            f"{history_block}=== 사용자 새 메시지 ===\n{combined_text}"
            if history_block else combined_text
        )

        # 페르소나 선택은 본문 텍스트(@key 멘션) 기준
        target_key = pick_target_persona(user_text, team)

        # 사용자 메시지에 스레드를 만들고 그 안에 답변한다.
        thread_name = (user_text.strip()[:60] or "대화")
        try:
            reply_thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
            )
        except discord.Forbidden:
            await channel.send(
                f"{ERROR_PREFIX}스레드 생성 권한이 없습니다. (Manage Threads / Create Public Threads 확인)"
            )
            return
        except discord.HTTPException as e:
            await channel.send(f"{ERROR_PREFIX}스레드 생성 실패: {e}")
            return

        await self._single_response(
            channel, team, target_key, full_message, thread=reply_thread
        )

    async def _handle_plain_thread_message(self, message: discord.Message) -> None:
        """!debate로 만든 토론 스레드가 아닌, 사용자가 직접 만든 일반 스레드에서의 단발 응답."""
        thread = message.channel
        if not isinstance(thread, discord.Thread):
            return
        parent = thread.parent
        if parent is None or not isinstance(parent, discord.TextChannel):
            return
        ch_cfg = self.cfg.channel(parent.id)
        if ch_cfg is None:
            return  # 부모 채널이 매핑되지 않음

        team = self.cfg.teams[ch_cfg.team_key]

        # 스레드 안에서 !debate → 그 스레드 자체를 토론 세션으로 등록
        text_stripped = message.content.lstrip()
        if text_stripped.startswith(self.cfg.bot.debate.command_prefix):
            topic = text_stripped[len(self.cfg.bot.debate.command_prefix):].strip()
            if not topic:
                await thread.send(
                    f"{ERROR_PREFIX}토론 주제를 적어주세요. 예: `{self.cfg.bot.debate.command_prefix} 검색 ML 도입`"
                )
                return
            if len(team.members) < 2:
                await thread.send(
                    f"{ERROR_PREFIX}'{team.display_name}' 팀은 멤버가 1명이라 토론할 상대가 없어요."
                )
                return
            await self._start_debate_in_thread(thread, parent, topic, team)
            return

        if not should_respond_to_mention(message, self.cfg, self.user.id):
            return
        user_text = extract_user_text(message, self.cfg, self.user.id)

        if self._is_clear_command(user_text):
            try:
                await message.add_reaction("🧹")
            except discord.HTTPException as e:
                log.warning("clear 리액션 실패: %s", e)
            return

        attach_block = await render_attachments(message, ATTACHMENT_CACHE_DIR)
        combined_text = (user_text + attach_block).strip()
        if not combined_text:
            return

        history_block = await self._gather_history_context(
            thread, message.id, self.cfg.bot.history_limit_thread,
        )
        full_message = (
            f"{history_block}=== 사용자 새 메시지 ===\n{combined_text}"
            if history_block else combined_text
        )

        target_key = pick_target_persona(user_text, team)
        await self._single_response(parent, team, target_key, full_message, thread=thread)

    # --- 단발 응답 ---
    async def _single_response(
        self,
        channel: discord.TextChannel,
        team: TeamConfig,
        persona_key: str,
        user_text: str,
        *,
        thread: discord.Thread | None = None,
    ) -> None:
        persona = self.cfg.personas[persona_key]
        log.info(
            "단발 요청: channel=%s thread=%s team=%s persona=%s len=%d",
            channel.id, thread.id if thread else "-", team.key, persona_key, len(user_text),
        )

        try:
            placeholder = await self.webhooks.send(
                channel,
                content=PLACEHOLDER,
                username=persona.display_name,
                avatar_url=persona.avatar_url,
                thread=thread,
            )
        except WebhookError as e:
            log.error("placeholder 전송 실패: %s", e)
            target = thread if thread is not None else channel
            try:
                await target.send(f"{ERROR_PREFIX}{e}")
            except discord.HTTPException:
                pass
            return

        started = time.monotonic()
        try:
            result = await run_claude(
                message=user_text,
                system_prompt=persona.read_prompt(),
                project_dir=team.project_dir,
                timeout=self.cfg.bot.claude_timeout,
                team_context=self._team_context(team, persona_key),
            )
            response_text = result.text
            log.info(
                "단발 완료: persona=%s elapsed=%.1fs",
                persona_key, time.monotonic() - started,
            )
        except ClaudeTimeout:
            response_text = f"{ERROR_PREFIX}claude 응답이 {self.cfg.bot.claude_timeout}초 내에 오지 않았어요."
        except ClaudeRunnerError as e:
            response_text = f"{ERROR_PREFIX}claude 실행 오류: {e}"
        except Exception as e:  # noqa: BLE001
            response_text = f"{ERROR_PREFIX}예상치 못한 오류: {e}"
            log.exception("unexpected (persona=%s)", persona_key)

        await self._post_chunks(
            channel, persona, placeholder.id, response_text,
            thread=thread, project_dir=team.project_dir,
        )

        # 일반 스레드 자동 dispatch: 페르소나 응답에 @<key> 멘션이 있으면 다음 페르소나 호출.
        if (
            thread is not None
            and not response_text.startswith(ERROR_PREFIX)
        ):
            next_key = parse_next_speaker(response_text, list(team.members))
            if next_key and next_key != persona_key:
                remaining = self.cfg.bot.thread_chain_max_speeches - 1
                if remaining > 0:
                    await self._chain_dispatch(channel, team, next_key, thread, remaining)

    async def _chain_dispatch(
        self,
        channel: discord.TextChannel,
        team: TeamConfig,
        persona_key: str,
        thread: discord.Thread,
        remaining: int,
    ) -> None:
        """일반 스레드 자동 핑퐁: 페르소나 → 페르소나.
        remaining = 앞으로 더 발화 가능한 횟수 (안전망).
        """
        if remaining <= 0:
            log.info("스레드 chain 한도 도달: thread=%s", thread.id)
            return
        persona = self.cfg.personas.get(persona_key)
        if persona is None:
            log.warning("chain dispatch: 알 수 없는 페르소나 %s", persona_key)
            return
        log.info(
            "스레드 chain 발화: thread=%s persona=%s remaining=%d",
            thread.id, persona_key, remaining,
        )

        try:
            placeholder = await self.webhooks.send(
                channel,
                content=PLACEHOLDER,
                username=persona.display_name,
                avatar_url=persona.avatar_url,
                thread=thread,
            )
        except WebhookError as e:
            log.error("chain placeholder 전송 실패: %s", e)
            return

        history_block = await self._gather_history_context(
            thread, placeholder.id, self.cfg.bot.history_limit_thread,
        )
        other_members = [k for k in team.members if k != persona_key]
        available_keys = ", ".join(f"@{k}" for k in other_members) or "(없음)"
        full_message = (
            f"{history_block}=== 당신 차례 ===\n"
            f"위 대화에서 이전 발화자가 당신({persona.display_name})을 @멘션했습니다. "
            f"맥락에 맞게 **짧게** 응답하세요. "
            f"다른 팀원 의견이 더 필요하면 마지막 줄에 {available_keys} 중 하나를 @멘션, "
            f"아니면 멘션 없이 끝내면 됩니다."
        )

        started = time.monotonic()
        try:
            result = await run_claude(
                message=full_message,
                system_prompt=persona.read_prompt(),
                project_dir=team.project_dir,
                timeout=self.cfg.bot.claude_timeout,
                team_context=self._team_context(team, persona_key),
            )
            response_text = result.text
            log.info(
                "스레드 chain 응답: persona=%s elapsed=%.1fs",
                persona_key, time.monotonic() - started,
            )
        except ClaudeTimeout:
            response_text = (
                f"{ERROR_PREFIX}claude 응답이 {self.cfg.bot.claude_timeout}초 내에 오지 않았어요."
            )
        except ClaudeRunnerError as e:
            response_text = f"{ERROR_PREFIX}claude 실행 오류: {e}"
        except Exception as e:  # noqa: BLE001
            response_text = f"{ERROR_PREFIX}예상치 못한 오류: {e}"
            log.exception("unexpected (chain persona=%s)", persona_key)

        await self._post_chunks(
            channel, persona, placeholder.id, response_text,
            thread=thread, project_dir=team.project_dir,
        )

        if response_text.startswith(ERROR_PREFIX):
            return
        next_key = parse_next_speaker(response_text, list(team.members))
        if next_key is None or next_key == persona_key:
            return
        await self._chain_dispatch(channel, team, next_key, thread, remaining - 1)

    async def _post_chunks(
        self,
        channel: discord.TextChannel,
        persona: PersonaConfig,
        placeholder_id: int,
        text: str,
        *,
        thread: discord.Thread | None = None,
        project_dir: Path | None = None,
    ) -> None:
        target_channel = thread.parent if thread is not None else channel

        # 응답 본문 백틱 경로 → 디스코드 첨부. 에러 응답엔 첨부 시도 안 함.
        files: list[discord.File] = []
        if project_dir is not None and not text.startswith(ERROR_PREFIX):
            paths, warnings = extract_referenced_files(text, project_dir)
            for p in paths:
                try:
                    files.append(discord.File(str(p), filename=p.name))
                except OSError as e:
                    log.warning("첨부 파일 열기 실패 %s: %s", p, e)
            if warnings:
                text = text + "\n\n" + "\n".join(warnings)

        chunks = split_message(text, self.cfg.bot.max_response_length)
        try:
            await self.webhooks.edit(
                target_channel, placeholder_id,
                content=chunks[0], thread=thread,
                files=files or None,
            )
            for chunk in chunks[1:]:
                await self.webhooks.send(
                    target_channel,
                    content=chunk,
                    username=persona.display_name,
                    avatar_url=persona.avatar_url,
                    thread=thread,
                )
        except WebhookError as e:
            log.error("응답 전송 실패: %s", e)
        finally:
            for f in files:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass

    # --- 토론 시작 ---
    async def _start_debate(
        self,
        channel: discord.TextChannel,
        trigger_message: discord.Message,
        topic: str,
        team: TeamConfig,
    ) -> None:
        log.info("토론 시작: channel=%s team=%s topic=%r", channel.id, team.key, topic[:60])
        try:
            thread = await trigger_message.create_thread(
                name=topic[:60] or "토론",
                auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
            )
        except discord.Forbidden:
            await channel.send(
                f"{ERROR_PREFIX}스레드 생성 권한이 없습니다. (Manage Threads / Create Public Threads 확인)"
            )
            return
        except discord.HTTPException as e:
            await channel.send(f"{ERROR_PREFIX}스레드 생성 실패: {e}")
            return

        session = self.debates.start(
            thread_id=thread.id,
            topic=topic,
            parent_channel_id=channel.id,
            first_speaker=team.lead,
        )
        log.info("토론 세션: thread=%s lead=%s", thread.id, team.lead)
        await self._dispatch_speaker(thread, session, team.lead)

    async def _start_debate_in_thread(
        self,
        thread: discord.Thread,
        parent: discord.TextChannel,
        topic: str,
        team: TeamConfig,
    ) -> None:
        """이미 존재하는 스레드를 토론 세션으로 등록 (새 스레드 생성 X).

        ended 세션이 남아있으면 제거 후 새 주제로 갱신.
        """
        log.info(
            "토론 시작(스레드 내): thread=%s team=%s topic=%r",
            thread.id, team.key, topic[:60],
        )
        if thread.id in self.debates:
            self.debates.remove(thread.id)
        session = self.debates.start(
            thread_id=thread.id,
            topic=topic,
            parent_channel_id=parent.id,
            first_speaker=team.lead,
        )
        try:
            await thread.send(f"🎬 토론 시작: **{topic}**")
        except discord.HTTPException as e:
            log.warning("토론 시작 안내 전송 실패: %s", e)
        await self._dispatch_speaker(thread, session, team.lead)

    # --- 토론 스레드 안 메시지 ---
    async def _handle_thread_message(
        self, message: discord.Message, session: DebateSession
    ) -> None:
        if session.ended:
            return
        if self.webhooks.is_our_webhook(message.webhook_id):
            return  # 페르소나 자체 발화

        thread = message.channel
        if not isinstance(thread, discord.Thread):
            return

        team = self.cfg.team_for_channel(session.parent_channel_id)
        if team is None:
            return

        text = message.content.strip()

        if text.startswith(self.cfg.bot.debate.end_command):
            await self._end_debate(thread, session, "user_command",
                                   "사용자가 토론을 종료했습니다.")
            return

        next_speaker = parse_next_speaker(text, list(team.members))
        if next_speaker is None:
            return  # 명시 멘션 없으면 history에만 남기고 대기
        await self._dispatch_speaker(thread, session, next_speaker)

    # --- 페르소나 발화 + 핑퐁 ---
    async def _dispatch_speaker(
        self,
        thread: discord.Thread,
        session: DebateSession,
        persona_key: str,
    ) -> None:
        if session.ended:
            return

        team = self.cfg.team_for_channel(session.parent_channel_id)
        if team is None:
            await self._end_debate(thread, session, "config_error",
                                   f"{ERROR_PREFIX}부모 채널 매핑이 사라졌습니다.")
            return
        available = list(team.members)
        debate_cfg = self.cfg.bot.debate

        if session.is_at_safety_limit(debate_cfg.max_total_speeches):
            await self._end_debate(
                thread, session, "max_total_speeches",
                f"누적 발화 {debate_cfg.max_total_speeches}회 도달 — 안전망으로 토론 종료.",
            )
            return

        persona = self.cfg.personas[persona_key]
        session.current_speaker = persona_key
        log.info(
            "토론 발화: thread=%s persona=%s round=%d",
            thread.id, persona_key, session.round_counts.get(persona_key, 0) + 1,
        )

        try:
            placeholder = await self.webhooks.send(
                thread.parent,
                content=PLACEHOLDER,
                username=persona.display_name,
                avatar_url=persona.avatar_url,
                thread=thread,
            )
        except WebhookError as e:
            log.error("placeholder 전송 실패: %s", e)
            return

        try:
            combined_message = await self._build_thread_prompt(thread, session, persona, team)
        except discord.HTTPException as e:
            log.error("스레드 history 조회 실패: %s", e)
            await self.webhooks.edit(
                thread.parent, placeholder.id,
                content=f"{ERROR_PREFIX}스레드 히스토리 조회 실패: {e}",
                thread=thread,
            )
            return

        started = time.monotonic()
        try:
            result = await run_claude(
                message=combined_message,
                system_prompt=persona.read_prompt(),
                project_dir=team.project_dir,
                timeout=self.cfg.bot.claude_timeout,
                team_context=self._team_context(team, persona_key),
            )
            response_text = result.text
            log.info(
                "토론 응답: thread=%s persona=%s elapsed=%.1fs",
                thread.id, persona_key, time.monotonic() - started,
            )
        except ClaudeTimeout:
            response_text = f"{ERROR_PREFIX}응답 timeout"
        except ClaudeRunnerError as e:
            response_text = f"{ERROR_PREFIX}claude 오류: {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("unexpected during debate dispatch")
            response_text = f"{ERROR_PREFIX}예상치 못한 오류: {e}"

        await self._post_chunks(
            thread.parent, persona, placeholder.id, response_text,
            thread=thread, project_dir=team.project_dir,
        )
        session.record_speech(persona_key)

        # 1차: 결론 마커 (자연 종료)
        if has_conclusion_marker(response_text, debate_cfg.conclusion_marker):
            await self._end_debate(
                thread, session, "concluded",
                f"{persona.display_name}가 결론을 제시해 토론을 마칩니다.",
            )
            return

        # 2차: 누적 발화 안전망
        if session.is_at_safety_limit(debate_cfg.max_total_speeches):
            await self._end_debate(
                thread, session, "max_total_speeches",
                f"누적 발화 {debate_cfg.max_total_speeches}회 도달 — 안전망으로 토론 종료.",
            )
            return

        next_key = parse_next_speaker(response_text, available)
        if next_key is None:
            log.info("토론 일시정지 (멘션 없음): thread=%s", thread.id)
            return
        if next_key == persona_key:
            log.info("자기 자신 멘션 무시: thread=%s persona=%s", thread.id, persona_key)
            return

        await self._dispatch_speaker(thread, session, next_key)

    async def _build_thread_prompt(
        self,
        thread: discord.Thread,
        session: DebateSession,
        persona: PersonaConfig,
        team: TeamConfig,
    ) -> str:
        history_lines: list[str] = []
        async for msg in thread.history(limit=60, oldest_first=True):
            speaker = self._identify_speaker(msg)
            content = msg.content.strip()
            attach_block = await render_attachments(msg, ATTACHMENT_CACHE_DIR)
            if not content and not attach_block:
                continue
            line = f"[{speaker}]: {content}" if content else f"[{speaker}]:"
            if attach_block:
                line += attach_block
            history_lines.append(line)

        max_history_chars = 50000
        total_len = sum(len(line) + 1 for line in history_lines)
        truncated = False
        while total_len > max_history_chars and len(history_lines) > 5:
            removed = history_lines.pop(0)
            total_len -= len(removed) + 1
            truncated = True
        if truncated:
            history_lines.insert(0, "[(이전 발언 일부 생략)]")

        history_block = "\n".join(history_lines) or "(아직 발언 없음)"
        other_members = [k for k in team.members if k != persona.key]
        available_keys = ", ".join(f"@{k}" for k in other_members) or "(다른 팀원 없음)"
        marker = self.cfg.bot.debate.conclusion_marker

        return (
            f"=== 팀 ===\n{team.display_name} (당신은 {persona.display_name})\n\n"
            f"=== 토론 주제 ===\n{session.topic}\n\n"
            f"=== 이전 발언 (시간 순) ===\n{history_block}\n\n"
            f"=== 당신의 차례 ===\n"
            f"위 맥락을 바탕으로 **한 단락(3~5줄)으로 짧게** 발언하세요. "
            f"동의면 동의 한 줄 + 근거 1개, 반대면 반대 한 줄 + 근거 1개. 에세이·긴 분석 금지.\n\n"
            f"**다음 동작은 셋 중 하나를 고르세요**:\n"
            f"1. 토론 계속: 응답 마지막 줄에 {available_keys} 중 한 명을 @멘션 → 그가 이어 발언.\n"
            f"2. 사용자 의견 대기: 멘션 없이 끝내면 일시정지되고 사용자 입력을 기다립니다.\n"
            f"3. 토론 종료: 충분히 합의됐다고 판단되면 응답의 **마지막 줄**을 "
            f"`{marker} 한 줄 요약` 형태로 작성하세요. 그러면 토론이 마무리됩니다. "
            f"이 마커는 진짜 결론일 때만 쓰고, 인용·예시로는 절대 쓰지 마세요."
        )

    def _identify_speaker(self, message: discord.Message) -> str:
        if message.webhook_id is None:
            return f"사용자 {message.author.display_name}"
        for persona in self.cfg.personas.values():
            if message.author.name == persona.display_name:
                return persona.display_name
        return f"webhook({message.author.name})"

    async def _end_debate(
        self,
        thread: discord.Thread,
        session: DebateSession,
        reason: str,
        notice: str,
    ) -> None:
        if session.ended:
            return
        session.end(reason)
        log.info("토론 종료: thread=%s reason=%s", thread.id, reason)
        try:
            await thread.send(f"🔚 {notice}")
        except discord.HTTPException as e:
            log.warning("종료 안내 전송 실패: %s", e)
        try:
            await thread.edit(archived=True)
        except discord.HTTPException as e:
            log.warning("스레드 archive 실패: %s", e)


def main() -> None:
    load_dotenv()
    setup_logging()

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN 환경변수가 설정되지 않았습니다. .env 파일을 확인하세요."
        )

    try:
        cfg = load_config("config.yaml")
    except ConfigError as e:
        raise SystemExit(f"설정 오류: {e}") from e

    bot = ClaudeBot(cfg)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
