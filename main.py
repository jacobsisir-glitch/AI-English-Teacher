import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, inspect, text
from sqlalchemy.orm import Session

import database.models
from config import DEFAULT_STUDENT_ID
from database.database import Base, SessionLocal, engine, get_db
from database.models import ErrorBook, KnowledgeMastery, Student, StudentQuestion
from livekit_utils import create_livekit_participant_token, livekit_is_configured
from voice.livekit_room_bridge import VoiceWorkerManager
from voice.session_state import structured_voice_log
from llm_wrapper import (
    bg_summarize_chat_history,
    chat_with_teacher_stream,
    generate_agent_class_reply_stream,
)

COURSE_TASKS = [
    {
        "task_name": "课程导读与开场白",
        "goal": "只做第一节的总导读与开场白，先搭建五大基本句型的总框架，不展开具体单点操练。简短说完后立刻进入第一关，不等待学生确认。",
        "textbook_file": "00_Grammar_Overview.md",
        "section_heading": "### 五大基本句型导读 (Opening Overview)",
        "reference_chars": 2200,
    },
    {
        "task_name": "主谓结构（SV）",
        "goal": "只讲主谓结构与不及物动词，引导学生造一个标准 SV 句。",
        "textbook_file": "00_Grammar_Overview.md",
        "section_heading": "### 主谓结构 (SV Pattern)",
        "reference_chars": 1400,
        "mastery_point": "不及物动词与主谓结构",
    },
    {
        "task_name": "主谓宾结构（SVO）",
        "goal": "只讲主谓宾结构与及物动词，让学生说清为什么宾语不能缺席。",
        "textbook_file": "00_Grammar_Overview.md",
        "section_heading": "### 主谓宾结构 (SVO Pattern)",
        "reference_chars": 1400,
        "mastery_point": "及物动词与宾语",
    },
    {
        "task_name": "主谓双宾结构（SVOO）",
        "goal": "只讲双宾结构，强调“先给人，再给物”的顺序。",
        "textbook_file": "00_Grammar_Overview.md",
        "section_heading": "### 主谓双宾结构 (SVOO Pattern)",
        "reference_chars": 1400,
        "mastery_point": "双及物动词",
    },
    {
        "task_name": "主谓宾补结构（SVOC）",
        "goal": "只讲主谓宾补结构，让学生分清宾补和双宾不是同一回事。",
        "textbook_file": "00_Grammar_Overview.md",
        "section_heading": "### 主谓宾补结构 (SVOC Pattern)",
        "reference_chars": 1400,
        "mastery_point": "复合及物动词与宾补",
    },
    {
        "task_name": "主系表结构（SVC / SVP）",
        "goal": "只讲系动词与表语，让学生理解系动词不是动作动词。",
        "textbook_file": "00_Grammar_Overview.md",
        "section_heading": "### 主系表结构 (SVC / SVP Pattern)",
        "reference_chars": 1400,
        "mastery_point": "系动词与表语",
    },
    {
        "task_name": "动词的时间：现在",
        "goal": "只讲 Present 这个时间坐标，让学生辨认现在时对应的时间感和基本形态。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 现在 (Present)",
        "mastery_point": "动词的时间：现在",
    },
    {
        "task_name": "动词的时间：过去",
        "goal": "只讲 Past 这个时间坐标，让学生理解过去动作已经结束。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去 (Past)",
        "mastery_point": "动词的时间：过去",
    },
    {
        "task_name": "动词的时间：将来",
        "goal": "只讲 Future 这个时间坐标，强调 will 后面必须接动词原形。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 将来 (Future)",
        "mastery_point": "动词的时间：将来",
    },
    {
        "task_name": "动词的时间：过去将来",
        "goal": "只讲 Past Future 这个时间坐标，强调“站在过去看未来”。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去将来 (Past Future)",
        "mastery_point": "过去将来时",
    },
    {
        "task_name": "动词的状态：进行",
        "goal": "只讲进行状态，锁死 be + doing 结构。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 进行状态 (Progressive Aspect)",
        "mastery_point": "动词的状态：进行",
    },
    {
        "task_name": "动词的状态：完成",
        "goal": "只讲完成状态，强调 have + done 与过去分词。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 完成状态 (Perfect Aspect)",
        "mastery_point": "动词的状态：完成",
    },
    {
        "task_name": "动词的状态：完成进行",
        "goal": "只讲完成进行状态，强调 have + been + doing 的持续感。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 完成进行状态 (Perfect Progressive Aspect)",
        "mastery_point": "动词的状态：完成进行",
    },
    {
        "task_name": "动词的状态：一般",
        "goal": "只讲一般状态，强调它表达事实、习惯和常态，而不是正在发生。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 一般状态 (Simple Aspect)",
        "mastery_point": "动词的状态：一般",
    },
    {
        "task_name": "现在进行时",
        "goal": "只讲现在进行时，要求学生能辨认并造出 am/is/are + doing 的句子。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 现在进行时 (Present Progressive)",
        "mastery_point": "现在时态体系",
    },
    {
        "task_name": "现在完成时",
        "goal": "只讲现在完成时，强调过去动作对现在的结果影响。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 现在完成时 (Present Perfect)",
        "mastery_point": "现在时态体系",
    },
    {
        "task_name": "have been to vs have gone to",
        "goal": "只讲 have been to 和 have gone to 的区别，必须把“去了回来了”和“去了还没回”讲清楚。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### have been to vs have gone to",
        "mastery_point": "现在时态体系",
    },
    {
        "task_name": "现在完成进行时",
        "goal": "只讲现在完成进行时，强调动作从过去持续到现在。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 现在完成进行时 (Present Perfect Progressive)",
        "mastery_point": "现在时态体系",
    },
    {
        "task_name": "一般现在时",
        "goal": "只讲一般现在时，强调习惯、真理与三单变化。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 一般现在时 (Simple Present)",
        "mastery_point": "现在时态体系",
    },
    {
        "task_name": "过去进行时",
        "goal": "只讲过去进行时，强调 was/were + doing。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去进行时 (Past Progressive)",
        "mastery_point": "过去时态体系",
    },
    {
        "task_name": "过去完成时",
        "goal": "只讲过去完成时，强调“过去的过去”。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去完成时 (Past Perfect)",
        "mastery_point": "过去时态体系",
    },
    {
        "task_name": "过去完成进行时",
        "goal": "只讲过去完成进行时，强调 had been + doing 的持续过程。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去完成进行时 (Past Perfect Progressive)",
        "mastery_point": "过去时态体系",
    },
    {
        "task_name": "一般过去时",
        "goal": "只讲一般过去时，强调过去发生且已经结束。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 一般过去时 (Simple Past)",
        "mastery_point": "过去时态体系",
    },
    {
        "task_name": "将来进行时",
        "goal": "只讲将来进行时，强调 will + be + doing。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 将来进行时 (Future Progressive)",
        "mastery_point": "将来时态体系",
    },
    {
        "task_name": "将来完成时",
        "goal": "只讲将来完成时，强调截止未来某点前已经完成。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 将来完成时 (Future Perfect)",
        "mastery_point": "将来时态体系",
    },
    {
        "task_name": "将来完成进行时",
        "goal": "只讲将来完成进行时，强调 will have been doing 的长期持续感。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 将来完成进行时 (Future Perfect Progressive)",
        "mastery_point": "将来时态体系",
    },
    {
        "task_name": "一般将来时",
        "goal": "只讲一般将来时，强调 will 后面接动词原形，以及时刻表例外。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 一般将来时 (Simple Future)",
        "mastery_point": "将来时态体系",
    },
    {
        "task_name": "过去将来进行时",
        "goal": "只讲过去将来进行时，强调 would + be + doing。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去将来进行时 (Past Future Progressive)",
        "mastery_point": "过去将来时",
    },
    {
        "task_name": "过去将来完成时",
        "goal": "只讲过去将来完成时，强调 would + have + done。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去将来完成时 (Past Future Perfect)",
        "mastery_point": "过去将来时",
    },
    {
        "task_name": "过去将来完成进行时",
        "goal": "只讲过去将来完成进行时，强调 would + have + been + doing。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 过去将来完成进行时 (Past Future Perfect Progressive)",
        "mastery_point": "过去将来时",
    },
    {
        "task_name": "一般过去将来时",
        "goal": "只讲一般过去将来时，强调 would + 动词原形的转述与预测用法。",
        "textbook_file": "01_Verb.md",
        "section_heading": "#### 一般过去将来时 (Simple Past Future)",
        "mastery_point": "过去将来时",
    },
]

