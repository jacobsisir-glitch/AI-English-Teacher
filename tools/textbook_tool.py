from __future__ import annotations

from pathlib import Path
import re


TEXTBOOKS_DIR = Path(__file__).resolve().parent.parent / "data" / "textbooks"
SECTION_HEADING_PATTERN = re.compile(r"^(##|###)\s+(.+?)\s*$", re.MULTILINE)
_TEXTBOOK_INDEX_CACHE: str | None = None
_TEXTBOOK_INDEX_CACHE_KEY: tuple[tuple[str, int, int], ...] | None = None


def _iter_textbook_files() -> list[Path]:
    if not TEXTBOOKS_DIR.exists():
        return []
    return sorted(path for path in TEXTBOOKS_DIR.glob("*.md") if path.is_file())


def _extract_first_level_heading(markdown_text: str, fallback: str) -> str:
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _build_textbook_index_cache_key(textbook_files: list[Path]) -> tuple[tuple[str, int, int], ...]:
    cache_items: list[tuple[str, int, int]] = []
    for path in textbook_files:
        stat = path.stat()
        cache_items.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(cache_items)


def _resolve_textbook_path(file_name: str) -> Path:
    requested_name = (file_name or "").strip()
    if not requested_name:
        raise ValueError("file_name 不能为空。")

    normalized_name = Path(requested_name).name
    if normalized_name != requested_name:
        raise ValueError("只允许传入教材文件名，不允许包含路径。")

    candidate = (TEXTBOOKS_DIR / normalized_name).resolve()
    textbook_root = TEXTBOOKS_DIR.resolve()
    if textbook_root != candidate.parent:
        raise ValueError("非法教材路径。")

    if candidate.suffix.lower() != ".md":
        raise ValueError("只允许读取 Markdown 教材文件。")

    return candidate


def get_textbook_index() -> str:
    global _TEXTBOOK_INDEX_CACHE
    global _TEXTBOOK_INDEX_CACHE_KEY

    textbook_files = _iter_textbook_files()
    if not textbook_files:
        return "当前未找到任何教材 Markdown 文件。"

    cache_key = _build_textbook_index_cache_key(textbook_files)
    if _TEXTBOOK_INDEX_CACHE is not None and _TEXTBOOK_INDEX_CACHE_KEY == cache_key:
        return _TEXTBOOK_INDEX_CACHE

    lines = ["教材目录树："]
    for path in textbook_files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            heading = _extract_first_level_heading(content, path.stem)
            lines.append(f"- {path.name}: {heading}")

            matches = SECTION_HEADING_PATTERN.findall(content)
            for level_marker, raw_title in matches:
                clean_title = raw_title.strip()
                if not clean_title:
                    continue

                if level_marker == "##":
                    lines.append(f"  - {clean_title}")
                else:
                    lines.append(f"    - {clean_title}")
        except OSError as exc:
            lines.append(f"- {path.name}: {path.stem}（读取标题失败：{exc}）")

    index_content = "\n".join(lines)
    print("\n=== 教材深层目录树 ===\n", index_content, "\n====================\n")

    _TEXTBOOK_INDEX_CACHE = index_content
    _TEXTBOOK_INDEX_CACHE_KEY = cache_key
    return index_content


def read_textbook_chapter(file_name: str) -> str:
    try:
        chapter_path = _resolve_textbook_path(file_name)
        return chapter_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"未找到教材文件：{file_name}。请改用教材目录中真实存在的文件名。"
    except ValueError as exc:
        return f"读取教材失败：{exc}"
    except OSError as exc:
        return f"读取教材文件时发生异常：{exc}"


TEXTBOOK_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_textbook_chapter",
            "description": "当需要查询具体语法点的详细教材内容时调用此工具。传入教材文件名后，返回对应章节的完整 Markdown 内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "要读取的教材 Markdown 文件名，例如 '01_Verb.md'。",
                    }
                },
                "required": ["file_name"],
                "additionalProperties": False,
            },
        },
    }
]
