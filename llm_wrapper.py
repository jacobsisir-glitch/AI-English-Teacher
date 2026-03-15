import os
import json
from openai import OpenAI
from dotenv import load_dotenv
from schemas import SentenceAnalysisReport
from memory_manager import recall_mistakes # 👈 新增：引入记忆读取模块

load_dotenv()

api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise ValueError("⚠️ 找不到 DEEPSEEK_API_KEY！请检查项目根目录是否有 .env 文件。")

client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

def generate_teacher_message(report: SentenceAnalysisReport, student_id: str = "user_Zeratul") -> SentenceAnalysisReport:
    """
    将完整的体检报告喂给 DeepSeek 大模型，并注入该学生的长期记忆！同时结合官方教材进行讲解！
    """
    print("🧠 正在跨海呼叫 DeepSeek 引擎...")
    
    # --- 1. 记忆模块（保持不变，这可是核心资产） ---
    memory_context = recall_mistakes(student_id, report.original_sentence)
    if memory_context:
        print(f"📚 成功提取到历史记忆，正在注入提示词：\n{memory_context}")
    else:
        print("📚 该句型没有发现历史错误记忆。")

    # --- 2. 新增：全自动扫描教材模块 ---
    import glob
    textbook_content = ""
    textbook_folder = "textbooks"
    if os.path.exists(textbook_folder):
        md_files = glob.glob(os.path.join(textbook_folder, "*.md"))
        for file_path in sorted(md_files):
            with open(file_path, "r", encoding="utf-8") as f:
                textbook_content += f"\n\n--- 【章节: {os.path.basename(file_path)}】 ---\n\n"
                textbook_content += f.read()

    report_json_str = report.model_dump_json(indent=2)
    
    # --- 3. 升级版 System Prompt：将教材、报告、记忆三合一 ---
    system_prompt = f"""
    你是一位温柔、耐心、充满活力的 AI 英语语法老师（未来会以二次元 Live2D 形象出现）。
    系统底层引擎已经完成了解剖，并以 JSON 格式提供给你了《全方位体检报告》。
    
    【官方教材内容】：
    {textbook_content}
    
    你的任务是：
    1. 仔细阅读这份体检报告。
    2. 【核心规则】虽然系统底层的 NLP 引擎提供了错误报告，但你作为最终的语法主考官，必须用自己的大脑重新审视句子。如果 NLP 说没错误，但你发现了明显的语法错误（如系动词后误加副词），请直接推翻 NLP 的结论，指出错误并结合教材讲解，绝对不能为了迎合空的报告而强行夸奖。
    3. 如果学生用了复杂句式，一定要先夸奖学生！
    4. 如果有语法错误（看 errors 列表，或你自行发现的上述情况），请用非常温柔、聊天般的语气向学生解释，切忌用生硬的教科书口吻。
    5. 🌟 核心要求：在解释错误或分析句子时，【务必结合上面的官方教材内容】！使用教材里的概念（比如SVO、系动词、表语等）来告诉学生为什么错。
    6. 结合系统给出的 correction_suggestion（或你自己指出的错误）进行讲解，并给出一个修改后的完整正确例句。
    7. 如果完全没有错误（且你亲自审视后也确认无误），就大力夸奖，并复习一下句子里用到的好词好句（可以引用教材里的概念）。
    8. 字数控制在 150-250 字左右，注意使用 Markdown 加粗核心语法名词。

    {memory_context} 
    （⚠️ 核心指令：如果上面的【AI 内部记忆】里提到了学生历史犯过类似的错误，请你务必在回答中用温柔但带点“小提醒”的语气，明确告诉他“你之前也犯过类似的错误哦，还记得当时那个句子吗...”，让他产生跨越时空的连贯感！）
    """

    user_prompt = f"这是学生的句子体检报告：\n{report_json_str}\n请你直接输出你想对学生说的话。"

    try:
        response = client.chat.completions.create(
            model="deepseek-chat", 
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.6 # 🌟 把温度从 0.4 调高到 0.6，让老师的讲解更加生动活泼！
        )
        report.teacher_message = response.choices[0].message.content
        
    except Exception as e:
        report.teacher_message = f"老师的脑电波暂时短路啦，没能连上 DeepSeek 总部~ (错误代码: {str(e)})"

    return report