student_state = {
    "is_in_class": False,
    "current_task_index": 0,
    "class_history": [],
    "awaiting_answer": False,
    "pending_question_node_key": "",
    "pending_question_text": "",
    "pending_question_wrong_attempts": 0,
    "last_consumed_answer_fingerprint": "",
}
stream_state = {"session_summary": ""}
stream_state_lock = threading.Lock()

TASK_COMPLETED_MARKER = "[TASK_COMPLETED]"
RETRY_REQUIRED_MARKER = "[RETRY_REQUIRED]"
CLASS_COMPLETED_MESSAGE = "🎉 恭喜你！我们所有的语法特训任务都通关啦！现在退出微课模式咯~"
NEXT_TASK_NUDGE = "直接开始当前知识点的正文讲解。不要复盘上一关，不要欢迎开场，不要重复过桥话。"
OPENING_TASK_NAME = "课程导读与开场白"
CLASS_DB_LOG_START = "===CLASS_DB_START==="
CLASS_DB_LOG_END = "===CLASS_DB_END==="
KNOWLEDGE_MASTERY_BASELINE = 50
KNOWLEDGE_MASTERY_MIN = 0
KNOWLEDGE_MASTERY_MAX = 100
MASTERY_DELTA_ERROR = -12
MASTERY_DELTA_CLASS_SUCCESS = 8
CURRENT_STUDENT_ID = DEFAULT_STUDENT_ID
TEXTBOOKS_DIR = Path(__file__).resolve().parent / "data" / "textbooks"
MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{2,4})\s+(.+?)\s*$")
STAGED_SECTION_HEADING_PATTERN = re.compile(r"^#{4,6}\s*\[STAGE:([A-Z_]+)\]\s*$")
STAGED_SECTION_FIELD_PATTERN = re.compile(r"^\[([A-Z_]+)\]\s*(.*)$")
TEXTBOOK_SLICE_CACHE: dict[tuple[str, str, int, int, int], str] = {}
WHITEBOARD_EVENT_OPEN = "<WBEVENT>"
WHITEBOARD_EVENT_CLOSE = "</WBEVENT>"
WHITEBOARD_WRAPPER_PATTERN = re.compile(r"^`?\[WHITEBOARD:\s*(.*?)\]`?$")
LEGACY_WHITEBOARD_DIRECTIVE_PATTERN = re.compile(
    r"`?\[(?:WHITEBOARD|WB_APPEND|WB_TOOL)(?:\s*:\s*|\s+)?(.*?)\]`?",
    re.IGNORECASE,
)


def _is_opening_task(task_info: dict | None) -> bool:
    if not task_info:
        return False
    task_name = str(task_info.get("task_name") or task_info.get("node_name") or "").strip()
    return task_name == OPENING_TASK_NAME


def _should_auto_advance_class_task(request_action: str, task_info: dict | None, user_msg: str) -> bool:
    if request_action == "start":
        return _is_opening_task(task_info)
    if _is_opening_task(task_info):
        return False
    if not bool(student_state.get("awaiting_answer")):
        return False

    normalized_user_msg = re.sub(r"\s+", " ", str(user_msg or "")).strip()
    if not normalized_user_msg:
        return False

    pending_node_key = str(student_state.get("pending_question_node_key") or "").strip()
    current_node_key = str((task_info or {}).get("task_name") or (task_info or {}).get("node_name") or "").strip()
    if pending_node_key and current_node_key and pending_node_key != current_node_key:
        return False

    pending_question_text = re.sub(
        r"\s+",
        " ",
        str(student_state.get("pending_question_text") or ""),
    ).strip()
    if pending_question_text and normalized_user_msg == pending_question_text:
        return False

    answer_fingerprint = f"{current_node_key}::{normalized_user_msg}"
    if answer_fingerprint == str(student_state.get("last_consumed_answer_fingerprint") or ""):
        return False

    return True


def _extract_markdown_section(markdown_text: str, heading_title: str) -> str:
    lines = markdown_text.splitlines()
    normalized_title = re.sub(r"^#{1,6}\s+", "", (heading_title or "").strip())
    if not normalized_title:
        return ""

    start_index = None
    start_level = None
    fallback_index = None
    fallback_level = None

    for index, raw_line in enumerate(lines):
        match = MARKDOWN_HEADING_PATTERN.match(raw_line.strip())
        if not match:
            continue

        level = len(match.group(1))
        title = match.group(2).strip()
        if title == normalized_title:
            start_index = index
            start_level = level
            break
        if fallback_index is None and normalized_title in title:
            fallback_index = index
            fallback_level = level

    if start_index is None:
        start_index = fallback_index
        start_level = fallback_level
    if start_index is None or start_level is None:
        return ""

    end_index = len(lines)
    for index in range(start_index + 1, len(lines)):
        match = MARKDOWN_HEADING_PATTERN.match(lines[index].strip())
        if match and len(match.group(1)) <= start_level:
            end_index = index
            break

    return "\n".join(lines[start_index:end_index]).strip()


def _compact_reference_text(section_text: str, max_chars: int = 420) -> str:
    normalized_lines: list[str] = []
    previous_blank = False

    for raw_line in section_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if not previous_blank:
                normalized_lines.append("")
            previous_blank = True
            continue
        normalized_lines.append(line)
        previous_blank = False

    compact_text = "\n".join(normalized_lines).strip()
    if len(compact_text) <= max_chars:
        return compact_text

    truncated = compact_text[:max_chars]
    if "\n" in truncated:
        truncated = truncated.rsplit("\n", 1)[0].rstrip()
    if len(truncated) < max_chars * 0.6:
        truncated = compact_text[:max_chars].rsplit(" ", 1)[0].rstrip()
    return truncated.rstrip() + "\n..."


def _read_precise_textbook_slice(file_name: str, section_heading: str, max_chars: int = 420) -> str:
    textbook_path = TEXTBOOKS_DIR / file_name
    if not textbook_path.exists():
        return f"教材切片缺失：{file_name} / {section_heading}"

    stat = textbook_path.stat()
    cache_key = (file_name, section_heading, max_chars, stat.st_mtime_ns, stat.st_size)
    cached = TEXTBOOK_SLICE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    try:
        markdown_text = textbook_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"教材切片读取失败：{file_name} / {section_heading} / {exc}"

    section_text = _extract_markdown_section(markdown_text, section_heading)
    if not section_text:
        return f"教材切片缺失：{file_name} / {section_heading}"

    precise_slice = _compact_reference_text(section_text, max_chars=max_chars)
    TEXTBOOK_SLICE_CACHE[cache_key] = precise_slice
    return precise_slice


