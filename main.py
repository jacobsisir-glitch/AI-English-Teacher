import json
import os

from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
import uvicorn

import database.models
from config import DEFAULT_STUDENT_ID
from database.database import Base, engine, get_db
from database.models import ErrorBook
from diagnostician import analyze_sentence
# 🌟 优化点：把所有的 import 都统一整齐地放在最上面
from llm_wrapper import (
    generate_teacher_message,
    generate_teacher_message_stream,
    ask_teacher_with_rag_stream,
    generate_agent_class_reply,
    generate_agent_class_reply_stream,
    classify_user_intent,
)
from schemas import SentenceAnalysisReport
from memory_manager import save_mistake

# 🌟 全新的 Agent 教学任务库
COURSE_TASKS = [
    {
        "task_name": "课程导读与开场白",
        "goal": "这是微课的第一环。请用你毒舌、傲娇充满英式幽默的语气的导师人设，向学生宣读本节课的宏观大纲（参考00_grammar_overview.md）绝对不要提问任何具体的语法知识点。 你的目标只是进行课前Overview，并在最后冷冷地问一句：'你那可怜的脑容量准备好接收这些硬核知识了吗？' 或者引用莎士比亚的名言：你就一定要毁了这门优雅的语言吗？当学生回复类似'准备好了/来吧'等确认话语时，输出 [TASK_COMPLETED] 推进到下一关。",
        "reference": "本节课涵盖：动词的过去、现在、将来、过去将来；以及一般、进行、完成、完成进行状态。"
    },
    {
        "task_name": "引言与不及物动词",
        "goal": "向学生介绍英语语法的核心是动词，并确保学生理解第1类：不及物动词(intransitive verbs)及主谓结构(SV)。引导学生自己造一个 SV 的句子来证明他们懂了。",
        "reference": "绝大多数句子表达的含义其实只有一个：“什么+怎么样”... 第一种是能够独立完成动作的动词，称为不及物动词，对应的句子结构就是：主语 + 不及物动词。经典例句：The birds fly."
    },
    {
        "task_name": "单及物动词与宾语",
        "goal": "引导学生学习第2类动词：及物动词(transitive verbs)与宾语(object)的概念。确保学生明白为什么有些动词后面必须加动作的承受者。",
        "reference": "有一个动作的承受者的动词 主谓宾结构(SVO)... 如果只说'I love'，句意是不完整的，这类动词称为及物动词，动作的承受者称为宾语。经典例句：I love apples."
    },
    {
        "task_name": "双及物动词",
        "goal": "讲解第3类动词：双及物动词(ditransitive verbs)。让学生区分直接宾语和间接宾语。",
        "reference": "有2个动作承受者的动词 主谓双宾结构 (SVOO)... 教授的对象是间接宾语，教授的内容是直接宾语。经典例句：Jack teaches me English."
    },
    {
        "task_name": "主谓宾补与复杂及物动词",
        "goal": "引导学生学习第4类动词：复杂及物动词(complex-transitive verbs)及主谓宾补结构(SVOC)。让学生理解为什么有些动词在带宾语后还需要补语才能把意思说完整，并区分宾语补语与双宾语。",
        "reference": "有一个动作承受者（但需要补充）的动词 主谓宾补结构 (SVOC)。如果只说 Mary considers Tom 会觉得话没说完；对承受者 Tom 的补充信息称为宾语补语(object complement)，这类动词称为复杂及物动词。经典例句：Mary considers Tom smart."
    },
    {
        "task_name": "主系表与系动词",
        "goal": "讲解第5类动词：系动词(linking verbs)及主系表结构(SVP/SVC)。让学生理解系动词不是表示动作，而是把表语的信息「赋予」主语，表示状态或身份，并会辨认常见系动词（如 be, look, seem）和表语。",
        "reference": "并非表示特定动作，而是将动词后的信息赋予动词前的动词，表示连接状态。主系表结构 (SVP/SVC)。系动词后的补充信息称为主语补语/表语(predicative)。可理解为 Jacob = tall；Smith = in the room。经典例句：Jacob is tall. Smith is in the room. Jim looks very sad."
    },
    {
        "task_name": "动词的时间（过去、现在、将来、过去将来）",
        "goal": "向学生解释动词的四种时间坐标（现在、过去、将来、过去将来），确保学生理解每种时间对应的形态标志与典型用法，并能区分「过去将来」是站在过去看未来的概念。",
        "reference": "动词的时间有四种，分别是：过去、现在、将来、过去将来。现在：形态标志：动词原形或第三人称单数形式（加 -s/-es）。经典例句：I buy apples every day. 过去：形态标志：动词的过去式（通常加 -ed，或有不规则变化如 go -> went）。经典例句：I bought apples yesterday. 将来：形态标志：通常需要助动词 will / shall + 动词原形（或者用 be going to 结构）。经典例句：I will buy apples tomorrow. 过去将来：对于过去某个时间点而言的将来。形态标志：通常用过去时的助动词 would / should + 动词原形（或者用 was/were going to）。经典例句：He said he would buy apples."
    },
    {
        "task_name": "动词的状态（一般、进行、完成、完成进行）",
        "goal": "向学生解释动词的四种状态（一般、进行、完成、完成进行）及其形态标志，重点让学生理解每种状态背后的「潜台词」，能根据语境体会说话人的隐含意思。",
        "reference": "动词的状态有四种：进行、完成、完成进行、一般状态。进行状态：形态标志 be + v-ing。经典例句：I am eating an apple.（潜台词：别跟我说话，我嘴里有东西。）完成状态：形态标志 have/has/had + v-ed。经典例句：I have eaten the apple.（潜台词：苹果没了，我现在肚子很饱，不用叫我吃饭了。）完成进行状态：形态标志 have/has/had + been + v-ing。经典例句：I have been eating apples all morning.（潜台词：我嚼得腮帮子都酸了，到现在还在嚼，或者刚刚才停下。）一般状态：形态标志动词原形/过去式等。经典例句：I eat apples.（潜台词：我不挑食，我具备吃苹果的习惯或能力。）"
    },
    {
        "task_name": "聚焦「现在」的四大时态",
        "goal": "对比讲解一般现在、现在进行、现在完成、现在完成进行四种时态，向学生解释形态标志与典型例句；重点考察学生对「现在完成时」潜台词的理解（过去动作对现在的影响或持续到现在）。",
        "reference": "一般现在时态：形态标志动词原形以及第三人称单数。经典例句：I live in Beijing. She eats apples. I go to lunch at 12:30 every day. 现在进行时态：形态标志助动词be的变位 + 动词现在分词。经典例句：I am doing homework. 现在完成时态：过去发生的动作对现在造成了影响，或过去的动作一直持续到现在。形态标志助动词have变位 + 动词的过去分词。经典例句：I have lost my keys. I have eaten a carrot. 与 I ate a carrot. 的区别。现在完成进行时态：形态标志 have的变位 + been + 动词的现在分词。经典例句：I have been waiting for you for two hours!"
    },
    {
        "task_name": "聚焦「过去」的四大时态",
        "goal": "讲解一般过去、过去进行、过去完成、过去完成进行四种时态；重点区分一般过去时与过去完成时（过去的过去），确保学生理解过去完成时表示在过去的某个时间点之前已经发生并产生影响。",
        "reference": "一般过去时态：过去某个时间里发生的动作或状态，已彻底结束。形态标志动词过去式。经典例句：I loved her.（潜台词：现在不爱了，彻底结束了。）过去进行时态：形态标志助动词be的变位(be的过去式) + 动词现在分词。经典例句：I was taking a shower when the phone rang. 现在完成时、一般过去时、过去进行时的区别：现在完成时强调对现在而言是否完成及对现在的影响；一般过去时侧重过去的事实；过去进行时强调过去某时正在发生。过去完成时态：强调过去的某个时间以前发生的事情，对之后(但仍是过去)造成影响。形态标志 have的过去式 + 过去分词。经典例句：When I arrived at the station, the train had left. 过去完成进行时态：形态标志 have的过去式 + been + 现在分词。经典例句：He was exhausted because he had been working all night."
    },
    {
        "task_name": "聚焦「将来」的四大时态",
        "goal": "讲解一般将来、将来进行、将来完成、将来完成进行四种时态，结合教材例句说明形态标志与用法；要求学生能用将来完成进行时造句，表达到将来某时将持续完成的动作。",
        "reference": "一般将来时态：形态标志 will + 动原。经典例句：I will buy some milk. 将来进行时态：形态标志 will + be + 现在分词。经典例句：I will be having dinner. 将来完成时态：形态标志 will + have + 过去分词。经典例句：I will have finished the report by Friday. 将来完成进行时态：对于将来某个时间，不但已经完成了，并且还要持续完成的动作。形态标志 will + have + been + 现在分词。经典例句：By next month, I will have been working at my job for 10 years."
    },
    {
        "task_name": "时空穿越的「过去将来」",
        "goal": "向学生解释过去将来时态的概念（站在过去看未来），说明它在从句转述与虚拟语气中的用法；让学生能区分 would 表时态、表虚拟、表礼貌请求等不同用法。",
        "reference": "一般过去将来时态：对于过去某个时间点而言的将来，常用于从句中。形态标志 would(will的过去式) + 动原。经典例句：He promised that he would help me. 过去将来进行：would + be + 现在分词。例句：I thought he would be sleeping when I called. 过去将来完成：would + have + 过去分词，极常用于虚拟语气。经典例句：If I had money, I would have bought that car.（虚拟语气：如果当时有钱，我就已经买下那辆车了。）过去将来完成进行：would + have + been + 现在分词。例句：He told me that by the end of the year, he would have been living there for a decade. would 可作为 will 的过去式构成时态；也可用作虚拟语气；表示礼貌：Would you like to have a lunch with me?"
    }
]

