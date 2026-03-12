from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 引入我们刚才写好的两大核心模块
from diagnostician import analyze_sentence
from llm_wrapper import generate_teacher_message, ask_teacher_with_rag
from schemas import SentenceAnalysisReport

# ==========================================
# 目标 3：主动式 AI 老师的“教案”与“状态记录本”
# ==========================================
# 这是一棵课程树。每一个 node 都是一个上课环节。
COURSE_SYLLABUS = {
    "node_1": {
        "type": "teach", # 教学环节
        "text": "👨‍🏫 同学你好！今天我们要掌握英语中最核心的骨架：【主谓宾】(SVO)。\n就像中文一样，I 是主语，eat 是谓语，apple 是宾语。\n\n你能听懂吗？(回复任意内容进入测试)",
        "next": "node_2" # 讲完后，流转到节点 2
    },
    "node_2": {
        "type": "test", # 测试环节
        "text": "📝 随堂测试：请用英语翻译『那个男孩去学校』。(提示：boy, go, school)",
        "next": "node_3"
    },
    "node_3": {
        "type": "teach",
        "text": "🎉 太棒了！The boy goes to school. 这是一个完美的主谓结构！今天的第一节微课圆满结束啦~",
        "next": None # 课程结束
    }
}

# 全局状态机小本本（记录当前正在上课的进度）
# 注：为了简单，我们先用一个字典记录单人的进度。未来上线可以存进数据库。
student_state = {
    "is_in_class": False,    # 当前是否在上课？
    "current_node": "node_1" # 当前上到了哪个环节？
}

# 初始化 FastAPI 诊所前台
app = FastAPI(
    title="AI English Teacher API",
    description="支持成分高亮、复杂句式识别与 AI 温柔讲解的英语语法诊断接口",
    version="1.0.0"
)

# 配置跨域请求 (CORS) - 极其重要！
# 这允许你未来的前端网页（比如运行在 localhost:3000）成功呼叫你现在的后端
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 允许所有前端网址访问
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 定义前台接收的数据格式：前端只需要传一段文本过来
class UserInput(BaseModel):
    text: str

# 核心接口开张！当前端向 /analyze 发送数据时，执行这个函数
@app.post("/analyze", response_model=SentenceAnalysisReport)
async def analyze_student_sentence(request: UserInput):
    print(f"\n📩 收到前端发来的学生句子: {request.text}")
    
    # 1. 把句子交给 B超室医生 (spaCy) 解剖查错
    raw_report = analyze_sentence(request.text)
    
    # 2. 把初步报告交给 AI 老师 (DeepSeek) 撰写讲义
    final_report = generate_teacher_message(raw_report)
    
    # 3. 将最终的 JSON 报告直接扔回给前端
    print("📤 处理完毕，正在将报告返回给前端...")
    return final_report

# ==========================================
# 新业务窗口：接收学生的语法提问 (RAG 机制)
# ==========================================
class QuestionInput(BaseModel):
    question: str

@app.post("/ask")
async def ask_question(request: QuestionInput):
    print(f"\n🙋‍♂️ 收到学生提问: {request.question}")
    
    # 呼叫 RAG 引擎作答
    answer = ask_teacher_with_rag(request.question)
    
    print("📤 AI 老师回答完毕，正在返回...")
    return {"teacher_answer": answer}

# ==========================================
# 新业务窗口：主动上课交互通道 (状态机)
# ==========================================
class ClassInput(BaseModel):
    text: str
    action: str = "chat" # 动作类型：如果是 "start" 则重置课程，"chat" 则继续上课

@app.post("/class_chat")
async def handle_class_interaction(request: ClassInput):
    global student_state
    
    # 1. 触发器：学生点击了“开始上课”
    if request.action == "start":
        student_state["is_in_class"] = True
        student_state["current_node"] = "node_1" # 初始化到第一课
        node = COURSE_SYLLABUS["node_1"]
        return {"teacher_reply": node["text"], "status": "TEACHING"}
        
    # 2. 状态流转：正在上课过程中的对话
    if student_state["is_in_class"]:
        current_node_id = student_state["current_node"]
        current_node = COURSE_SYLLABUS[current_node_id]
        
        # 🌟 核心逻辑：如果当前环节是“随堂测试”，必须先批改！
        if current_node["type"] == "test":
            # 呼叫第一阶段的 B 超医生查错！
            report = analyze_sentence(request.text)
            
            if not report.is_grammar_correct:
                # 如果答错了，停留在当前节点，不准往下走，并给出纠错建议
                error_msg = report.errors[0].correction_suggestion
                return {
                    "teacher_reply": f"❌ 哎呀，有个小失误哦：\n{error_msg}\n\n请修改后再发一次吧！", 
                    "status": "WAITING_RETRY"
                }
        
        # 只要不是测试，或者测试全答对了，就流转到下一个节点！
        next_node_id = current_node["next"]
        
        if next_node_id:
            # 翻开教案的下一页
            student_state["current_node"] = next_node_id
            next_node = COURSE_SYLLABUS[next_node_id]
            return {"teacher_reply": next_node["text"], "status": "TEACHING"}
        else:
            # 教案翻完了，下课
            student_state["is_in_class"] = False
            return {"teacher_reply": "叮铃铃~ 课程已经结束啦，你可以自由提问或诊断句子了。", "status": "ENDED"}
            
    # 3. 如果没在上课却误入了这个接口
    return {"teacher_reply": "现在是课间休息时间哦，请点击『开始上课』唤醒我。"}