def _sanitize_reference_for_llm(reference_text: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in str(reference_text or "").splitlines():
        cleaned_line = LEGACY_WHITEBOARD_DIRECTIVE_PATTERN.sub(lambda match: match.group(1).strip(), raw_line)
        cleaned_lines.append(cleaned_line)
    return "\n".join(cleaned_lines).strip()


def _parse_explicit_stage_plan(reference_text: str) -> list[dict]:
    stages: list[dict] = []
    current_stage: dict | None = None
    current_field = ""

    def finalize_stage(stage: dict | None) -> None:
        if not stage:
            return
        fields = stage.get("fields") or {}
        wb_title = " ".join(fields.get("WB_TITLE", [])).strip()
        wb_lines = [line.strip() for line in fields.get("WB_LINES", []) if line.strip()]
        voice_guidance = [line.strip() for line in fields.get("VOICE_GUIDE", []) if line.strip()]
        stage_rule = " ".join(fields.get("STAGE_RULE", [])).strip()
        question_text = " ".join(fields.get("QUESTION", [])).strip()
        answer_rule = " ".join(fields.get("ANSWER_RULE", [])).strip()
        transition_hint = " ".join(fields.get("TRANSITION", [])).strip()
        expects_answer = str(stage.get("stage_kind") or "").upper() == "QUIZ"

        if not (wb_title or wb_lines or voice_guidance or question_text or answer_rule):
            return

        stages.append(
            {
                "stage_kind": str(stage.get("stage_kind") or "").strip().lower(),
                "wb_title": wb_title,
                "wb_lines": wb_lines,
                "voice_guidance": voice_guidance,
                "stage_rule": stage_rule,
                "question_text": question_text,
                "answer_rule": answer_rule,
                "transition_hint": transition_hint,
                "expects_answer": expects_answer,
            }
        )

    for raw_line in str(reference_text or "").splitlines():
        stripped_line = raw_line.strip()
        stage_match = STAGED_SECTION_HEADING_PATTERN.match(stripped_line)
        if stage_match:
            finalize_stage(current_stage)
            current_stage = {"stage_kind": stage_match.group(1).strip(), "fields": {}}
            current_field = ""
            continue

        if current_stage is None:
            continue

        field_match = STAGED_SECTION_FIELD_PATTERN.match(stripped_line)
        if field_match:
            current_field = field_match.group(1).strip().upper()
            field_bucket = current_stage["fields"].setdefault(current_field, [])
            first_line = field_match.group(2).strip()
            if first_line:
                field_bucket.append(first_line)
            continue

        if not current_field:
            continue

        if not stripped_line:
            continue

        current_stage["fields"].setdefault(current_field, []).append(stripped_line)

    finalize_stage(current_stage)
    return stages


def _build_stage_plan_from_reference(task_info: dict, reference_text: str) -> list[dict]:
    parsed_stages = _parse_explicit_stage_plan(reference_text)
    if not parsed_stages:
        return []

    task_label = str(task_info.get("task_name") or task_info.get("node_name") or "当前知识点").strip()
    stage_plan: list[dict] = []

    for index, stage in enumerate(parsed_stages):
        stage_kind = str(stage.get("stage_kind") or "").strip().lower() or f"stage_{index + 1}"
        raw_lines = [str(line or "").strip() for line in stage.get("wb_lines") or []]
        cleaned_lines = [(_clean_whiteboard_markdown_line(line) or line.strip()) for line in raw_lines if line.strip()]
        cleaned_lines = [line for line in cleaned_lines if line]
        wb_title = str(stage.get("wb_title") or "").strip() or f"{task_label} · {stage_kind.upper()}"
        question_text = str(stage.get("question_text") or "").strip()
        answer_rule = str(stage.get("answer_rule") or "").strip()
        stage_rule = str(stage.get("stage_rule") or "").strip()
        transition_hint = str(stage.get("transition_hint") or "").strip()
        voice_guidance = [str(line or "").strip() for line in stage.get("voice_guidance") or [] if str(line or "").strip()]
        content_markdown = "\n".join(cleaned_lines).strip()

        stage_plan.append(
            {
                "stage_kind": stage_kind,
                "page_key": _build_whiteboard_page_key(task_info, f"{index + 1}_{stage_kind}"),
                "title": wb_title,
                "content": content_markdown,
                "voice_guidance": voice_guidance,
                "stage_rule": stage_rule,
                "transition_hint": transition_hint,
                "question_text": question_text,
                "answer_rule": answer_rule,
                "expects_answer": bool(stage.get("expects_answer")),
            }
        )

    return stage_plan


def _build_stage_plan_reference_summary(stage_plan: list[dict]) -> str:
    if not stage_plan:
        return ""

    summary_lines = ["当前知识点已经按舞台阶段拆分，老师要跟着阶段推进，不要抢跑。"]
    for stage in stage_plan[:6]:
        stage_kind = str(stage.get("stage_kind") or "").strip().upper()
        title = str(stage.get("title") or "").strip()
        voice_guidance = [str(line or "").strip() for line in stage.get("voice_guidance") or [] if str(line or "").strip()]
        line = f"- {stage_kind}: {title}" if title else f"- {stage_kind}"
        if voice_guidance:
            line += f"；可借用话术：{' / '.join(voice_guidance[:3])}"
        stage_rule = str(stage.get("stage_rule") or "").strip()
        if stage_rule:
            line += f"；阶段要求：{stage_rule}"
        summary_lines.append(line)
    return "\n".join(summary_lines).strip()


def _build_stage_plan_question_text(stage_plan: list[dict]) -> str:
    for stage in reversed(stage_plan):
        question_text = str(stage.get("question_text") or "").strip()
        if not question_text:
            continue
        answer_rule = str(stage.get("answer_rule") or "").strip()
        if answer_rule:
            return f"老师提问：{question_text}\n作答要求：{answer_rule}"
        return f"老师提问：{question_text}"
    return ""


def _extract_reference_section_lines(reference_text: str) -> tuple[list[str], list[str], list[str]]:
    formula_lines: list[str] = []
    example_lines: list[str] = []
    note_lines: list[str] = []
    section = ""

    for raw_line in str(reference_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if "白板核心公式" in line:
            section = "formula"
            continue
        if "经典对错对比" in line:
            section = "example"
            continue
        if "AI 主播话术" in line or "Trigger" in line:
            section = "note"
            continue

        cleaned_line = _clean_whiteboard_markdown_line(line)
        if not cleaned_line:
            continue

        if section == "formula":
            formula_lines.append(cleaned_line)
        elif section == "example":
            example_lines.append(cleaned_line)
        else:
            note_lines.append(cleaned_line)

    return formula_lines, example_lines, note_lines


def _extract_backtick_text(line: str) -> str:
    text = str(line or "").strip()
    matches = re.findall(r"`([^`]+)`", text)
    if matches:
        return matches[-1].strip()

    text = re.sub(r"^-\s+", "", text)
    text = re.sub(r"^[❌✅]\s*", "", text)
    text = re.sub(r"^(?:弹幕常犯错误|常犯错误|错误|错句|正确标准答案|正确答案|标准答案)\s*[：:]\s*", "", text)
    if "：" in text:
        text = text.split("：", 1)[-1].strip()
    if ":" in text:
        text = text.split(":", 1)[-1].strip()
    return text.strip("` ").strip()


def _build_whiteboard_question(task_info: dict) -> str:
    if _is_opening_task(task_info):
        return ""

    task_name = str(task_info.get("node_name") or task_info.get("task_name") or "当前知识点").strip()
    goal = str(task_info.get("goal") or "").strip()
    formula_lines, example_lines, _ = _extract_reference_section_lines(task_info.get("reference") or "")

    wrong_line = next((line for line in example_lines if "❌" in line or "错误" in line), "")
    correct_line = next((line for line in example_lines if "✅" in line or "正确" in line), "")
    formula_focus = re.sub(r"^-\s+", "", formula_lines[0]).strip() if formula_lines else task_name

    if wrong_line:
        wrong_example = _extract_backtick_text(wrong_line)
        requirement = (
            "作答要求：你可以直接改这句，也可以自己另外造一个符合当前知识点的正确句子；"
            "然后再用中文说明你为什么这样改。"
        )
        if correct_line:
            requirement = (
                "作答要求：你可以改正这句，也可以自己另外造一个符合当前知识点的正确句子；"
                "然后再用中文说明原句错在什么地方。"
            )
        return f"老师提问：请你改正 `{wrong_example}`。\n{requirement}"

    if any(keyword in goal for keyword in ("造一个", "造出", "造句")):
        return (
            f"老师提问：请你用“{formula_focus}”自己造一个句子。\n"
            "作答要求：你可以参考老师刚才的例句，但不必照搬；直接给句子，再用中文点出你用到的关键结构。"
        )

    return (
        f"老师提问：请你围绕“{task_name}”说一个正确句子。\n"
        "作答要求：你可以参考老师给的例句，也可以自己重新造句；先给答案，再用中文解释你抓住了哪条判断依据。"
    )


def _build_spoken_reference_summary(reference_text: str) -> str:
    formula_lines, example_lines, note_lines = _extract_reference_section_lines(reference_text)

    summary_lines: list[str] = []
    if formula_lines:
        summary_lines.append("黑板上已经写好了当前知识点的核心公式。你只需要用中文解释它表示什么、怎么判断、什么时候容易错。")
        for line in formula_lines[:2]:
            focus = re.sub(r"^-\s+", "", line).strip()
            if "=" in focus:
                focus = focus.split("=", 1)[0].strip()
            if focus:
                summary_lines.append(f"- 当前板书关键词：{focus}")
    if example_lines:
        summary_lines.append("黑板上已经给了对错对比。你只解释错因、修改理由和判断依据。必要时可以点一个很短的英文例句，但不要整段照念。")
    if note_lines:
        summary_lines.append("你可以借用下面这些人设钩子、比喻或讲解节奏，但要自然说人话，不要逐条朗读提示词。")
        for line in note_lines[:6]:
            cue = re.sub(r"^-\s+", "", line).strip()
            if cue:
                summary_lines.append(f"- 可借用话术：{cue}")

    return "\n".join(summary_lines).strip()


def _build_runtime_course_task(task_index: int) -> dict:
    base_task = dict(COURSE_TASKS[task_index])
    reference_text = str(base_task.get("reference_text") or "").strip()
    textbook_file = str(base_task.get("textbook_file") or "").strip()
    section_heading = str(base_task.get("section_heading") or "").strip()
    reference_chars = int(base_task.get("reference_chars") or 420)
    if textbook_file == "00_Grammar_Overview.md" and not base_task.get("reference_chars"):
        reference_chars = 1400

    if textbook_file and section_heading:
        reference_text = _read_precise_textbook_slice(
            textbook_file,
            section_heading,
            max_chars=reference_chars,
        )
        base_task["reference_source"] = f"{textbook_file} :: {section_heading}"

    base_task["reference"] = reference_text
    stage_plan = _build_stage_plan_from_reference(base_task, reference_text)
    base_task["stage_plan"] = stage_plan
    base_task["llm_reference"] = (
        _build_stage_plan_reference_summary(stage_plan)
        or _build_spoken_reference_summary(reference_text)
        or _sanitize_reference_for_llm(reference_text)
    )
    base_task["whiteboard_question"] = (
        _build_stage_plan_question_text(stage_plan)
        or _build_whiteboard_question(base_task)
    )
    base_task["node_name"] = base_task.get("task_name", "当前知识点")
    return base_task

def _clean_whiteboard_markdown_line(raw_line: str) -> str:
    line = str(raw_line or "").strip()
    if not line:
        return ""

    is_bullet = bool(re.match(r"^[-*]\s+", line))
    core_text = re.sub(r"^[-*]\s+", "", line).strip()
    wrapped_match = WHITEBOARD_WRAPPER_PATTERN.match(core_text)
    if wrapped_match:
        core_text = wrapped_match.group(1).strip()

    core_text = core_text.strip("`").strip()
    if not core_text:
        return ""

    return f"- {core_text}" if is_bullet else core_text


def _build_whiteboard_contents(reference_text: str) -> list[str]:
    formula_lines, example_lines, note_lines = _extract_reference_section_lines(reference_text)

    contents: list[str] = []
    if formula_lines:
        contents.append("### 核心公式\n" + "\n".join(formula_lines[:2]))
    if example_lines:
        contents.append("### 经典对错对比\n" + "\n".join(example_lines[:3]))

    if contents:
        return contents

    fallback_lines = [
        cleaned_line
        for line in str(reference_text or "").splitlines()
        if (cleaned_line := _clean_whiteboard_markdown_line(line))
    ]
    if not fallback_lines:
        return []
    return ["### 当前板书\n" + "\n".join(fallback_lines)]


def _build_whiteboard_formula_content(reference_text: str) -> str:
    formula_lines, _, _ = _extract_reference_section_lines(reference_text)
    if formula_lines:
        return "### 核心公式\n" + "\n".join(formula_lines[:2])

    fallback_lines = [
        cleaned_line
        for line in str(reference_text or "").splitlines()
        if (cleaned_line := _clean_whiteboard_markdown_line(line))
    ]
    if not fallback_lines:
        return ""
    return "### 当前板书\n" + "\n".join(fallback_lines[:2])


def _build_whiteboard_example_content(reference_text: str) -> str:
    _, example_lines, _ = _extract_reference_section_lines(reference_text)
    if not example_lines:
        return ""
    return "### 经典对错对比\n" + "\n".join(example_lines[:3])


def _build_whiteboard_stage_title(task_info: dict, stage_kind: str = "") -> str:
    base_title = str(task_info.get("node_name") or task_info.get("task_name") or "微课板书").strip()
    stage_kind = str(stage_kind or "").strip().lower()
    if stage_kind == "formula":
        return f"{base_title} · 核心公式"
    if stage_kind == "example":
        return f"{base_title} · 经典对错对比"
    return base_title


def _build_whiteboard_page_key(task_info: dict, stage_kind: str = "") -> str:
    node_key = str(task_info.get("task_name") or task_info.get("node_name") or "node").strip()
    stage_kind = str(stage_kind or "").strip().lower() or "default"
    return f"{node_key}::{stage_kind}"


def _build_whiteboard_stage_system_notice(task_info: dict, stage_kind: str = "") -> str:
    page_key = _build_whiteboard_page_key(task_info, stage_kind)
    return f"[SYSTEM:WB_STAGE_READY::{page_key}]"


def _iter_whiteboard_stage_events(
    task_info: dict,
    *,
    include_new_page: bool = False,
    include_formula: bool = False,
    include_example: bool = False,
    include_question: bool = False,
    stage_kind: str = "",
):
    node_key = str(task_info.get("task_name") or task_info.get("node_name") or "").strip()
    title = _build_whiteboard_stage_title(task_info, stage_kind)
    page_key = _build_whiteboard_page_key(task_info, stage_kind)

    if include_new_page:
        yield _serialize_whiteboard_update(
            {
                "action": "new_page",
                "node_key": node_key,
                "page_key": page_key,
                "title": title,
            }
        )

    if include_formula:
        formula_content = _build_whiteboard_formula_content(task_info.get("reference") or "")
        if formula_content:
            yield _serialize_whiteboard_update(
                {
                    "action": "append",
                    "node_key": node_key,
                    "page_key": page_key,
                    "title": title,
                    "content": formula_content,
                }
            )

    if include_example:
        example_content = _build_whiteboard_example_content(task_info.get("reference") or "")
        if example_content:
            yield _serialize_whiteboard_update(
                {
                    "action": "append",
                    "node_key": node_key,
                    "page_key": page_key,
                    "title": title,
                    "content": example_content,
                }
            )

    if include_question:
        question = str(task_info.get("whiteboard_question") or "").strip()
        if question:
            yield _serialize_whiteboard_update(
                {
                    "action": "question",
                    "node_key": node_key,
                    "page_key": page_key,
                    "title": title,
                    "question": question,
                }
            )


def _build_whiteboard_update_payload(task_info: dict) -> dict:
    return {
        "type": "whiteboard_update",
        "node_key": str(task_info.get("task_name") or task_info.get("node_name") or "").strip(),
        "title": str(task_info.get("node_name") or task_info.get("task_name") or "当前知识点").strip(),
        "contents": _build_whiteboard_contents(task_info.get("reference") or ""),
        "question": str(task_info.get("whiteboard_question") or "").strip(),
    }


def _serialize_whiteboard_update(payload: dict) -> str:
    return (
        f"{WHITEBOARD_EVENT_OPEN}"
        f"{json.dumps(payload, ensure_ascii=False)}"
        f"{WHITEBOARD_EVENT_CLOSE}\n"
    )


def _iter_whiteboard_events(task_info: dict):
    node_key = str(task_info.get("task_name") or task_info.get("node_name") or "").strip()
    title = str(task_info.get("node_name") or task_info.get("task_name") or "微课板书").strip()
    page_key = _build_whiteboard_page_key(task_info, "default")
    contents = _build_whiteboard_contents(task_info.get("reference") or "")
    question = str(task_info.get("whiteboard_question") or "").strip()

    yield _serialize_whiteboard_update(
        {
            "action": "new_page",
            "node_key": node_key,
            "page_key": page_key,
            "title": title,
        }
    )

    for content in contents:
        cleaned_content = str(content or "").strip()
        if not cleaned_content:
            continue
        yield _serialize_whiteboard_update(
            {
                "action": "append",
                "node_key": node_key,
                "page_key": page_key,
                "title": title,
                "content": cleaned_content,
            }
        )

    if question:
        yield _serialize_whiteboard_update(
            {
                "action": "question",
                "node_key": node_key,
                "page_key": page_key,
                "title": title,
                "question": question,
            }
        )


class TaskCompletedBuffer:
    def __init__(self, markers: str | list[str] | tuple[str, ...]):
        if isinstance(markers, str):
            markers = [markers]
        self.markers = tuple(
            sorted(
                {str(marker) for marker in markers if str(marker)},
                key=len,
                reverse=True,
            )
        )
        self.buffer = ""
        self.visible_parts = []
        self.detected_markers: set[str] = set()

    def push(self, chunk: str) -> str:
        if not chunk:
            return ""

        released_chars = []
        for char in chunk:
            self.buffer += char
            matched_marker = next(
                (marker for marker in self.markers if self.buffer == marker),
                None,
            )
            if matched_marker:
                self.detected_markers.add(matched_marker)
                self.buffer = ""
                continue
            while self.buffer and not any(
                marker.startswith(self.buffer) for marker in self.markers
            ):
                released_chars.append(self.buffer[0])
                self.visible_parts.append(self.buffer[0])
                self.buffer = self.buffer[1:]
        return "".join(released_chars)

    def finalize(self) -> str:
        if not self.buffer:
            return ""
        remaining = self.buffer
        self.visible_parts.append(remaining)
        self.buffer = ""
        return remaining

    @property
    def clean_text(self) -> str:
        return "".join(self.visible_parts)

    @property
    def detected(self) -> bool:
        return bool(self.detected_markers)

    def saw(self, marker: str) -> bool:
        return marker in self.detected_markers


class AnalyzeDBLogBuffer:
    def __init__(self, start_marker: str, end_marker: str):
        self.start_marker = start_marker
        self.end_marker = end_marker
        self.mode = "text"
        self.text_buffer = ""
        self.db_buffer = ""
        self.visible_parts = []
        self.db_parts = []
        self.detected = False
        self.completed = False

    def push(self, chunk: str) -> str:
        if not chunk or self.mode == "done":
            return ""

        released_chars = []
        for char in chunk:
            if self.mode == "text":
                self.text_buffer += char
                if self.text_buffer == self.start_marker:
                    self.detected = True
                    self.mode = "db"
                    self.text_buffer = ""
                    continue
                while self.text_buffer and not self.start_marker.startswith(self.text_buffer):
                    released_chars.append(self.text_buffer[0])
                    self.visible_parts.append(self.text_buffer[0])
                    self.text_buffer = self.text_buffer[1:]
            elif self.mode == "db":
                self.db_buffer += char
                if self.db_buffer == self.end_marker:
                    self.completed = True
                    self.mode = "done"
                    self.db_buffer = ""
                    continue
                while self.db_buffer and not self.end_marker.startswith(self.db_buffer):
                    self.db_parts.append(self.db_buffer[0])
                    self.db_buffer = self.db_buffer[1:]
        return "".join(released_chars)

    def finalize(self) -> str:
        if self.mode == "text" and self.text_buffer:
            remaining = self.text_buffer
            self.visible_parts.append(remaining)
            self.text_buffer = ""
            return remaining

        if self.mode == "db" and self.db_buffer:
            while self.db_buffer and not self.end_marker.startswith(self.db_buffer):
                self.db_parts.append(self.db_buffer[0])
                self.db_buffer = self.db_buffer[1:]
        return ""

    @property
    def ai_comment(self) -> str:
        return "".join(self.visible_parts).strip()

    @property
    def db_json_text(self) -> str:
        return "".join(self.db_parts).strip()


def _clamp_mastery_score(score: int) -> int:
    return max(KNOWLEDGE_MASTERY_MIN, min(KNOWLEDGE_MASTERY_MAX, score))


def _score_to_mastery_status(score: int) -> str:
    if score >= 85:
        return "mastered"
    if score >= 65:
        return "improving"
    if score >= 40:
        return "learning"
    return "needs_review"


def _format_mastery_status(status: str) -> str:
    return {
        "mastered": "已掌握",
        "improving": "提升中",
        "learning": "学习中",
        "needs_review": "待巩固",
    }.get(status, status or "学习中")


def _resolve_course_mastery_point(task_index: int) -> str | None:
    if 0 <= task_index < len(COURSE_TASKS):
        return COURSE_TASKS[task_index].get("mastery_point")
    return None


def _update_knowledge_mastery(
    db: Session,
    grammar_point: str,
    delta: int,
    student_id: str = DEFAULT_STUDENT_ID,
) -> None:
    normalized_point = re.sub(r"\s+", " ", grammar_point or "").strip()
    if not normalized_point:
        return

    try:
        record = (
            db.query(KnowledgeMastery)
            .filter(
                KnowledgeMastery.student_id == student_id,
                KnowledgeMastery.grammar_point == normalized_point,
            )
            .first()
        )

        if record is None:
            next_score = _clamp_mastery_score(KNOWLEDGE_MASTERY_BASELINE + delta)
            record = KnowledgeMastery(
                student_id=student_id,
                grammar_point=normalized_point,
                mastery_score=next_score,
                status=_score_to_mastery_status(next_score),
                last_tested_at=datetime.utcnow(),
            )
            db.add(record)
        else:
            next_score = _clamp_mastery_score((record.mastery_score or 0) + delta)
            record.mastery_score = next_score
            record.status = _score_to_mastery_status(next_score)
            record.last_tested_at = datetime.utcnow()

        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"KnowledgeMastery update failed: {exc}")


def _save_error_book_entry(db: Session, user_input: str, ai_comment: str, db_json_text: str) -> None:
    try:
        payload = json.loads(db_json_text)
    except json.JSONDecodeError as exc:
        print(f"ErrorBook JSON parse failed: {exc}. raw={db_json_text}")
        return

    grammar_point = str(payload.get("grammar_point", "")).strip()
    error_tag = str(payload.get("error_tag", "")).strip()
    if not grammar_point or not error_tag:
        return

    try:
        db.add(
            ErrorBook(
                grammar_point=grammar_point,
                error_tag=error_tag,
                user_input=user_input,
                ai_comment=ai_comment,
            )
        )
        db.commit()
        _update_knowledge_mastery(
            db,
            grammar_point=grammar_point,
            delta=MASTERY_DELTA_ERROR,
            student_id=CURRENT_STUDENT_ID,
        )
    except Exception as exc:
        db.rollback()
        print(f"ErrorBook save failed: {exc}")


def _ensure_knowledge_mastery_schema() -> None:
    inspector = inspect(engine)
    if "knowledge_mastery" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("knowledge_mastery")}
    alter_statements: list[str] = []

    if "student_id" not in column_names:
        alter_statements.append(
            "ALTER TABLE knowledge_mastery "
            "ADD COLUMN student_id VARCHAR(64) NOT NULL DEFAULT 'default_student'"
        )
    if "status" not in column_names:
        alter_statements.append(
            "ALTER TABLE knowledge_mastery "
            "ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'learning'"
        )
    if "last_tested_at" not in column_names:
        alter_statements.append(
            "ALTER TABLE knowledge_mastery "
            "ADD COLUMN last_tested_at DATETIME"
        )

    if not alter_statements:
        return

    with engine.begin() as connection:
        for statement in alter_statements:
            connection.execute(text(statement))


