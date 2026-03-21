from typing import List, Optional

from pydantic import BaseModel, Field


class SentenceComponent(BaseModel):
    text: str = Field(
        description="该成分对应的具体文本，可以是单词，也可以是一个更长的短语或从句。"
    )
    component_type: str = Field(
        description="成分标签，例如主语、谓语、宾语、定语从句、非谓语等。"
    )
    start_char: int = Field(
        description="该成分在原句中的起始字符索引，用于前端高亮。"
    )
    end_char: int = Field(
        description="该成分在原句中的结束字符索引。"
    )
    is_complex: bool = Field(
        default=False,
        description="是否为复杂语法结构，例如从句、不定式或其他长结构。"
    )


class GrammarError(BaseModel):
    error_type: str = Field(
        description="错误类型，例如主谓不一致、缺少冠词、时态错误等。"
    )
    error_span: str = Field(
        description="发生错误的具体文本片段。"
    )
    start_char: int = Field(
        description="错误在原句中的起始字符索引。"
    )
    end_char: int = Field(
        description="错误在原句中的结束字符索引。"
    )
    correction_suggestion: str = Field(
        description="对应的修正建议。"
    )


class SentenceAnalysisReport(BaseModel):
    original_sentence: str = Field(
        description="学生输入的原始句子。"
    )
    is_grammar_correct: bool = Field(
        description="整体判断：这句话的语法是否完全正确。"
    )
    basic_components: List[SentenceComponent] = Field(
        description="底层句子成分，例如主谓宾等基础结构。"
    )
    structural_components: List[SentenceComponent] = Field(
        description="更高层的结构成分，例如从句、短语、非谓语结构等。"
    )
    errors: List[GrammarError] = Field(
        default_factory=list,
        description="发现的语法错误列表；如果为空，表示未发现明确错误。"
    )
    teacher_message: Optional[str] = Field(
        default=None,
        description="AI 老师用傲娇、毒舌、带英式冷幽默但仍然专业的语气给学生的综合讲解词。"
    )
