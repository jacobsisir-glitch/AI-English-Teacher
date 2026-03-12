# AI English Grammar Teacher (AI 英语语法智能辅导系统)

![Vue](https://img.shields.io/badge/Frontend-Vue%203-4FC08D?style=for-the-badge&logo=vuedotjs)
![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi)
![spaCy](https://img.shields.io/badge/NLP-spaCy-09A3D5?style=for-the-badge)
![LLM](https://img.shields.io/badge/AI-DeepSeek%20%2F%20RAG-191919?style=for-the-badge)

## 项目简介 (Overview)
本项目是一个基于 Vue 3 与 FastAPI 构建的全栈 AI 教育交互应用。项目深度实践了 AI Vibe Coding 现代开发工作流，实现了从底层 NLP 算法、大语言模型工程化接入到跨端复杂前端 DOM 渲染的完整闭环。该系统旨在通过 AI 技术提供具备高度可控性与专业教学逻辑的英语语法辅导体验。

---

## 核心架构与技术亮点 (Architecture & Features)

### 1. NLP 与 LLM 混合语法诊断引擎 (Hybrid Diagnostic Engine)
- 工程挑战：纯 LLM 在输出精细化坐标以控制前端 DOM 渲染时容易出现幻觉，且结构化数据输出不够稳定。
- 解决方案：采用双层架构。底层接入 spaCy NLP 引擎进行物理级的依存句法分析（SVO 骨架提取与从句拆解），并输出高精度的 JSON 坐标数据；顶层接入大语言模型，基于底层生成的结构化数据进行上下文学习 (In-context Learning)，生成符合教学规范的纠错解析。

### 2. 基于 RAG 架构的动态知识库管控 (RAG-based Knowledge Retrieval)
- 工程挑战：需严格限制教学 AI 的发散边界，避免超纲解答或教学内容错误。
- 解决方案：设计并接入本地 Markdown 结构化教材库。前端通过正则匹配与智能路由拦截用户意图，命中提问逻辑后，后端启动 RAG 机制，将教材切片作为 Prompt 上下文强制 LLM 进行推理，确保输出结果贴合官方课程大纲。

### 3. 基于有限状态机的主动式课堂中枢 (FSM-driven Session Management)
- 工程挑战：传统问答型 Chatbot 无法主导多轮对话节奏，难以实现具有闭环属性的教学体验。
- 解决方案：在后端构建基于有限状态机 (FSM) 的会话控制中枢。将教学过程抽象为 Teach (知识讲解) 与 Test (随堂测试) 节点。系统在 Test 状态下接管用户输入，内部静默调用语法诊断流进行评估，实现“不达标即拦截重试”的控制权反转，大幅提升交互深度。

### 4. 复杂 DOM 多图层物理切片渲染 (Vue 3 Custom Rendering)
- 工程挑战：需在 Web 端实现堪比原生客户端的富文本语法高亮与交互反馈。
- 解决方案：脱离常规文本框渲染模式，基于 Vue 3 Composition API 手写文本分块与坐标映射算法。将后端返回的语法坐标映射为独立组件，实现底层背景色、虚线下划线及悬浮 Tooltip 解析的三重物理叠加视图，提供高稳定性的视觉反馈。

---

## 技术栈选型 (Technology Stack)

- 前端架构：Vue 3 (Composition API), HTML5, TailwindCSS, Axios
- 后端服务：Python, FastAPI, Uvicorn, Pydantic
- 核心算法：OpenAI SDK, spaCy (en_core_web_sm), DeepSeek Chat API
- 工程规范：python-dotenv, Git

---

## AI 辅助开发实践 (AI-Assisted Development Practice)
本项目开发全流程深度融入 AI 工具链，全面覆盖需求拆解、开发、测试与基建构建环节：
1. 架构设计与解构：将宏观教育需求拆解为可执行的微服务模块（诊断、问答、状态机调度）。
2. 跨端疑难攻坚：在 Vue 多图层渲染错位、FastAPI 异步路由设计等复杂工程节点，进行结对编程与报错分析。
3. 闭环效能验证：快速生成基础样板代码与测试用例，将核心精力聚焦于系统调度层与前端交互打磨，验证了 AI Coding 在全栈开发中的生产力。

---

## 本地部署与启动 (Quick Start)

### 1. 后端环境初始化
```bash
# 安装核心依赖
pip install fastapi uvicorn spacy openai python-dotenv pydantic

# 下载 spaCy 英文核心模型
python -m spacy download en_core_web_sm

# 环境变量配置
# 在根目录创建 .env 文件并注入 API 密钥：DEEPSEEK_API_KEY=your_api_key_here

# 启动服务
uvicorn main:app --reload