def _build_mastery_snapshot(
    db: Session,
    student_id: str = DEFAULT_STUDENT_ID,
    limit: int = 5,
) -> list[dict]:
    rows = (
        db.query(KnowledgeMastery)
        .filter(KnowledgeMastery.student_id == student_id)
        .order_by(
            KnowledgeMastery.mastery_score.asc(),
            KnowledgeMastery.last_tested_at.is_(None),
            KnowledgeMastery.last_tested_at.desc(),
            KnowledgeMastery.grammar_point.asc(),
        )
        .limit(limit)
        .all()
    )

    return [
        {
            "grammar_point": row.grammar_point,
            "mastery_score": row.mastery_score or 0,
            "status": row.status or "learning",
            "status_label": _format_mastery_status(row.status or "learning"),
            "last_tested_at": row.last_tested_at.isoformat() if row.last_tested_at else None,
        }
        for row in rows
    ]


def _build_mastery_summary(db: Session, student_id: str = DEFAULT_STUDENT_ID) -> str:
    snapshot = _build_mastery_snapshot(db, student_id=student_id, limit=3)
    if not snapshot:
        return ""

    weakest = snapshot[0]
    lines = [
        (
            f"当前掌握度最低的知识点是「{weakest['grammar_point']}」，"
            f"当前分数 {weakest['mastery_score']}/100，状态为「{weakest['status_label']}」。"
        )
    ]

    if len(snapshot) > 1:
        lines.append("其余需要优先盯住的点还有：")
        for item in snapshot[1:]:
            lines.append(
                f"- {item['grammar_point']}：{item['mastery_score']}/100（{item['status_label']}）"
            )

    return "\n".join(lines)


