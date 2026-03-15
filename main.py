from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from diagnostician import analyze_sentence
# 🌟 优化点：把所有的 import 都统一整齐地放在最上面
from llm_wrapper import generate_teacher_message, ask_teacher_with_rag_stream, generate_agent_class_reply
from schemas import SentenceAnalysisReport
from memory_manager import save_mistake

# 🌟 全新的 Agent 教学任务库
COURSE_TASKS = [
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
    }
]

# 🌟 升级版状态管理
student_state = {
    "is_in_class": False,
    "current_task_index": 0, 
    "class_history": []      
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

CURRENT_STUDENT_ID = "user_Zeratul"

@app.post("/analyze", response_model=SentenceAnalysisReport)
async def analyze_student_sentence(request: UserInput, background_tasks: BackgroundTasks):
    print(f"\n📩 收到前端发来的学生句子: {request.text}")
    raw_report = analyze_sentence(request.text)
    
    if not raw_report.is_grammar_correct:
        for error in raw_report.errors:
            background_tasks.add_task(
                save_mistake,
                student_id=CURRENT_STUDENT_ID,
                original_sentence=request.text,
                error_type=error.error_type,
                suggestion=error.correction_suggestion
            )
            
    final_report = generate_teacher_message(raw_report, student_id=CURRENT_STUDENT_ID)
    print("📤 处理完毕，正在将报告返回给前端...")
    return final_report

class QuestionInput(BaseModel):
    question: str

@app.post("/ask")
async def ask_question(request: QuestionInput):
    print(f"\n🙋‍♂️ 收到学生流式提问: {request.question}")
    return StreamingResponse(ask_teacher_with_rag_stream(request.question), media_type="text/plain")

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
        return {"teacher_reply": "🎉 恭喜你！我们所有的语法特训任务都通关啦！现在退出微课模式咯~", "status": "ENDED"}

    current_task = COURSE_TASKS[student_state["current_task_index"]]
    print(f"🎯 正在派发当前教学任务: {current_task['task_name']}")
    
    raw_reply = generate_agent_class_reply(current_task, student_state["class_history"], user_msg)
    print(f"🤖 Agent 原始回复生成完毕。")
    
    is_task_completed = False
    if "[TASK_COMPLETED]" in raw_reply:
        print("🔑 触发通关秘钥：[TASK_COMPLETED]！准备推进进度！")
        is_task_completed = True
        clean_reply = raw_reply.replace("[TASK_COMPLETED]", "").strip()
    else:
        clean_reply = raw_reply

    student_state["class_history"].append({"role": "user", "content": user_msg})
    student_state["class_history"].append({"role": "assistant", "content": clean_reply})
    if len(student_state["class_history"]) > 12: 
        student_state["class_history"] = student_state["class_history"][-12:]

    if is_task_completed:
        student_state["current_task_index"] += 1
        clean_reply += "\n\n*(🎉 恭喜！当前任务通关，老师带你进入下一个知识点...)*"
        print(f"✅ 进度推进成功，下一个任务索引将变为: {student_state['current_task_index']}")

    return {"teacher_reply": clean_reply, "status": "TEACHING"}