"""Discord 메시지의 첨부를 Claude 입력으로 변환.

- 텍스트성 파일(.md, .py, .json, ...): 본문에 inline 으로 붙임
- 이미지(.png, .jpg, ...): cache_dir 에 저장 후 절대 경로를 안내해
  Claude 가 Read 도구로 직접 보게 함
- 그 외: "[첨부 무시: ...]" 한 줄

bot.py 의 on_message 와 _build_thread_prompt 양쪽에서 호출됨 (history 복원 포함).

또한 응답 텍스트 → 디스코드 첨부 추출 헬퍼 `extract_referenced_files` 도 제공한다.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import discord

log = logging.getLogger(__name__)

TEXT_EXTS = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".csv", ".tsv",
    ".sh", ".bash", ".zsh", ".ps1", ".bat",
    ".toml", ".ini", ".cfg", ".conf", ".env",
    ".html", ".htm", ".css", ".scss", ".sass",
    ".xml", ".sql", ".go", ".rs", ".rb", ".java",
    ".cpp", ".cc", ".c", ".h", ".hpp",
    ".kt", ".swift", ".lua", ".vim",
    ".log", ".diff", ".patch",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(name: str) -> str:
    return _FILENAME_SAFE.sub("_", name)[:120] or "file"


async def render_attachments(
    message: discord.Message,
    image_cache_dir: Path,
) -> str:
    """메시지 첨부를 prompt 뒤에 붙일 텍스트 블록으로 변환.

    반환값은 빈 문자열이거나 선행 \\n\\n 으로 시작하는 블록.
    """
    if not message.attachments:
        return ""

    blocks: list[str] = []
    for att in message.attachments:
        suffix = Path(att.filename).suffix.lower()
        if suffix in TEXT_EXTS:
            blocks.append(await _render_text_attachment(att))
        elif suffix in IMAGE_EXTS:
            blocks.append(await _render_image_attachment(att, message.id, image_cache_dir))
        else:
            blocks.append(
                f"[첨부 무시: {att.filename} (지원하지 않는 형식 {suffix or '확장자 없음'})]"
            )

    return "\n\n" + "\n\n".join(blocks)


async def _render_text_attachment(att: discord.Attachment) -> str:
    try:
        raw = await att.read()
    except (discord.HTTPException, discord.NotFound) as e:
        log.warning("텍스트 첨부 다운로드 실패 %s: %s", att.filename, e)
        return f"[첨부 읽기 실패: {att.filename} ({e})]"
    text = raw.decode("utf-8", errors="replace")
    return (
        f"=== 첨부 파일: {att.filename} ({len(raw):,} bytes) ===\n"
        f"{text}\n"
        f"=== 첨부 끝: {att.filename} ==="
    )


async def _render_image_attachment(
    att: discord.Attachment, message_id: int, image_cache_dir: Path,
) -> str:
    try:
        image_cache_dir.mkdir(parents=True, exist_ok=True)
        safe = _sanitize_filename(att.filename)
        dest = image_cache_dir / f"{message_id}_{safe}"
        await att.save(dest)
    except (discord.HTTPException, discord.NotFound, OSError) as e:
        log.warning("이미지 첨부 저장 실패 %s: %s", att.filename, e)
        return f"[이미지 저장 실패: {att.filename} ({e})]"
    abs_path = dest.resolve()
    return (
        f"=== 첨부 이미지: {att.filename} ===\n"
        f"파일 경로: {abs_path}\n"
        f"이 이미지를 직접 보려면 Read 도구로 위 경로를 읽으세요.\n"
        f"=== 첨부 끝: {att.filename} ==="
    )


# --- 응답 텍스트 → 디스코드 첨부 추출 ---

# Discord 한도 (부스트 없음 기본값)
DISCORD_MAX_FILES_PER_MESSAGE = 10
DISCORD_MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB

# 백틱 안 짧은 토큰만. 줄바꿈·공백·또다른 백틱은 경로에 들어갈 일이 사실상 없음.
_BACKTICK_PATH_RE = re.compile(r"`([^`\n\r]{1,260})`")


def extract_referenced_files(
    text: str,
    project_dir: Path,
) -> tuple[list[Path], list[str]]:
    """응답 텍스트의 백틱 토큰 중 project_dir 안에 실재하는 파일을 첨부 후보로.

    - 백틱 안에 점(.) 이 있고 멘션(@…) 이 아닌 토큰만 후보로 본다.
    - 상대경로면 project_dir 기준으로 resolve, 절대경로면 그대로.
    - resolve 결과가 project_dir 밖이면 보안상 무시.
    - 디스코드 한도(파일 25MB, 메시지당 10개) 초과 분은 warnings 로.

    반환: (files, warnings)
      files: 첨부할 파일 절대경로 (중복 제거, 한도 내, 최대 10개)
      warnings: 사용자에게 보여줄 한 줄 경고 (없으면 빈 리스트)
    """
    try:
        root = project_dir.resolve()
    except (OSError, RuntimeError):
        return [], []

    seen: set[Path] = set()
    found: list[Path] = []
    too_large: list[str] = []

    for m in _BACKTICK_PATH_RE.finditer(text):
        candidate = m.group(1).strip().strip("'\"")
        if not candidate or candidate.startswith("@") or "." not in candidate:
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            continue  # project_dir 밖
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            size = resolved.stat().st_size
        except OSError:
            continue
        if size > DISCORD_MAX_FILE_SIZE:
            too_large.append(resolved.name)
            continue
        found.append(resolved)

    files = found[:DISCORD_MAX_FILES_PER_MESSAGE]
    over_count = [p.name for p in found[DISCORD_MAX_FILES_PER_MESSAGE:]]

    warnings: list[str] = []
    if too_large:
        warnings.append(
            f"⚠️ 용량 초과({DISCORD_MAX_FILE_SIZE // (1024 * 1024)}MB↑)로 첨부 생략: "
            + ", ".join(too_large)
        )
    if over_count:
        warnings.append(
            f"⚠️ 메시지당 첨부 한도({DISCORD_MAX_FILES_PER_MESSAGE}개) 초과로 생략: "
            + ", ".join(over_count)
        )
    return files, warnings