def _normalize_history(history: list[dict] | None, limit: int = 6) -> list[dict]:
    if not history:
        return []

    normalized_history: list[dict] = []
    for item in history[-limit:]:
        if not isinstance(item, dict):
            continue

        raw_role = str(item.get("role", "")).strip().lower()
        content = str(item.get("content", "")).strip()
        if not content:
            continue

        if raw_role == "ai":
            raw_role = "assistant"
        if raw_role not in {"user", "assistant"}:
            continue

        normalized_history.append({"role": raw_role, "content": content})

    return normalized_history


def _split_history_for_summary(
    history: list[dict] | None,
    keep_limit: int = 6,
    evict_count: int = 3,
) -> tuple[list[dict], list[dict]]:
    normalized_history = _normalize_history(history, limit=64)
    if len(normalized_history) <= keep_limit:
        return normalized_history, []

    evicted_messages = normalized_history[:evict_count]
    recent_history = normalized_history[evict_count:]
    return recent_history, evicted_messages


def _pick_prompt_history(request_history: list[dict] | None, fallback_history: list[dict] | None = None) -> list[dict]:
    normalized_request_history = _normalize_history(request_history)
    if normalized_request_history:
        return normalized_request_history
    return _normalize_history(fallback_history)


def _get_or_create_student_record(db: Session, student_id: str = DEFAULT_STUDENT_ID) -> Student:
    record = (
        db.query(Student)
        .filter(Student.student_id == student_id)
        .first()
    )
    if record is None:
        record = Student(student_id=student_id, session_summary="")
        db.add(record)
        db.commit()
        db.refresh(record)
    return record


