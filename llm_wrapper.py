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

def generate_teacher_message(report: SentenceAnalysisReport, student_id: str = "user_zhouziyu") -> SentenceAnalysisReport:
    """
    将完整的体检报告喂给 DeepSeek 大模型，并注入该学生的长期记忆！
    """
    print("🧠 正在跨海呼叫 DeepSeek 引擎...")
    
    # 🌟 核心焊接点 1：翻旧账！去向量数据库里查这个学生以前有没有犯过类似的错
    memory_context = recall_mistakes(student_id, report.original_sentence)
    if memory_context:
        print(f"📚 成功提取到历史记忆，正在注入提示词：\n{memory_context}")
    else:
        print("📚 该句型没有发现历史错误记忆。")

    report_json_str = report.model_dump_json(indent=2)
    
    system_prompt = f"""
    你是一位温柔、耐心、充满活力的 AI 英语语法老师（未来会以二次元 Live2D 形象出现）。
    系统底层引擎已经完成了解剖，并以 JSON 格式提供给你了《全方位体检报告》。
    
    你的任务是：
    1. 仔细阅读这份报告。
    2. 如果学生用了复杂句式，一定要先夸奖学生！
    3. 如果有语法错误（看 errors 列表），请用非常温柔、聊天般的语气向学生解释，切忌用生硬的教科书口吻。
    4. 结合系统给出的 correction_suggestion 进行讲解，并给出一个修改后的完整正确例句。
    5. 如果完全没有错误，就大力夸奖，并复习一下句子里用到的好词好句。
    6. 字数控制在 80-150 字以内。

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
            temperature=0.4 
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
            temperature=0.1,
            stream=True # 🌟 核心魔法：告诉 DeepSeek 开启水管模式！
        )
        
        # 🌟 核心循环：只要 DeepSeek 吐出一个字，我们就 yield 扔出去一个字
        for chunk in response:
            if chunk.choices[0].delta.content is not None:
                yield chunk.choices[0].delta.content
                
    except Exception as e:
        yield f"老师的脑电波暂时短路啦~ (错误代码: {str(e)})"