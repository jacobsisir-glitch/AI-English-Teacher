import spacy
from schemas import SentenceAnalysisReport, SentenceComponent, GrammarError

# 1. 加载英语大脑模型
print("正在加载 spaCy 英语模型...")
nlp = spacy.load("en_core_web_sm")

# 图层一：基础成分字典（只抓取单个核心词）
BASIC_MAPPING = {
    "nsubj": "主语", "nsubjpass": "被动主语",
    "ROOT": "核心谓语", "dobj": "宾语", "pobj": "介词宾语",
    "amod": "定语", "advmod": "状语"
}

# 图层二：结构成分字典（连根拔起整个子树）
STRUCT_MAPPING = {
    "prep": "介词短语", "relcl": "定语从句", 
    "ccomp": "名词性从句", "advcl": "状语从句", 
    "xcomp": "非谓语/不定式"
}

def analyze_sentence(text: str) -> SentenceAnalysisReport:
    """
    接收句子，执行【解剖成分】+【诊断错误】，返回《全方位体检报告》
    """
    doc = nlp(text)
    errors = []
    basic_components = []
    structural_components = []

    # ==========================================
    # 第一阶段：全面解剖句子成分（为前端双层高亮做准备）
    # ==========================================
    for token in doc:
        # 1. 提取基础成分 (用于底层背景色)
        if token.dep_ in BASIC_MAPPING:
            basic_components.append(SentenceComponent(
                text=token.text,
                component_type=BASIC_MAPPING[token.dep_],
                start_char=token.idx,
                end_char=token.idx + len(token),
                is_complex=False
            ))
            
        # 2. 提取结构成分 (用于上层下划线)
        if token.dep_ in STRUCT_MAPPING:
            subtree_tokens = list(token.subtree)
            start_char = subtree_tokens[0].idx
            end_char = subtree_tokens[-1].idx + len(subtree_tokens[-1])
            structural_components.append(SentenceComponent(
                text=text[start_char:end_char],
                component_type=STRUCT_MAPPING[token.dep_],
                start_char=start_char,
                end_char=end_char,
                is_complex=True
            ))

    # ==========================================
    # 第二阶段：主治医生查错（主谓不一致拦截器）
    # ==========================================
    for token in doc:
        if token.pos_ == "VERB":
            subjects = [child for child in token.children if child.dep_ == "nsubj"]
            for subj in subjects:
                subj_person = subj.morph.get("Person")
                subj_number = subj.morph.get("Number")
                verb_tag = token.tag_ 
                
                # 重新定义“第三人称单数”的判断逻辑：
                is_third_person_singular = False
                
                if "Sing" in subj_number: # 前提必须是单数
                    # 如果它是名词或专有名词（比如 boy, Apple, Tom），那它天然就是第三人称
                    if subj.pos_ in ["NOUN", "PROPN"]: 
                        is_third_person_singular = True
                    # 如果它是代词（比如 he, she, it），则必须带有 '3' 的标签
                    elif "3" in subj_person:
                        is_third_person_singular = True
                
                if is_third_person_singular:
                    if verb_tag in ["VBP", "VB"]:
                        errors.append(GrammarError(
                            error_type="主谓不一致",
                            error_span=token.text,
                            start_char=token.idx, # 🎯 修正：错误高亮只框住错误的动词本身
                            end_char=token.idx + len(token),
                            correction_suggestion=f"主语 '{subj.text}' 是第三人称单数，谓语动词 '{token.text}' 应当使用三单形式（如加 -s 或 -es）。"
                        ))

    # ==========================================
    # 第三阶段：打包出具最终的《全方位体检报告》
    # ==========================================
    report = SentenceAnalysisReport(
        original_sentence=text,
        is_grammar_correct=(len(errors) == 0),
        basic_components=basic_components,         # 【修改点】装入基础成分
        structural_components=structural_components, # 【修改点】装入结构成分
        errors=errors,
        teacher_message=None
    )

    return report

# === 本地深度测试 ===
if __name__ == "__main__":
    # 注意这句测试文本：包含了一个定语从句 (who is standing there)，还包含了一个错误 (boy 搭配了 go)
    test_text = "The boy who is standing there go to school."
    print(f"\n正在深度解剖句子: '{test_text}'\n")
    
    report = analyze_sentence(test_text)
    print(report.model_dump_json(indent=4))