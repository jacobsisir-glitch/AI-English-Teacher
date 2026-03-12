from pydantic import BaseModel, Field
from typing import List, Optional

# ==========================================
# 模块一：句子成分解剖节点 (用于前端画线和打标签)
# ==========================================
class SentenceComponent(BaseModel):
    text: str = Field(description="该成分的具体文本，可以是一个词(如 He)或一个从句(如 what he said)")
    component_type: str = Field(description="成分中文标签，例如：主语、谓语、宾语、定语从句、不定式等")
    start_char: int = Field(description="在原句中的起始字符索引 (极其重要：用于前端精准高亮)")
    end_char: int = Field(description="在原句中的结束字符索引")
    is_complex: bool = Field(default=False, description="是否是复杂语法(如从句、不定式)，提示前端用特殊颜色标记")

# ==========================================
# 模块二：具体错误诊断单 (如果没有错，这个列表就为空)
# ==========================================
class GrammarError(BaseModel):
    error_type: str = Field(description="错误类型，如：主谓不一致、缺少冠词")
    error_span: str = Field(description="发生错误的具体片段文本")
    start_char: int = Field(description="错误的起始字符索引")
    end_char: int = Field(description="错误的结束字符索引")
    correction_suggestion: str = Field(description="给出的正确修改建议")

# ==========================================
# 模块三：全方位体检报告 (最终发给前端的大脑壳)
# ==========================================
class SentenceAnalysisReport(BaseModel):
    original_sentence: str = Field(description="学生输入的原始句子")
    is_grammar_correct: bool = Field(description="总体判断：句子的语法是否完全正确")
    
    # 【修改点】：将原来的 components 拆分为两层图层
    basic_components: List[SentenceComponent] = Field(description="底层图层：主谓宾等基础单词成分")
    structural_components: List[SentenceComponent] = Field(description="上层图层：从句、短语等长结构")
    
    errors: List[GrammarError] = Field(default_factory=list, description="发现的语法错误列表")
    teacher_message: Optional[str] = Field(
        default=None, 
        description="AI 老师用平实、温柔的语气讲给学生听的综合讲解词"
    )