# 🌟 升级版状态管理
student_state = {
    "is_in_class": False,
    "current_task_index": 0, 
    "class_history": []      
}

TASK_COMPLETED_MARKER = "[TASK_COMPLETED]"
CLASS_COMPLETED_MESSAGE = "🎉 恭喜你！我们所有的语法特训任务都通关啦！现在退出微课模式咯~"
NEXT_TASK_NUDGE = "好，进入下一关。请直接开始讲授本关的第一段内容。"
ANALYZE_META_START = "===META_START==="
ANALYZE_META_END = "===META_END==="
DB_LOG_START = "===DB_START==="
DB_LOG_END = "===DB_END==="

class TaskCompletedBuffer:
    """
    滑动窗口缓冲器：安全拦截被切片的 [TASK_COMPLETED]，其余字符正常放行。
    """
    def __init__(self, marker: str):
        self.marker = marker
        self.buffer = ""
        self.visible_parts = []
        self.detected = False

    def push(self, chunk: str) -> str:
        if not chunk:
            return ""

        released_chars = []

        for char in chunk:
            self.buffer += char

            if self.buffer == self.marker:
                self.detected = True
                self.buffer = ""
                continue

            while self.buffer and not self.marker.startswith(self.buffer):
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


def _save_error_book_entry(db: Session, user_input: str, ai_comment: str, db_json_text: str) -> None:
    try:
        payload = json.loads(db_json_text)
    except json.JSONDecodeError as exc:
        print(f"⚠️ ErrorBook JSON 解析失败: {exc}. 原始内容: {db_json_text}")
        return

    grammar_point = str(payload.get("grammar_point", "")).strip()
    error_tag = str(payload.get("error_tag", "")).strip()

    if not grammar_point or not error_tag:
        print(f"⚠️ ErrorBook JSON 缺少必要字段: {payload}")
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
        print(f"📝 ErrorBook 写入成功: grammar_point={grammar_point}, error_tag={error_tag}")
    except Exception as exc:
        db.rollback()
        print(f"⚠️ ErrorBook 写入失败: {exc}")