def ask_teacher_with_rag(question: str) -> str:
    """
    RAG 问答引擎：读取本地教材，约束大模型回答
    """
    print(f"🔍 收到提问: '{question}'，AI 老师正在翻阅教材...")
    textbook_path = "textbooks/module2_sentence_structures.md"
    if not os.path.exists(textbook_path):
        return "老师把教材忘在办公室啦！(找不到教材文件)"
        
    with open(textbook_path, "r", encoding="utf-8") as f:
        textbook_content = f.read()
        
    system_prompt = f"""
    你是一位严谨且温柔的 AI 英语语法老师。
    请你**严格且仅仅基于**以下【官方教材】的内容来回答学生的问题。
    如果学生的问题超出了教材范围，请直接回答：“我们目前的课程大纲还没讲到这里哦，咱们先聚焦现在的知识点吧~”
    严禁自己编造教材外的内容。
    
    【官方教材内容】：
    {textbook_content}
    """
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            temperature=0.1
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"老师的脑电波暂时短路啦~ (错误代码: {str(e)})"
    
def ask_teacher_with_rag_stream(question: str):
    """
    流式 RAG 问答引擎：像水管一样源源不断地吐出文字
    """
    print(f"🔍 收到提问: '{question}'，AI 老师正在翻阅教材并准备流式输出...")
    # 🌟 升级版：自动扫描 textbooks 目录下所有的 .md 教材并拼装
    import glob
    textbook_content = ""
    textbook_folder = "textbooks"
    
    if not os.path.exists(textbook_folder):
        yield "老师的教材库不见啦！"
        return
        
    # 找到所有的 md 文件并按名字排序读取
    md_files = glob.glob(os.path.join(textbook_folder, "*.md"))
    for file_path in sorted(md_files):
        with open(file_path, "r", encoding="utf-8") as f:
            textbook_content += f"\n\n--- 【章节: {os.path.basename(file_path)}】 ---\n\n"
            textbook_content += f.read()
        
    # 🌟 升级后的 System Prompt：赋予 AI 老师更有趣的灵魂和更聪明的护栏
    system_prompt = f"""
    你是一位严谨、温柔且极其幽默的 AI 英语语法老师（未来会以二次元 Live2D 形象出现）。
    请你严格基于以下【官方教材】的内容来回答学生的问题。
    
    【核心授课要求】：
    1. 拒绝复读机：每次回答都要用不同的、自然的聊天开场白，绝对不要每次都死板地说“根据教材...”。你可以用“哈，这个问题问到点子上了！”或“来，老师给你变个魔术...”等生动的话术切入。
    2. 善用比喻：在讲解教材里的生硬概念时，尽量多用生活中生动形象的比喻（比如剧组与演员、房子与砖块等）。
    3. 聪明的边界感：如果学生问的问题在教材里【完全找不到任何关联线索】，请你不要生硬地拒绝，而是委婉幽默地引导：“哎呀，这个问题有点超纲咯，咱们的大纲还没推进到这里，咱们先聚焦眼前的知识点好不好呀~”
    
    【官方教材内容】：
    {textbook_content}
    """
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            temperature=0.6, # 🌟 核心魔法：把温度从 0.1 调高到 0.6，释放她的创造力！
            stream=True 
        )
        
        for chunk in response:
            if chunk.choices[0].delta.content is not None:
                yield chunk.choices[0].delta.content
                
    except Exception as e:
        yield f"老师的脑电波暂时短路啦~ (错误代码: {str(e)})"

def generate_agent_class_reply(task_info: dict, chat_history: list, user_message: str) -> str:
    """
    驱动 Agent 进行自主授课的核心引擎
    """
    # 构建当前任务的具体指令
    system_prompt = f"""
    你是一位极其幽默、擅长苏格拉底式提问法的特级英语语法 AI 老师。
    现在，你正在给学生上一对一的辅导课。
    
    【你当前的专属教学任务】：
    {task_info['goal']}
    
    【官方教材参考资料】：
    {task_info['reference']}
    
    【你的行为准则】：
    1. 每次回复不要超过 100 字，像微信聊天一样自然，每次只抛出一个小知识点或一个问题。
    2. 不要一次性把教材内容全倒给学生，要用提问的方式引导学生自己思考和造句。
    3. 如果学生答错了，温柔地纠正他；如果学生问了无关的问题，幽默地拉回正题。
    4. 🌟【终极机密任务】：作为老师，你需要时刻评估学生的掌握情况。当你通过多轮对话，确信学生已经完全理解了当前的【专属教学任务】，并且能正确回答你的考察问题时，请你立刻在你的回复的最末尾，另起一行，输出这个确切的暗号：[TASK_COMPLETED]。
    千万不要让学生察觉到这个暗号的存在！
    """
    
    # 组装消息（保留之前的聊天上下文，让 AI 知道聊到哪了）
    messages = [{"role": "system", "content": system_prompt}]
    for msg in chat_history:
        messages.append(msg)
    messages.append({"role": "user", "content": user_message})
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.7 # 给 Agent 足够的温度去自由发挥
        )
        return response.choices[0].message.content
    except Exception as e:
        return "老师的麦克风好像坏了，稍等哦~"