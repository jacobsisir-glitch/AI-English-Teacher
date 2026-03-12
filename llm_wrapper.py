import os
import json
from openai import OpenAI
from dotenv import load_dotenv # 👈 引入刚刚安装的库

# 1. 尝试加载本地的 .env 文件（寻找保险箱）
load_dotenv()

# 2. 从环境里安全地获取 API Key
# 如果找不到，就抛出错误提醒开发者，而不是带着空的 Key 往下跑
api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise ValueError("⚠️ 找不到 DEEPSEEK_API_KEY！请检查项目根目录是否有 .env 文件。")

# 3. 带着安全的钥匙去连接大模型
client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com" # 确保这里的 url 和你原来用的一致
)
from schemas import SentenceAnalysisReport

# ==========================================
# ⚠️ 填入你从 DeepSeek 开放平台申请的 API 密钥
# ==========================================
API_KEY = "sk-79f76e37a42141bb9a7244b384f0c5f6" # 请替换为你真实的 DeepSeek API Key
BASE_URL = "https://api.deepseek.com" # 这是 DeepSeek 官方指定的通用接口地址

# 初始化客户端，把请求强制导向 DeepSeek 的服务器，而不是美国的 OpenAI
client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

def generate_teacher_message(report: SentenceAnalysisReport) -> SentenceAnalysisReport:
    """
    将完整的体检报告喂给 DeepSeek 大模型，让它生成一段平易近人的名师讲义
    """
    print("🧠 正在跨海呼叫 DeepSeek 引擎，AI 老师正在构思讲义...")
    
    # 将 Pydantic 对象转成 JSON 字符串
    report_json_str = report.model_dump_json(indent=2)
    
    system_prompt = """
    你是一位温柔、耐心、充满活力的 AI 英语语法老师（未来会以二次元 Live2D 形象出现）。
    系统底层引擎已经完成了解剖，并以 JSON 格式提供给你了《全方位体检报告》。
    
    你的任务是：
    1. 仔细阅读这份报告。
    2. 如果学生用了复杂句式（请检查 JSON 数据中的 structural_components 列表是否为空），一定要先夸奖学生！
    3. 如果有语法错误（看 errors 列表），请用非常温柔、聊天般的语气向学生解释，切忌用生硬的教科书口吻。
    4. 结合系统给出的 correction_suggestion 进行讲解，并给出一个修改后的完整正确例句。
    5. 如果完全没有错误，就大力夸奖，并复习一下句子里用到的好词好句。
    6. 字数控制在 80-150 字以内。
    """

    user_prompt = f"这是学生的句子体检报告：\n{report_json_str}\n请你直接输出你想对学生说的话。"

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",  # 🚀 极其关键：必须指定模型为 deepseek-chat
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.4 # 稍微给一点点创造力，让语气更自然
        )
        
        # 将 DeepSeek 生成的文案，无缝填入报告的最后一个空缺！
        report.teacher_message = response.choices[0].message.content
        
    except Exception as e:
        report.teacher_message = f"老师的脑电波暂时短路啦，没能连上 DeepSeek 总部~ (错误代码: {str(e)})"

    return report

# === 本地大满贯联调测试 ===
if __name__ == "__main__":
    from diagnostician import analyze_sentence
    
    test_text = "The boy who is standing there go to school."
    
    # 1. B超医生先做解剖和查错
    raw_report = analyze_sentence(test_text)
    
    # 2. 拿着报告去找 DeepSeek 老师写评语
    final_report = generate_teacher_message(raw_report)
    
    print("\n🎉 DeepSeek 接入成功！最终发送给前端的完美数据包：\n")
    print(final_report.model_dump_json(indent=4))

    import os

def ask_teacher_with_rag(question: str) -> str:
    """
    RAG 问答引擎：读取本地教材，约束大模型回答
    """
    print(f"🔍 收到提问: '{question}'，AI 老师正在翻阅教材...")
    
    # 1. 精准指向我们刚写的模块二教材
    textbook_path = "textbooks/module2_sentence_structures.md"
    if not os.path.exists(textbook_path):
        return "老师把教材忘在办公室啦！(找不到教材文件)"
        
    with open(textbook_path, "r", encoding="utf-8") as f:
        textbook_content = f.read()
        
    # 2. 组装 RAG 提示词（用教材内容锁死 AI 的自由发挥）
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
            temperature=0.1 # 极低的温度，扼杀大模型的发散性，保证教学严谨
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"老师的脑电波暂时短路啦~ (错误代码: {str(e)})"