def _trim_class_history():
    if len(student_state["class_history"]) > 12:
        student_state["class_history"] = student_state["class_history"][-12:]

def _strip_task_completed(text: str) -> tuple[str, bool]:
    has_marker = TASK_COMPLETED_MARKER in text
    clean_text = text.replace(TASK_COMPLETED_MARKER, "").strip()
    return clean_text, has_marker


def _build_meta_chunk(payload: dict) -> str:
    return f"{ANALYZE_META_START}{json.dumps(payload, ensure_ascii=False)}{ANALYZE_META_END}"


def _build_analyze_stream_response(
    text: str,
    background_tasks: BackgroundTasks,
    db: Session,
) -> StreamingResponse:
    print(f"\n📩 收到前端发来的学生句子: {text}")
    raw_report = analyze_sentence(text)
    raw_report.teacher_message = ""

    if not raw_report.is_grammar_correct:
        for error in raw_report.errors:
            background_tasks.add_task(
                save_mistake,
                student_id=CURRENT_STUDENT_ID,
                original_sentence=text,
                error_type=error.error_type,
                suggestion=error.correction_suggestion
            )

    meta_payload = {
        "intent": "ANALYZE",
        "originalText": raw_report.original_sentence,
        "report": raw_report.model_dump()
    }

    def generate():
        yield _build_meta_chunk(meta_payload)

        db_log_buffer = AnalyzeDBLogBuffer(DB_LOG_START, DB_LOG_END)
        for chunk in generate_teacher_message_stream(raw_report, student_id=CURRENT_STUDENT_ID):
            visible_chunk = db_log_buffer.push(chunk)
            if visible_chunk:
                yield visible_chunk

        remaining_text = db_log_buffer.finalize()
        if remaining_text:
            yield remaining_text

        if db_log_buffer.detected:
            if db_log_buffer.completed:
                _save_error_book_entry(
                    db=db,
                    user_input=text,
                    ai_comment=db_log_buffer.ai_comment,
                    db_json_text=db_log_buffer.db_json_text,
                )
            else:
                print("⚠️ 检测到 DB_START，但未找到完整的 DB_END，已跳过 ErrorBook 写入。")

    print("📤 句法分析已完成，正在以 Meta + 文本流的形式返回前端...")
    return StreamingResponse(
        generate(),
        media_type="text/plain; charset=utf-8",
        background=background_tasks
    )