def _load_session_summary_from_db(student_id: str = DEFAULT_STUDENT_ID) -> str:
    db = SessionLocal()
    try:
        student = _get_or_create_student_record(db, student_id=student_id)
        summary = str(student.session_summary or "").strip()
        with stream_state_lock:
            stream_state["session_summary"] = summary
        return summary
    except Exception as exc:
        print(f"加载直播长期记忆失败：{exc}")
        return ""
    finally:
        db.close()


def _bg_update_session_summary(evicted_messages: list[dict]) -> None:
    if not evicted_messages:
        return

    with stream_state_lock:
        old_summary = str(stream_state.get("session_summary", "") or "")

    new_summary = bg_summarize_chat_history(old_summary, evicted_messages).strip()

    with stream_state_lock:
        latest_summary = str(stream_state.get("session_summary", "") or "")
        if latest_summary != old_summary:
            new_summary = bg_summarize_chat_history(latest_summary, evicted_messages).strip()
        stream_state["session_summary"] = new_summary

    db = SessionLocal()
    try:
        student = _get_or_create_student_record(db, student_id=CURRENT_STUDENT_ID)
        student.session_summary = new_summary
        db.commit()
        print(f"直播长期记忆已更新并落盘：{new_summary}")
    except Exception as exc:
        db.rollback()
        print(f"直播长期记忆落盘失败：{exc}")
    finally:
        db.close()


def _should_log_student_question(text: str) -> bool:
    normalized_text = re.sub(r"\s+", " ", text or "").strip()
    return bool(normalized_text)


def _save_student_question(
    db: Session,
    question_text: str,
    source: str,
    student_id: str = DEFAULT_STUDENT_ID,
) -> None:
    normalized_text = re.sub(r"\s+", " ", question_text or "").strip()
    if not normalized_text:
        return

    try:
        db.add(
            StudentQuestion(
                student_id=student_id,
                question_text=normalized_text,
                mode=source,
                source=source,
            )
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"StudentQuestion save failed: {exc}")


def _ensure_student_question_schema() -> None:
    inspector = inspect(engine)
    if "student_questions" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("student_questions")}
    alter_statements: list[str] = []

    if "student_id" not in column_names:
        alter_statements.append(
            "ALTER TABLE student_questions "
            "ADD COLUMN student_id VARCHAR(64) NOT NULL DEFAULT 'default_student'"
        )
    if "mode" not in column_names:
        alter_statements.append(
            "ALTER TABLE student_questions "
            "ADD COLUMN mode VARCHAR(50) NOT NULL DEFAULT 'chat'"
        )

    if not alter_statements:
        return

    with engine.begin() as connection:
        for statement in alter_statements:
            connection.execute(text(statement))


def _extract_db_log(text_value: str, start_marker: str, end_marker: str) -> tuple[str, str, bool]:
    buffer = AnalyzeDBLogBuffer(start_marker, end_marker)
    buffer.push(text_value or "")
    visible_remainder = buffer.finalize()
    visible_text = (buffer.ai_comment + visible_remainder).strip()
    return visible_text, buffer.db_json_text, buffer.detected and buffer.completed


def _get_top_weakness_summary(db: Session, student_id: str = DEFAULT_STUDENT_ID) -> str:
    mastery_summary = _build_mastery_summary(db, student_id=student_id)
    if mastery_summary:
        return mastery_summary

    top_row = (
        db.query(
            ErrorBook.grammar_point.label("grammar_point"),
            func.count(ErrorBook.id).label("error_count"),
        )
        .group_by(ErrorBook.grammar_point)
        .order_by(func.count(ErrorBook.id).desc(), ErrorBook.grammar_point.asc())
        .first()
    )

    if not top_row or not top_row.grammar_point:
        return "暂时还没有足够的错题或掌握度记录，先拿几轮互动把问题暴露出来再说。"

    return f"错题本里最容易翻车的知识点是「{top_row.grammar_point}」，累计失误 {top_row.error_count} 次。"


def _build_student_profile_summary(
    db: Session,
    student_id: str = DEFAULT_STUDENT_ID,
    question_limit: int = 3,
) -> str:
    summary_parts = [_get_top_weakness_summary(db, student_id=student_id)]
    question_rows = (
        db.query(StudentQuestion)
        .filter(StudentQuestion.student_id == student_id)
        .order_by(StudentQuestion.created_at.desc(), StudentQuestion.id.desc())
        .limit(question_limit)
        .all()
    )

    recent_question_lines = [
        f"- [{row.mode or row.source}] {row.question_text}"
        for row in question_rows
        if row.question_text
    ]
    if recent_question_lines:
        summary_parts.append("最近学生主动暴露出来的疑问有：\n" + "\n".join(recent_question_lines))

    return "\n".join(part for part in summary_parts if part).strip()


def _trim_class_history():
    if len(student_state["class_history"]) > 12:
        student_state["class_history"] = student_state["class_history"][-12:]


def _reset_pending_question_state() -> None:
    student_state["awaiting_answer"] = False
    student_state["pending_question_node_key"] = ""
    student_state["pending_question_text"] = ""
    student_state["pending_question_wrong_attempts"] = 0


def _arm_pending_question(task_info: dict) -> bool:
    question_text = str(task_info.get("whiteboard_question") or "").strip()
    has_question = bool(question_text)
    if not has_question:
        _reset_pending_question_state()
        return False

    student_state["awaiting_answer"] = True
    student_state["pending_question_node_key"] = str(
        task_info.get("task_name") or task_info.get("node_name") or ""
    ).strip()
    student_state["pending_question_text"] = re.sub(r"\s+", " ", question_text).strip()
    student_state["pending_question_wrong_attempts"] = 0
    return True


