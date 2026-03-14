from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from diagnostician import analyze_sentence
from llm_wrapper import generate_teacher_message, ask_teacher_with_rag_stream
from schemas import SentenceAnalysisReport
from memory_manager import save_mistake # 👈 新增：引入记忆写入模块

COURSE_SYLLABUS = {
    "node_1": {
        "type": "teach",
        "text": "👨‍🏫 同学你好！今天我们要掌握英语中最核心的骨架：【主谓宾】(SVO)。\n就像中文一样，I 是主语，eat 是谓语，apple 是宾语。\n\n你能听懂吗？(回复任意内容进入测试)",
        "next": "node_2"
    },
    "node_2": {
        "type": "test",
        "text": "📝 随堂测试：请用英语翻译『那个男孩去学校』。(提示：boy, go, school)",
        "next": "node_3"
    },
    "node_3": {
        "type": "teach",
        "text": "🎉 太棒了！The boy goes to school. 这是一个完美的主谓结构！今天的第一节微课圆满结束啦~",
        "next": None
    }
}

student_state = {
    "is_in_class": False,
    "current_node": "node_1"
}

app = FastAPI(title="AI English Teacher API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserInput(BaseModel):
    text: str

# 假设当前使用网站的是你本人，设定专属 ID
CURRENT_STUDENT_ID = "user_zhouziyu"

@app.post("/analyze", response_model=SentenceAnalysisReport)
@app.post("/analyze", response_model=SentenceAnalysisReport)
async def analyze_student_sentence(request: UserInput, background_tasks: BackgroundTasks):
    print(f"\n📩 收到前端发来的学生句子: {request.text}")
    
    # 1. 把句子交给 B超室医生 (spaCy) 解剖查错
    raw_report = analyze_sentence(request.text)
    
    # 🌟 核心焊接点 2：全自动记仇机制！
    if not raw_report.is_grammar_correct:
        for error in raw_report.errors:
            # 🚨 修改这里！把原来直接调用 save_mistake(...) 改成让后台任务去执行
            background_tasks.add_task(
                save_mistake, # 注意：这里只写函数名，不要加括号
                student_id=CURRENT_STUDENT_ID,
                original_sentence=request.text,
                error_type=error.error_type,
                suggestion=error.correction_suggestion
            )
    
    # 2. 把初步报告交给 AI 老师 (DeepSeek) 撰写讲义（带上 ID 方便她去翻旧账）
    final_report = generate_teacher_message(raw_report, student_id=CURRENT_STUDENT_ID)
    
    print("📤 处理完毕，正在将报告返回给前端...")
    return final_report

class QuestionInput(BaseModel):
    question: str

@app.post("/ask")
async def ask_question(request: QuestionInput):
    print(f"\n🙋‍♂️ 收到学生流式提问: {request.question}")
    # 🌟 核心魔法：不再返回 JSON，而是返回一条接通的水管 (text/plain 流)
    return StreamingResponse(ask_teacher_with_rag_stream(request.question), media_type="text/plain")

class ClassInput(BaseModel):
    text: str
    action: str = "chat"

@app.post("/class_chat")
async def handle_class_interaction(request: ClassInput):
    global student_state
    
    if request.action == "start":
        student_state["is_in_class"] = True
        student_state["current_node"] = "node_1"
        node = COURSE_SYLLABUS["node_1"]
        return {"teacher_reply": node["text"], "status": "TEACHING"}
        
    if student_state["is_in_class"]:
        current_node_id = student_state["current_node"]
        current_node = COURSE_SYLLABUS[current_node_id]
        
        if current_node["type"] == "test":
            report = analyze_sentence(request.text)
            if not report.is_grammar_correct:
                error_msg = report.errors[0].correction_suggestion
                return {
                    "teacher_reply": f"❌ 哎呀，有个小失误哦：\n{error_msg}\n\n请修改后再发一次吧！", 
                    "status": "WAITING_RETRY"
                }
        
        next_node_id = current_node["next"]
        if next_node_id:
            student_state["current_node"] = next_node_id
            next_node = COURSE_SYLLABUS[next_node_id]
            return {"teacher_reply": next_node["text"], "status": "TEACHING"}
        else:
            student_state["is_in_class"] = False
            return {"teacher_reply": "叮铃铃~ 课程已经结束啦，你可以自由提问或诊断句子了。", "status": "ENDED"}
            
    return {"teacher_reply": "现在是课间休息时间哦，请点击『开始上课』唤醒我。"}