def _build_question_stream_response(text: str, include_meta: bool = False) -> StreamingResponse:
    print(f"\n🙋‍♂️ 收到学生流式提问: {text}")

    def generate():
        if include_meta:
            yield _build_meta_chunk({"intent": "QUESTION"})

        for chunk in ask_teacher_with_rag_stream(text):
            yield chunk

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")

app = FastAPI(title="AI English Teacher API")


def _build_cors_origins() -> list[str]:
    default_origins = {
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "https://ai-english-teacher-2psn.vercel.app",
        "https://ai-english-teacher-77da-6bwzywihd-jacobsisir-glitchs-projects.vercel.app",
    }
    extra_origins = {
        origin.strip()
        for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    }
    return sorted(default_origins | extra_origins)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserInput(BaseModel):
    text: str

CURRENT_STUDENT_ID = DEFAULT_STUDENT_ID

@app.post("/analyze")
async def analyze_student_sentence(
    request: UserInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    return _build_analyze_stream_response(request.text, background_tasks, db)

class QuestionInput(BaseModel):
    question: str

@app.post("/ask")
async def ask_question(request: QuestionInput):
    return _build_question_stream_response(request.question, include_meta=False)


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

    return {
        "total_errors": total_errors,
        "radar_data": radar_data,
        "recent_errors": recent_errors,
    }


@app.post("/practice_chat")
async def practice_chat(
    request: UserInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    print(f"\n🧭 收到统一练习入口请求: {request.text}")
    intent = classify_user_intent(request.text)
    print(f"🧠 LLM 语义路由判定结果: {intent}")

    if intent == "ANALYZE":
        return _build_analyze_stream_response(request.text, background_tasks, db)

    return _build_question_stream_response(request.text, include_meta=True)

class ClassInput(BaseModel):
    text: str
    action: str = "chat"

@app.post("/course/exit")
async def exit_course():
    global student_state
    print("\n🚪 收到退出微课请求，清理状态...")
    # 🌟 修复点：确保清理的是 current_task_index 而不是过期的 current_node
    student_state["is_in_class"] = False
    student_state["current_task_index"] = 0
    student_state["class_history"] = []
    return {"status": "success", "message": "已成功重置微课状态"}

@app.post("/class_chat")
async def handle_class_interaction(request: ClassInput):
    global student_state
    print(f"\n👩‍🏫 收到微课互动: action={request.action}, text='{request.text}'")
    
    if request.action == "start":
        print("🎬 正在初始化全新 Agent 微课状态...")
        student_state["is_in_class"] = True
        student_state["current_task_index"] = 0
        student_state["class_history"] = []
        user_msg = "老师好，我准备好上课了！"
    else:
        user_msg = request.text
        
    if student_state["current_task_index"] >= len(COURSE_TASKS):
        print("🎉 所有任务已通关，下课！")
        student_state["is_in_class"] = False
        return {"teacher_reply": CLASS_COMPLETED_MESSAGE, "status": "ENDED"}

    current_task = COURSE_TASKS[student_state["current_task_index"]]
    print(f"🎯 正在派发当前教学任务: {current_task['task_name']}")
    
    raw_reply = generate_agent_class_reply(current_task, student_state["class_history"], user_msg)
    print(f"🤖 Agent 原始回复生成完毕。")
    
    clean_reply, is_task_completed = _strip_task_completed(raw_reply)
    if is_task_completed:
        print("🔑 触发通关秘钥：[TASK_COMPLETED]！准备推进进度！")

    student_state["class_history"].append({"role": "user", "content": user_msg})
    student_state["class_history"].append({"role": "assistant", "content": clean_reply})
    _trim_class_history()

    if is_task_completed:
        student_state["current_task_index"] += 1
        print(f"✅ 进度推进成功，下一个任务索引将变为: {student_state['current_task_index']}")
        
        # 无缝衔接：自动触发下一关的第一段讲授内容（无需等待学生再发一句）
        if student_state["current_task_index"] < len(COURSE_TASKS):
            next_task = COURSE_TASKS[student_state["current_task_index"]]
            print(f"⏭️ 自动衔接下一关: {next_task['task_name']}")
            # 这里的“轻推”提示只用于让老师立即开讲下一关，不应向学生透露任何系统机制
            next_reply_raw = generate_agent_class_reply(next_task, student_state["class_history"], NEXT_TASK_NUDGE)
            next_reply, _ = _strip_task_completed(next_reply_raw)
            if next_reply:
                student_state["class_history"].append({"role": "assistant", "content": next_reply})
                _trim_class_history()
                clean_reply = (clean_reply + "\n\n" + next_reply).strip()
            return {"teacher_reply": clean_reply, "status": "TEACHING"}

        student_state["is_in_class"] = False
        clean_reply = (clean_reply + "\n\n" + CLASS_COMPLETED_MESSAGE).strip()
        return {"teacher_reply": clean_reply, "status": "ENDED"}

    return {"teacher_reply": clean_reply, "status": "TEACHING"}

@app.post("/class_chat_stream")
async def handle_class_interaction_stream(request: ClassInput):
    global student_state

    def generate():
        global student_state
        print(f"\n🌊 收到流式微课互动: action={request.action}, text='{request.text}'")

        if request.action == "start":
            print("🎬 正在初始化全新 Agent 微课状态（流式）...")
            student_state["is_in_class"] = True
            student_state["current_task_index"] = 0
            student_state["class_history"] = []
            user_msg = "老师好，我准备好上课了！"
        else:
            user_msg = request.text

        if student_state["current_task_index"] >= len(COURSE_TASKS):
            print("🎉 所有任务已通关，直接返回结课提示。")
            student_state["is_in_class"] = False
            yield CLASS_COMPLETED_MESSAGE
            return

        current_task = COURSE_TASKS[student_state["current_task_index"]]
        print(f"🎯 正在流式派发当前教学任务: {current_task['task_name']}")

        current_filter = TaskCompletedBuffer(TASK_COMPLETED_MARKER)
        for chunk in generate_agent_class_reply_stream(current_task, student_state["class_history"], user_msg):
            visible_chunk = current_filter.push(chunk)
            if visible_chunk:
                yield visible_chunk

        remaining_text = current_filter.finalize()
        if remaining_text:
            yield remaining_text

        clean_reply = current_filter.clean_text.strip()
        student_state["class_history"].append({"role": "user", "content": user_msg})
        student_state["class_history"].append({"role": "assistant", "content": clean_reply})
        _trim_class_history()

        if not current_filter.detected:
            return

        print("🔑 流式通关秘钥拦截成功：[TASK_COMPLETED] 已被过滤，准备推进进度！")
        student_state["current_task_index"] += 1
        print(f"✅ 流式进度推进成功，下一个任务索引将变为: {student_state['current_task_index']}")

        if student_state["current_task_index"] >= len(COURSE_TASKS):
            print("🎓 微课全部完成，准备退出微课模式。")
            student_state["is_in_class"] = False
            yield "\n\n" + CLASS_COMPLETED_MESSAGE
            return

        next_task = COURSE_TASKS[student_state["current_task_index"]]
        print(f"⏭️ 流式自动衔接下一关: {next_task['task_name']}")

        yield "\n\n"

        next_filter = TaskCompletedBuffer(TASK_COMPLETED_MARKER)
        for chunk in generate_agent_class_reply_stream(next_task, student_state["class_history"], NEXT_TASK_NUDGE):
            visible_chunk = next_filter.push(chunk)
            if visible_chunk:
                yield visible_chunk

        next_remaining_text = next_filter.finalize()
        if next_remaining_text:
            yield next_remaining_text

        next_reply = next_filter.clean_text.strip()
        if next_reply:
            student_state["class_history"].append({"role": "assistant", "content": next_reply})
            _trim_class_history()

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
    )