app = FastAPI(title="AI English Teacher API")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
voice_worker_manager = VoiceWorkerManager()


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    _ensure_student_question_schema()
    _ensure_knowledge_mastery_schema()
    _load_session_summary_from_db(CURRENT_STUDENT_ID)


@app.on_event("shutdown")
async def on_shutdown():
    await voice_worker_manager.stop_all()


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "null",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_origin_regex=(
        r"https://ai-english-teacher.*\.vercel\.app"
        r"|https://ai-english-teacher-o63p\.onrender\.com"
        r"|https?://(localhost|127\.0\.0\.1)(:\d+)?"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UserInput(BaseModel):
    text: str
    history: list[dict] = Field(default_factory=list)


class ClassInput(BaseModel):
    text: str
    action: str = "chat"
    history: list[dict] = Field(default_factory=list)


class LiveKitTokenRequest(BaseModel):
    roomName: str | None = None
    userId: str | None = None
    displayName: str | None = None


@app.post("/chat_stream")
async def chat_stream(
    request: UserInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    if _should_log_student_question(request.text):
        _save_student_question(
            db,
            request.text,
            source="chat",
            student_id=CURRENT_STUDENT_ID,
        )

    prompt_history, evicted_messages = _split_history_for_summary(request.history, keep_limit=6, evict_count=3)
    if evicted_messages:
        background_tasks.add_task(_bg_update_session_summary, evicted_messages)

    with stream_state_lock:
        session_summary = str(stream_state.get("session_summary", "") or "")

    def generate():
        student_profile_summary = _build_student_profile_summary(db, CURRENT_STUDENT_ID)
        yield from chat_with_teacher_stream(
            request.text,
            history=prompt_history,
            student_profile_summary=student_profile_summary,
            session_summary=session_summary,
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        background=background_tasks,
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/memory/summary")
async def get_memory_summary():
    with stream_state_lock:
        summary = str(stream_state.get("session_summary", "") or "")
    return {"summary": summary}


@app.get("/api/dashboard/data")
async def get_dashboard_data(db: Session = Depends(get_db)):
    total_errors = db.query(func.count(ErrorBook.id)).scalar() or 0

    radar_rows = (
        db.query(
            ErrorBook.grammar_point.label("name"),
            func.count(ErrorBook.id).label("value"),
        )
        .group_by(ErrorBook.grammar_point)
        .order_by(func.count(ErrorBook.id).desc(), ErrorBook.grammar_point.asc())
        .all()
    )
    radar_data = [{"name": row.name, "value": row.value} for row in radar_rows]

    recent_rows = (
        db.query(ErrorBook)
        .order_by(ErrorBook.created_at.desc(), ErrorBook.id.desc())
        .limit(3)
        .all()
    )
    recent_errors = [
        {
            "user_input": row.user_input,
            "error_tag": row.error_tag,
            "ai_comment": row.ai_comment,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in recent_rows
    ]

    recent_question_rows = (
        db.query(StudentQuestion)
        .filter(StudentQuestion.student_id == CURRENT_STUDENT_ID)
        .order_by(StudentQuestion.created_at.desc(), StudentQuestion.id.desc())
        .limit(5)
        .all()
    )
    recent_questions = [
        {
            "question_text": row.question_text,
            "source": row.mode or row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in recent_question_rows
    ]

    mastery_snapshot = _build_mastery_snapshot(db, student_id=CURRENT_STUDENT_ID, limit=5)

    return {
        "total_errors": total_errors,
        "radar_data": radar_data,
        "recent_errors": recent_errors,
        "recent_questions": recent_questions,
        "mastery_snapshot": mastery_snapshot,
    }


@app.post("/api/livekit/token")
async def create_livekit_token(request: LiveKitTokenRequest):
    if not livekit_is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "LiveKit is not configured. "
                "Please set LIVEKIT_WS_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, and VOICE_DEFAULT_ROOM."
            ),
        )

    try:
        token_payload = create_livekit_participant_token(
            room_name=request.roomName,
            user_id=request.userId,
            display_name=request.displayName,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create LiveKit token: {exc}") from exc

    structured_voice_log(
        "voice.token_worker_ensure_start",
        room_id=token_payload.room_name,
        room_name=token_payload.room_name,
        user_identity=token_payload.identity,
    )
    try:
        await voice_worker_manager.ensure_session(token_payload.room_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Voice worker startup failed: {exc}") from exc
    return token_payload.as_response()


@app.get("/api/livekit/worker-status")
async def get_livekit_worker_status(roomName: str):
    return voice_worker_manager.get_status(roomName)


@app.post("/course/exit")
async def exit_course():
    global student_state
    student_state["is_in_class"] = False
    student_state["current_task_index"] = 0
    student_state["class_history"] = []
    _reset_pending_question_state()
    student_state["last_consumed_answer_fingerprint"] = ""
    return {"status": "success", "message": "已成功重置微课状态"}


@app.post("/class_chat_stream")
async def handle_class_interaction_stream(request: ClassInput, db: Session = Depends(get_db)):
    global student_state
    prompt_history = [] if request.action == "start" else _pick_prompt_history(
        request.history,
        student_state["class_history"],
    )
    student_profile_summary = _build_student_profile_summary(db, CURRENT_STUDENT_ID)

    if request.action != "start" and _should_log_student_question(request.text):
        _save_student_question(
            db,
            request.text,
            source="class",
            student_id=CURRENT_STUDENT_ID,
        )

    def generate():
        global student_state

        if request.action == "start":
            student_state["is_in_class"] = True
            student_state["current_task_index"] = 0
            student_state["class_history"] = []
            _reset_pending_question_state()
            student_state["last_consumed_answer_fingerprint"] = ""
            user_msg = "老师好，我准备好上课了！"
        else:
            user_msg = request.text

        if student_state["current_task_index"] >= len(COURSE_TASKS):
            student_state["is_in_class"] = False
            _reset_pending_question_state()
            yield CLASS_COMPLETED_MESSAGE
            return

        current_task = _build_runtime_course_task(student_state["current_task_index"])
        is_answer_turn = _should_auto_advance_class_task(
            request.action,
            current_task,
            user_msg,
        )
        current_task["wrong_attempt_count"] = int(
            student_state.get("pending_question_wrong_attempts") or 0
        )

        def stream_teach_stage(
            task_info: dict,
            cue_text: str,
            response_mode: str,
            stage_kind: str = "",
        ):
            stage_filter = TaskCompletedBuffer(TASK_COMPLETED_MARKER)
            yield _build_whiteboard_stage_system_notice(task_info, stage_kind)
            for chunk in generate_agent_class_reply_stream(
                task_info,
                student_state["class_history"],
                cue_text,
                history=[],
                weakness_summary=student_profile_summary,
                response_mode=response_mode,
            ):
                visible_chunk = stage_filter.push(chunk)
                if visible_chunk:
                    yield visible_chunk

            remaining_text = stage_filter.finalize()
            if remaining_text:
                yield remaining_text

            stage_reply = stage_filter.clean_text.strip()
            if stage_reply:
                student_state["class_history"].append({"role": "assistant", "content": stage_reply})
                _trim_class_history()

        def stream_lesson_stages(
            task_info: dict,
            *,
            formula_cue: str,
            formula_mode: str,
            example_cue: str,
            example_mode: str,
            include_question: bool = True,
        ):
            stage_plan = list(task_info.get("stage_plan") or [])
            if stage_plan:
                last_stage_page_key = ""
                last_stage_title = ""
                for stage in stage_plan:
                    stage_kind = str(stage.get("stage_kind") or "").strip().lower()
                    stage_title = str(stage.get("title") or task_info.get("node_name") or task_info.get("task_name") or "当前知识点").strip()
                    stage_page_key = str(stage.get("page_key") or "").strip()
                    stage_content = str(stage.get("content") or "").strip()
                    stage_question = str(stage.get("question_text") or "").strip()
                    answer_rule = str(stage.get("answer_rule") or "").strip()
                    combined_question = (
                        f"老师提问：{stage_question}\n作答要求：{answer_rule}"
                        if stage_question and answer_rule
                        else (f"老师提问：{stage_question}" if stage_question else "")
                    )

                    if stage_content:
                        yield _serialize_whiteboard_update(
                            {
                                "action": "new_page",
                                "node_key": str(task_info.get("task_name") or task_info.get("node_name") or "").strip(),
                                "page_key": stage_page_key,
                                "title": stage_title,
                            }
                        )
                        yield _serialize_whiteboard_update(
                            {
                                "action": "append",
                                "node_key": str(task_info.get("task_name") or task_info.get("node_name") or "").strip(),
                                "page_key": stage_page_key,
                                "title": stage_title,
                                "content": stage_content,
                            }
                        )
                        yield "\n\n"

                        stage_task_info = dict(task_info)
                        stage_task_info["active_stage"] = stage
                        stage_task_info["whiteboard_question"] = combined_question
                        stage_cue = str(stage.get("transition_hint") or formula_cue).strip() or formula_cue
                        yield from stream_teach_stage(
                            stage_task_info,
                            stage_cue,
                            "teach_stage",
                            stage_page_key.split("::", 1)[-1] if stage_page_key else stage_kind,
                        )
                        last_stage_page_key = stage_page_key
                        last_stage_title = stage_title

                    if include_question and combined_question and stage.get("expects_answer"):
                        question_page_key = last_stage_page_key or stage_page_key or _build_whiteboard_page_key(task_info, f"question_{stage_kind or 'quiz'}")
                        question_title = last_stage_title or stage_title
                        yield _serialize_whiteboard_update(
                            {
                                "action": "question",
                                "node_key": str(task_info.get("task_name") or task_info.get("node_name") or "").strip(),
                                "page_key": question_page_key,
                                "title": question_title,
                                "question": combined_question,
                            }
                        )
                        task_with_question = dict(task_info)
                        task_with_question["whiteboard_question"] = combined_question
                        if _arm_pending_question(task_with_question):
                            yield "\n\n看上方悬浮题目，按要求作答。"
                        return
                return

            for event_str in _iter_whiteboard_stage_events(
                task_info,
                include_new_page=True,
                include_formula=True,
                stage_kind="formula",
            ):
                yield event_str
            yield "\n\n"
            yield from stream_teach_stage(
                task_info,
                formula_cue,
                formula_mode,
                "formula",
            )

            example_content = _build_whiteboard_example_content(task_info.get("reference") or "")
            if example_content:
                for event_str in _iter_whiteboard_stage_events(
                    task_info,
                    include_new_page=True,
                    include_example=True,
                    stage_kind="example",
                ):
                    yield event_str
                yield "\n\n"
                yield from stream_teach_stage(
                    task_info,
                    example_cue,
                    example_mode,
                    "example",
                )
                if include_question:
                    for event_str in _iter_whiteboard_stage_events(
                        task_info,
                        include_question=True,
                        stage_kind="example",
                    ):
                        yield event_str
                    if _arm_pending_question(task_info):
                        yield "\n\n看上方悬浮题目，按要求作答。"
                return

            if include_question:
                for event_str in _iter_whiteboard_stage_events(
                    task_info,
                    include_question=True,
                    stage_kind="formula",
                ):
                    yield event_str
                if _arm_pending_question(task_info):
                    yield "\n\n看上方悬浮题目，按要求作答。"

        if request.action == "start" and _is_opening_task(current_task):
            student_state["class_history"].append({"role": "user", "content": user_msg})
            _trim_class_history()
            yield from stream_lesson_stages(
                current_task,
                formula_cue="先根据当前白板讲清英语简单句的底层骨架，以及五大基本句型为什么本质上是五类谓语动词的说明书。",
                formula_mode="teach_opening_formula",
                example_cue="继续根据当前白板点破学生最常见的误区：不要一上来就扑向枝叶规则，要先抓住句子的核心骨架。",
                example_mode="teach_opening_example",
                include_question=False,
            )

            student_state["current_task_index"] += 1
            if student_state["current_task_index"] >= len(COURSE_TASKS):
                student_state["is_in_class"] = False
                _reset_pending_question_state()
                yield "\n\n" + CLASS_COMPLETED_MESSAGE
                return

            next_task = _build_runtime_course_task(student_state["current_task_index"])
            yield from stream_lesson_stages(
                next_task,
                formula_cue=NEXT_TASK_NUDGE,
                formula_mode="teach_formula",
                example_cue="继续讲刚刚追加到白板上的典型错误和对错对比，然后再提醒学生看悬浮题目作答。",
                example_mode="teach_example",
                include_question=True,
            )
            return

        current_filter = TaskCompletedBuffer(
            [TASK_COMPLETED_MARKER, RETRY_REQUIRED_MARKER]
            if is_answer_turn
            else TASK_COMPLETED_MARKER
        )
        class_db_buffer = AnalyzeDBLogBuffer(CLASS_DB_LOG_START, CLASS_DB_LOG_END)

        for chunk in generate_agent_class_reply_stream(
            current_task,
            student_state["class_history"],
            user_msg,
            history=prompt_history,
            weakness_summary=student_profile_summary,
            response_mode=(
                "feedback"
                if is_answer_turn
                else "teach"
            ),
        ):
            visible_chunk = class_db_buffer.push(current_filter.push(chunk))
            if visible_chunk:
                yield visible_chunk

        remaining_text = current_filter.finalize()
        if remaining_text:
            trailing_chunk = class_db_buffer.push(remaining_text)
            if trailing_chunk:
                yield trailing_chunk

        class_db_remaining = class_db_buffer.finalize()
        if class_db_remaining:
            yield class_db_remaining

        clean_reply = class_db_buffer.ai_comment.strip()
        answer_has_error_log = class_db_buffer.detected and class_db_buffer.completed
        if answer_has_error_log:
            _save_error_book_entry(
                db=db,
                user_input=user_msg,
                ai_comment=clean_reply,
                db_json_text=class_db_buffer.db_json_text,
            )

        student_state["class_history"].append({"role": "user", "content": user_msg})
        student_state["class_history"].append({"role": "assistant", "content": clean_reply})
        _trim_class_history()

        inferred_retry_required = False
        inferred_task_completed = False
        if is_answer_turn and not current_filter.detected and clean_reply:
            wrong_attempt_count = int(current_task.get("wrong_attempt_count") or 0)
            if answer_has_error_log:
                inferred_retry_required = wrong_attempt_count < 1
                inferred_task_completed = wrong_attempt_count >= 1
            else:
                inferred_task_completed = True

        should_advance = current_filter.saw(TASK_COMPLETED_MARKER) or inferred_task_completed
        if not should_advance:
            if is_answer_turn and (
                current_filter.saw(RETRY_REQUIRED_MARKER) or inferred_retry_required
            ):
                student_state["pending_question_wrong_attempts"] = int(
                    student_state.get("pending_question_wrong_attempts") or 0
                ) + 1
                yield "\n\n看上方悬浮题目，再答一次。"
            return

        if is_answer_turn:
            normalized_user_msg = re.sub(r"\s+", " ", str(user_msg or "")).strip()
            current_node_key = str(
                current_task.get("task_name") or current_task.get("node_name") or ""
            ).strip()
            student_state["last_consumed_answer_fingerprint"] = (
                f"{current_node_key}::{normalized_user_msg}"
            )
        _reset_pending_question_state()
        mastery_point = _resolve_course_mastery_point(student_state["current_task_index"])
        answer_was_student_success = not (
            is_answer_turn and answer_has_error_log
        )
        if mastery_point and answer_was_student_success:
            _update_knowledge_mastery(
                db,
                grammar_point=mastery_point,
                delta=MASTERY_DELTA_CLASS_SUCCESS,
                student_id=CURRENT_STUDENT_ID,
            )

        student_state["current_task_index"] += 1

        if student_state["current_task_index"] >= len(COURSE_TASKS):
            student_state["is_in_class"] = False
            _reset_pending_question_state()
            yield "\n\n" + CLASS_COMPLETED_MESSAGE
            return

        next_task = _build_runtime_course_task(student_state["current_task_index"])
        yield from stream_lesson_stages(
            next_task,
            formula_cue=NEXT_TASK_NUDGE,
            formula_mode="teach_formula",
            example_cue="继续讲刚刚追加到白板上的典型错误和对错对比，然后再提醒学生看悬浮题目作答。",
            example_mode="teach_example",
            include_question=True,
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
    )
