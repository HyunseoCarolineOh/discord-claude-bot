"""config.yaml 로드 및 검증.

구조: channel -> team -> members(N persona). 한 채널은 한 팀에 매핑되고,
팀 안의 여러 페르소나(직원)가 그 채널에서 활동함. 팀에는 lead가 있어
직원 미지정 멘션 시 lead가 응답.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

TriggerMode = Literal["mention", "prefix", "all_messages"]


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PersonaConfig:
    key: str
    display_name: str
    avatar_url: str
    prompt_file: Path

    def read_prompt(self) -> str:
        return self.prompt_file.read_text(encoding="utf-8")


@dataclass(frozen=True)
class TeamConfig:
    key: str
    display_name: str
    project_dir: Path
    members: tuple[str, ...]   # 페르소나 키 목록
    lead: str                  # 페르소나 키 — members에 포함되어야 함


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: int
    team_key: str


@dataclass(frozen=True)
class DebateConfig:
    max_total_speeches: int = 30  # 안전망: 토론 전체 누적 발화 수 한도
    conclusion_marker: str = "[결론]"  # 응답 마지막 줄이 이걸로 시작하면 종료
    command_prefix: str = "!debate"
    end_command: str = "!end"


@dataclass(frozen=True)
class BotConfig:
    trigger_mode: TriggerMode = "mention"
    prefix: str = "!claude"
    silent_prefix: str = "//"
    max_response_length: int = 1900
    claude_timeout: int = 300
    debate: DebateConfig = field(default_factory=DebateConfig)
    clear_command: str = "/clear"
    history_limit_channel: int = 30
    history_limit_thread: int = 60
    history_max_chars: int = 50000
    thread_chain_max_speeches: int = 30  # 일반 스레드에서 페르소나끼리 자동 핑퐁 누적 발화 한도


@dataclass(frozen=True)
class AppConfig:
    personas: dict[str, PersonaConfig]
    teams: dict[str, TeamConfig]
    channels: dict[int, ChannelConfig]
    bot: BotConfig
    base_dir: Path = field(default_factory=Path.cwd)

    def channel(self, channel_id: int) -> ChannelConfig | None:
        return self.channels.get(channel_id)

    def team_for_channel(self, channel_id: int) -> TeamConfig | None:
        ch = self.channel(channel_id)
        if ch is None:
            return None
        return self.teams.get(ch.team_key)

    def lead_for_channel(self, channel_id: int) -> PersonaConfig | None:
        team = self.team_for_channel(channel_id)
        if team is None:
            return None
        return self.personas.get(team.lead)

    def members_for_channel(self, channel_id: int) -> list[PersonaConfig]:
        team = self.team_for_channel(channel_id)
        if team is None:
            return []
        return [self.personas[k] for k in team.members if k in self.personas]


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    base_dir = cfg_path.parent

    personas = _parse_personas(raw.get("personas"), base_dir)
    teams = _parse_teams(raw.get("teams"), base_dir, personas)
    channels = _parse_channels(raw.get("channels"), teams)
    bot = _parse_bot(raw.get("bot") or {})

    return AppConfig(
        personas=personas, teams=teams, channels=channels,
        bot=bot, base_dir=base_dir,
    )


def _parse_personas(raw: object, base_dir: Path) -> dict[str, PersonaConfig]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("personas 섹션이 비어있거나 형식이 잘못되었습니다.")

    out: dict[str, PersonaConfig] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ConfigError(f"personas.{key} 항목이 매핑이 아닙니다.")
        try:
            display_name = str(value["display_name"])
            avatar_url = str(value["avatar_url"])
            prompt_file_raw = str(value["prompt_file"])
        except KeyError as e:
            raise ConfigError(f"personas.{key}.{e.args[0]} 가 누락되었습니다.") from None

        prompt_path = (base_dir / prompt_file_raw).resolve()
        if not prompt_path.exists():
            raise ConfigError(
                f"personas.{key}.prompt_file 파일이 없습니다: {prompt_path}"
            )

        out[str(key)] = PersonaConfig(
            key=str(key),
            display_name=display_name,
            avatar_url=avatar_url,
            prompt_file=prompt_path,
        )
    return out


def _parse_teams(
    raw: object, base_dir: Path, personas: dict[str, PersonaConfig]
) -> dict[str, TeamConfig]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("teams 섹션이 비어있거나 형식이 잘못되었습니다.")

    out: dict[str, TeamConfig] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ConfigError(f"teams.{key} 항목이 매핑이 아닙니다.")
        try:
            display_name = str(value["display_name"])
            project_dir_raw = str(value["project_dir"])
            members_raw = value["members"]
            lead = str(value["lead"])
        except KeyError as e:
            raise ConfigError(f"teams.{key}.{e.args[0]} 가 누락되었습니다.") from None

        if not isinstance(members_raw, list) or not members_raw:
            raise ConfigError(f"teams.{key}.members 는 비어있지 않은 리스트여야 합니다.")
        members = tuple(str(m) for m in members_raw)

        for m in members:
            if m not in personas:
                raise ConfigError(
                    f"teams.{key}.members 의 '{m}' 가 personas에 정의되어 있지 않습니다."
                )
        if lead not in members:
            raise ConfigError(
                f"teams.{key}.lead='{lead}' 가 members({members}) 에 포함되어 있지 않습니다."
            )

        project_dir = (base_dir / project_dir_raw).resolve()
        if not project_dir.exists():
            raise ConfigError(
                f"teams.{key}.project_dir 디렉토리가 없습니다: {project_dir}"
            )
        if not project_dir.is_dir():
            raise ConfigError(
                f"teams.{key}.project_dir 가 디렉토리가 아닙니다: {project_dir}"
            )

        out[str(key)] = TeamConfig(
            key=str(key),
            display_name=display_name,
            project_dir=project_dir,
            members=members,
            lead=lead,
        )
    return out


def _parse_channels(raw: object, teams: dict[str, TeamConfig]) -> dict[int, ChannelConfig]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("channels 섹션이 비어있거나 형식이 잘못되었습니다.")

    out: dict[int, ChannelConfig] = {}
    for cid, value in raw.items():
        try:
            channel_id = int(str(cid))
        except ValueError:
            raise ConfigError(f"channels 키가 숫자 ID가 아닙니다: {cid!r}") from None

        if not isinstance(value, dict):
            raise ConfigError(f"channels.{cid} 항목이 매핑이 아닙니다.")

        try:
            team_key = str(value["team"])
        except KeyError:
            raise ConfigError(f"channels.{cid}.team 가 누락되었습니다.") from None

        if team_key not in teams:
            raise ConfigError(
                f"channels.{cid}.team='{team_key}' 가 teams에 정의되어 있지 않습니다."
            )

        out[channel_id] = ChannelConfig(channel_id=channel_id, team_key=team_key)
    return out


def _parse_bot(raw: dict) -> BotConfig:
    trigger_mode = raw.get("trigger_mode", "mention")
    if trigger_mode not in ("mention", "prefix", "all_messages"):
        raise ConfigError(
            f"bot.trigger_mode 값이 잘못되었습니다: {trigger_mode!r} "
            "(mention | prefix | all_messages 중 하나)"
        )

    debate_raw = raw.get("debate") or {}
    if not isinstance(debate_raw, dict):
        raise ConfigError("bot.debate 항목은 매핑이어야 합니다.")
    debate = DebateConfig(
        max_total_speeches=int(debate_raw.get("max_total_speeches", 30)),
        conclusion_marker=str(debate_raw.get("conclusion_marker", "[결론]")),
        command_prefix=str(debate_raw.get("command_prefix", "!debate")),
        end_command=str(debate_raw.get("end_command", "!end")),
    )
    if debate.max_total_speeches < 2:
        raise ConfigError(
            f"bot.debate.max_total_speeches는 2 이상이어야 합니다: {debate.max_total_speeches}"
        )
    if not debate.conclusion_marker.strip():
        raise ConfigError("bot.debate.conclusion_marker 는 비어있을 수 없습니다.")

    return BotConfig(
        trigger_mode=trigger_mode,
        prefix=str(raw.get("prefix", "!claude")),
        silent_prefix=str(raw.get("silent_prefix", "//")),
        max_response_length=int(raw.get("max_response_length", 1900)),
        claude_timeout=int(raw.get("claude_timeout", 300)),
        debate=debate,
        clear_command=str(raw.get("clear_command", "/clear")),
        history_limit_channel=int(raw.get("history_limit_channel", 30)),
        history_limit_thread=int(raw.get("history_limit_thread", 60)),
        history_max_chars=int(raw.get("history_max_chars", 50000)),
        thread_chain_max_speeches=int(raw.get("thread_chain_max_speeches", 30)),
    )
