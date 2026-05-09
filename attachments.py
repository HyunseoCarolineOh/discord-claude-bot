"""Discord 메시지의 첨부를 Claude 입력으로 변환.

- 텍스트성 파일(.md, .py, .json, ...): 본문에 inline 으로 붙임
- 이미지(.png, .jpg, ...): cache_dir 에 저장 후 절대 경로를 안내해
  Claude 가 Read 도구로 직접 보게 함
- 그 외: "[첨부 무시: ...]" 한 줄

bot.py 의 on_message 와 _build_thread_prompt 양쪽에서 호출됨 (history 복원 포함).
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
