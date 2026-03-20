import uuid

import chromadb

from config import CHROMA_DB_PATH, DEFAULT_STUDENT_ID


CHROMA_DB_PATH.mkdir(parents=True, exist_ok=True)
chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_PATH))
collection = chroma_client.get_or_create_collection(name="student_mistakes")


def save_mistake(student_id: str, original_sentence: str, error_type: str, suggestion: str):
    """Persist a student's grammar mistake for later recall."""
    mistake_id = str(uuid.uuid4())
    document_text = (
        f"原句: {original_sentence} | "
        f"错误类型: {error_type} | "
        f"建议: {suggestion}"
    )

    collection.add(
        documents=[document_text],
        metadatas=[{"student_id": student_id, "error_type": error_type}],
        ids=[mistake_id],
    )
    print(f"[Memory] Saved mistake for {student_id}: {error_type}")


def recall_mistakes(student_id: str, current_sentence: str, n_results: int = 2) -> str:
    """Recall similar past mistakes for the same student."""
    if collection.count() == 0:
        return ""

    results = collection.query(
        query_texts=[current_sentence],
        n_results=n_results,
        where={"student_id": student_id},
    )
    past_mistakes = results.get("documents", [[]])[0]

    if not past_mistakes:
        return ""

    lines = [
        "[AI 内部记忆，请结合这些历史问题进行讲解]",
        "该学生历史上出现过类似错误：",
    ]
    for index, mistake in enumerate(past_mistakes, start=1):
        lines.append(f"{index}. {mistake}")
    return "\n".join(lines)


if __name__ == "__main__":
    test_student = DEFAULT_STUDENT_ID

    print("--- 1. 模拟保存历史错误 ---")
    save_mistake(test_student, "The boy go to school.", "主谓不一致", "boy 是第三人称单数，go 应改为 goes")
    save_mistake(test_student, "I am like apple.", "动词误用", "be 动词和实义动词不能这样连用")

    print("\n--- 2. 模拟查询相似错误 ---")
    new_input = "She like banana."
    print(f"学生输入: {new_input}")

    print("\n--- 3. 检索历史记录 ---")
    memory_str = recall_mistakes(test_student, new_input)
    print(memory_str)
