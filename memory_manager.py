import chromadb
import uuid

# 1. 初始化本地持久化向量数据库 (就像建一个本地的小仓库)
# 存在本地的 'chroma_db' 文件夹中，哪怕重启电脑数据也不会丢！
chroma_client = chromadb.PersistentClient(path="./chroma_db")

# 2. 创建一个名为 "student_mistakes" 的记忆集合 (Collection)
# 如果已经有了，就直接读取 (get_or_create)
collection = chroma_client.get_or_create_collection(name="student_mistakes")

def save_mistake(student_id: str, original_sentence: str, error_type: str, suggestion: str):
    """
    【写库】把学生犯的错，转化为多维向量存进数据库
    """
    # 自动生成一个唯一的 ID
    mistake_id = str(uuid.uuid4())
    
    # 我们把学生写错的原句，以及错误的类型拼接起来作为要向量化的核心文本
    document_text = f"原句：{original_sentence} | 错误类型：{error_type} | 建议：{suggestion}"
    
    # 存入 ChromaDB
    collection.add(
        documents=[document_text], # 这是会被转换成向量并被搜索的核心内容
        metadatas=[{"student_id": student_id, "error_type": error_type}], # 贴上元数据标签，方便以后按人名筛选
        ids=[mistake_id]
    )
    print(f"🧠 [记忆写入] 成功记下 {student_id} 的一个错误：{error_type}")


def recall_mistakes(student_id: str, current_sentence: str, n_results: int = 2) -> str:
    """
    【读库】当学生输入新句子时，去数据库里翻翻他以前有没有犯过类似的错
    """
    # 先检查这个集合里有没有数据，如果没有，直接返回空
    if collection.count() == 0:
        return ""
        
    # 根据学生当前输入的新句子，去向量空间里搜索最相似的旧账！
    results = collection.query(
        query_texts=[current_sentence], # 用新句子作为搜索词
        n_results=n_results,            # 找回最相似的前 2 个错误
        where={"student_id": student_id} # 极其重要：只搜这个学生自己的错题本！不能把别人的错算在他头上
    )
    
    # 把搜出来的历史错误拼接成一段文本，准备喂给大模型
    past_mistakes = results.get("documents", [[]])[0]
    
    if not past_mistakes:
        return ""
        
    # 把查到的旧账整理成一段话
    memory_context = "【AI 内部记忆（请结合此记忆给学生写评语）】：\n该学生在历史学习中，曾犯过以下类似错误：\n"
    for i, mistake in enumerate(past_mistakes):
        memory_context += f"{i+1}. {mistake}\n"
        
    return memory_context

# === 本地简单测试 ===
if __name__ == "__main__":
    test_student = "user_Zeratul" # 假设这是你的账号
    
    print("--- 1. 模拟昨天上课犯错 ---")
    save_mistake(test_student, "The boy go to school.", "主谓不一致", "boy是三单，go应该加es")
    save_mistake(test_student, "I am like apple.", "动词混用", "be动词和实义动词不能连用")
    
    print("\n--- 2. 模拟今天上课输入新句子 ---")
    new_input = "She like banana."
    print(f"学生输入了新句子: {new_input}")
    
    print("\n--- 3. 正在检索海马体... ---")
    memory_str = recall_mistakes(test_student, new_input)
    print(memory_str)