# AI English Grammar Teacher (AI 英语语法智能辅导系统)

![Vue](https://img.shields.io/badge/Frontend-Vue%203-4FC08D?style=for-the-badge&logo=vuedotjs)
![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi)
![spaCy](https://img.shields.io/badge/NLP-spaCy-09A3D5?style=for-the-badge)
![LLM](https://img.shields.io/badge/AI-DeepSeek%20%2F%20RAG-191919?style=for-the-badge)
![Deployment](https://img.shields.io/badge/Deployment-Vercel%20%26%20Render-success?style=for-the-badge)

## 🌟 项目简介 (Overview)
本项目是一个基于 Vue 3 与 FastAPI 构建的垂直领域 AI 教育 Agent 全栈应用。

在当前的 AI 教育赛道，纯 LLM 应用极易在严谨的语法规则上产生“幻觉”。本项目旨在破解这一核心痛点，通过**融合传统 NLP 规则引擎的确定性与 LLM 的生成能力**，打造了一款具备高容错率、高精确度可视化诊断，且能主动把控教学节奏的商业级 AI 私教产品。

**🚀 在线体验 Demo:** [点击这里访问 Vercel 公网环境](https://ai-english-teacher-77da.vercel.app/index.html)
*(注：后端部署于 Serverless 免费节点，首次对话可能需要约 30 秒的冷启动唤醒时间，请耐心等待。)*

---

## 🏗️ 系统架构与数据流 (System Architecture)

本项目彻底实现了前后端分离，核心业务逻辑包含三大链路：语法物理级诊断、RAG 教材问答、FSM 状态机课堂流转。

```mermaid
graph TD
    %% 用户端交互
    User((用户)) -->|输入一段文本| UI[前端界面 Vue3 + localStorage 状态持久化]

    %% 前端智能路由
    UI -->|意图识别与状态判断| Router{前端智能路由分发}
    
    Router -->|处于微课模式| RouteClass[请求 POST /class_chat]
    Router -->|包含中文或问号| RouteAsk[请求 POST /ask]
    Router -->|纯英文自然句| RouteAnalyze[请求 POST /analyze]

    %% 后端服务架构
    subgraph 后端大脑服务
        
        %% 记忆引擎 (新增)
        ChromaDB[(ChromaDB 向量记忆库)]

        %% 业务流 1：语法诊断流水线
        RouteAnalyze -->|1. 物理级解剖| spacy[spaCy 规则引擎]
        spacy -->|2. 依存句法分析+坐标提取| RawReport(原始 JSON 体检报告)
        
        %% 异步写库与同步读库逻辑
        RawReport -.->|若存在语法错误: FastAPI 异步后台任务| ChromaDB
        RouteAnalyze -.->|3. 语义检索历史错题| ChromaDB
        
        ChromaDB -.->|注入历史记忆上下文| deepseek_analyze[DeepSeek 大模型]
        RawReport -->|4. 情感化包装| deepseek_analyze
        
        deepseek_analyze -->|5. 生成专属点评讲义| FinalReport(最终完整 JSON 报告)

        %% 业务流 2：RAG 教材问答流水线
        RouteAsk -->|第一步：读取本地知识库| LocalDB[(本地 Markdown 教材)]
        LocalDB -->|拼接严格的 Prompt 上下文| deepseek_rag[DeepSeek RAG 问答引擎]
        deepseek_rag -->|生成不超纲的解答| QA_Answer(问答 JSON 数据包)

        %% 业务流 3：状态机课堂控制流
        RouteClass -->|读取全局进度| FSM{有限状态机 FSM}
        FSM -->|当前为 Teach 讲解节点| TeachNode[下发教学文案，状态步进]
        FSM -->|当前为 Test 测试节点| TestNode[拦截用户答题文本]
        
        TestNode -.->|后台静默调用| spacy
        spacy -.->|诊断失败| TestFail[阻断步进，打回重做并提示错误]
        spacy -.->|诊断成功| TestPass[允许步进，进入下一节点]
    end

    %% 前端渲染引擎接收结果
    FinalReport --> RenderEngine[marked.js 富文本与物理切片渲染引擎]
    QA_Answer --> RenderEngine
    TeachNode --> RenderEngine
    TestFail --> RenderEngine
    TestPass --> RenderEngine

    %% 多图层叠加与展示
    RenderEngine -->|基于绝对坐标渲染高亮/下划线/Tooltip| FinalDisplay[展示 AI 老师回复与高亮错题]
    FinalDisplay --> User
```

---

## 💡 核心技术亮点 (Technical Highlights)

### 1. 神经符号混合架构 (Neuro-Symbolic Hybrid Architecture)
摒弃了让 LLM 直接输出前端渲染坐标的不可靠方案。底层由 `spaCy` NLP 引擎进行物理级依存句法分析（Dependency Parsing），输出高精度的 JSON 绝对坐标；顶层 LLM（DeepSeek）仅基于结构化数据进行上下文学习（In-context Learning），生成教学文案。实现了**“工程底线防守 + AI 体验跃升”**。

### 2. 跨越时空的双域记忆系统 (Dual-Domain Memory System)
* **长期记忆 (Long-term Memory)**：接入 `ChromaDB` 本地持久化向量数据库。每次查出语法错误，系统都会将其转化为高维向量静默落盘；用户再次输入新句子时，通过语义检索召回历史相似错题，注入 LLM 提示词，实现“千人千面”的连贯性教学点评。
* **短期记忆 (Short-term Memory)**：前端利用 Vue 3 的 `watch` 深度监听与 `localStorage`，实现聊天记录的本地实时存档，刷新网页或重启浏览器数据不丢失，提供类似原生 App 的无缝体验。

### 3. 异步非阻塞性能优化 (Asynchronous Background Tasks)
针对向量数据库写库耗时的痛点，利用 FastAPI 的 `BackgroundTasks` 将“错题记入错题本”的重I/O操作剥离出主请求生命周期。前端无需等待数据库写入即可秒级获取 AI 回复，大幅降低感知延迟 (Latency)。

### 4. 复杂 DOM 多图层渲染与 Markdown 解析 (Advanced Vue Rendering)
在 Web 端实现了堪比原生客户端的富文本语法高亮交互。前端不仅接入了 `marked.js` 完美解析大模型输出的 Markdown 格式列表与粗体，还基于 Vue 3 手写了文本分块算法，实现了底层背景色、虚线下划线及悬浮 Tooltip 的三重物理叠加视图。

### 5. 基于 RAG 架构的强管控知识库 (RAG-based Knowledge Retrieval)
为避免 AI 教师“超纲教学”，系统接入了本地 Markdown 结构化教材库。触发提问时，系统动态提取关联教材切片作为强上下文约束 LLM，确保教学严谨性。

### 6. 状态机驱动的主动式课堂 (FSM-driven Session Management)
突破传统 Chatbot 的被动问答模式，在后端构建基于有限状态机 (FSM) 的会话中枢。主动发起 Teach（讲解）与 Test（测试）节点循环；在测试环节静默调用底层规则引擎进行校验，实现“不达标即拦截重做”的闭环教学。

---

## 🗂️ 项目结构 (Project Structure)
```text
.
├── main.py                # FastAPI 核心入口与异步任务分发中枢
├── diagnostician.py       # spaCy 语法规则引擎与查错逻辑
├── llm_wrapper.py         # DeepSeek API 接入与 RAG 查询逻辑
├── memory_manager.py      # ChromaDB 向量数据库读写管理中枢
├── schemas.py             # Pydantic 数据结构模型定义
├── frontend/              # 前端静态资源目录 (独立部署于 Vercel)
│   └── index.html         # Vue 3 前端界面与多图层渲染引擎
├── textbooks/             # RAG 本地教材知识库
│   └── module2_sentence_structures.md
├── requirements.txt       # 云端部署核心依赖清单
└── .env.example           # 环境变量配置模板
