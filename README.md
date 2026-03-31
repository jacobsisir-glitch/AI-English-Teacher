# AI English Teacher
## AI 虚拟英语老师

![Frontend](https://img.shields.io/badge/Frontend-HTML%20%2F%20CSS%20%2F%20JavaScript-2563EB?style=for-the-badge)
![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi)
![LLM](https://img.shields.io/badge/LLM-Streaming%20Subtitles-111827?style=for-the-badge)
![StateMachine](https://img.shields.io/badge/Class-Orchestrated%20Stages-8B5CF6?style=for-the-badge)

> 面向 B 站直播间场景的 AI 虚拟英语老师。  
> 当前项目已经从“传统语法分析器”重构为“沉浸式直播间 + 后端控白板 + LLM 讲字幕”的微课系统。

## 项目定位

当前系统有两条主链路：

- `POST /chat_stream`：闲聊 / 英语答疑
- `POST /class_chat_stream`：微课讲授

核心原则已经变成：

- 白板由后端状态机和教材结构驱动
- 字幕由 LLM 负责口语化讲解
- 白板与字幕共享同一套阶段推进，但不互相猜测对方内容

换句话说，当前课堂不是“模型边说边决定写什么板书”，而是：

1. 后端决定当前知识点、当前阶段、当前白板页
2. 前端接收白板事件并展示对应页面
3. LLM 只讲当前阶段该讲的内容
4. 学生答题后，后端再决定是否重答或推进

## 当前真实架构

### 1. 后端状态机是唯一真相源

微课节奏不再依赖前端猜字幕语义，也不再依赖模型临场控制白板。

当前由 `main.py` 负责：

- 维护 `COURSE_TASKS`
- 读取教材切片
- 构建运行时任务 `reference / llm_reference / whiteboard_question / stage_plan`
- 按阶段发送白板事件
- 决定何时等待学生作答、何时重试、何时推进下一节点

### 2. 双轨制并行课堂

当前微课链路已经固定为双轨制：

- 白板轨：后端直接发送 `<WBEVENT>{json}</WBEVENT>`
- 字幕轨：LLM 只输出中文口语解释

白板事件当前主要包括：

- `new_page`
- `append`
- `question`

前端不再从模型文本里猜白板 DSL，也不再依赖旧的 `WB_TOOL` / tool calling。

### 3. 教材正在迁移到“显式阶段脚本”

旧教材结构主要是：

- `【白板核心公式】`
- `【经典对错对比】`
- `【AI 主播话术与人设 Trigger】`

当前项目已经开始支持新的显式阶段结构，适合更细粒度的白板与字幕同步。  
当前已用于试点的章节包括：

- `### 五大基本句型导读 (Opening Overview)`
- `### 主谓结构 (SV Pattern)`

新结构示例：

```md
#### [STAGE:HOOK]
[WB_TITLE] 主谓结构（SV）· 开场引入
[WB_LINES]
- 主谓结构 = 英语句子的最低生存配置
- 主语出场，谓语发力，句子才算真的活着
[VOICE_GUIDE]
- 开场要继续保持傲娇名师口吻，但重点是优雅地引出“最低骨架”这个概念。
[STAGE_RULE]
- 这一页只负责引出主谓结构的舞台定位，不要直接讲错句，不要提问。
[TRANSITION]
- 现在把主谓结构的核心公式亮出来。

#### [STAGE:QUIZ]
[QUESTION] 请用英语说“那个男孩在跑步。”你也可以自己另造一个正确的 SV 句。
[ANSWER_RULE] 先给出英文句子，再用中文说一句为什么这里不用宾语。
```

当前支持的字段包括：

- `[STAGE:HOOK|FRAME|CORE|ERROR|REINFORCE|PREVIEW|QUIZ]`
- `[WB_TITLE]`
- `[WB_LINES]`
- `[VOICE_GUIDE]`
- `[STAGE_RULE]`
- `[TRANSITION]`
- `[QUESTION]`
- `[ANSWER_RULE]`

这意味着每个知识点不必再强行限制为 3 页白板，而是可以按教学需要拆成 4 到 6 个阶段。

### 4. LLM 只负责“讲”

`llm_wrapper.py` 现在的职责是：

- 构建闲聊 prompt
- 构建微课 prompt
- 按阶段生成字幕
- 清洗协议碎片、旧白板标签和异常输出

当前约束包括：

- 不再输出白板协议或系统提示词
- 不再把 `[WHITEBOARD: ...]`、`[WB_APPEND: ...]`、`<WBEVENT>` 漏进字幕
- 不再使用“约会 / 暧昧 / 私人感情生活”这类老师人设
- 对导读页允许更厚的讲解
- 对普通知识点页要求一页一轮完整讲解

### 5. 前端是直播间舞台，不是流程控制器

`frontend/index.html` 当前负责：

- 沉浸式直播间 UI
- 多页白板 `whiteboardPages`
- 双行字幕队列
- 顶部问题悬浮窗
- 学习报告面板

但前端不负责决定课程推进。  
推进依旧由后端状态机掌控。

## 当前课堂节奏

当前推荐节奏不是“一进知识点就把所有板书和问题全部抖出来”，而是：

1. 进入知识点
2. 白板显示当前阶段的一页
3. AI teacher 只讲这一页
4. 讲完后进入下一页
5. 最后才挂题
6. 学生回答
7. 后端决定重答或推进

对于当前已迁移的导读与 `SV`，大致会走：

### 五大基本句型导读

- `HOOK`
- `FRAME`
- `CORE`
- `REINFORCE`
- `PREVIEW`

### 主谓结构（SV）

- `HOOK`
- `CORE`
- `ERROR`
- `REINFORCE`
- `QUIZ`

## 学生作答逻辑

当前微课答题轮规则：

- 学生不必死改老师给的原句
- 也可以自己另造一个符合当前知识点的正确句子
- 第一次答错：老师嘲讽 + 解释错因 + 要求重答
- 第二次还错：老师给标准答案 + 点评，然后推进
- 答对后推进到下一个知识点

## 前端访问方式

FastAPI 已挂载静态前端：

```python
app.mount("/frontend", StaticFiles(...), name="frontend")
```

推荐访问：

```text
http://127.0.0.1:8000/frontend/index.html
```

## 当前接口

- `POST /chat_stream`
- `POST /class_chat_stream`
- `POST /course/exit`
- `GET /api/dashboard/data`
- `GET /api/memory/summary`
- `GET /frontend/index.html`

## 目录结构

```text
AIEnglish_grammar_teacher/
|-- frontend/
|   `-- index.html
|-- data/
|   |-- textbooks/
|   |   |-- 00_Grammar_Overview.md
|   |   |-- 01_Verb.md
|   |   |-- 02_Subordinate_Clause.md
|   |   `-- 03_Parts_of_Speech.md
|   `-- ai_teacher.db
|-- database/
|   |-- database.py
|   `-- models.py
|-- tools/
|   `-- textbook_tool.py
|-- llm_wrapper.py
|-- main.py
|-- Open_AI_teacher.bat
|-- config.py
|-- requirements.txt
`-- README.md
```

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/<your-account>/<your-repo>.git
cd AIEnglish_grammar_teacher
```

### 2. 配置环境变量

```bash
copy .env.example .env
```

示例：

```env
DEEPSEEK_API_KEY=your_api_key_here
DATABASE_URL=sqlite:///data/ai_teacher.db
DEFAULT_STUDENT_ID=TestUser
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 启动服务

```bash
uvicorn main:app --reload
```

或者使用：

```bat
Open_AI_teacher.bat
```

### 5. 打开前端

```text
http://127.0.0.1:8000/frontend/index.html
```

如果只想确认后端在线：

```text
http://127.0.0.1:8000/docs
```

## 当前教材说明

教材位于 `data/textbooks/`。

当前重点教材：

- `00_Grammar_Overview.md`
- `01_Verb.md`
- `02_Subordinate_Clause.md`
- `03_Parts_of_Speech.md`

其中：

- `00_Grammar_Overview.md` 已开始迁移到显式阶段脚本
- `01_Verb.md` 仍以旧结构为主，但可以继续迁移
- 系统兼容“显式阶段脚本”和“旧三段式结构”两种教材写法

## 已知现实约束

### 1. 前端当前依赖外部 CDN

当前前端入口仍直接引用：

- Tailwind CDN
- Vue CDN
- Axios CDN
- Marked CDN
- ECharts CDN
- Font Awesome CDN

如果本地网络阻断这些外链，页面可能出现样式缺失或图表加载失败。  
这属于当前前端部署现实，不是课堂状态机本身的问题。

### 2. 阶段化教材仍在迁移中

目前不是整套教材都已经迁到 `[STAGE:...]` 格式。  
所以当前系统是：

- 已迁移章节：走显式阶段计划
- 未迁移章节：走旧结构的兼容解析路径

### 3. 课堂节奏仍在继续打磨

虽然当前系统已经从“白板一次性倾倒全部内容”往阶段化推进迈了一步，但：

- 白板阶段时机
- 字幕长度
- 浮窗动画
- 页间切换体感

仍然在持续优化中。

## 当前最重要的文件

- [main.py](./main.py)：路由、微课状态机、阶段推进、白板事件
- [llm_wrapper.py](./llm_wrapper.py)：prompt、字幕生成、清洗规则
- [frontend/index.html](./frontend/index.html)：直播间 UI、白板、字幕、题窗、Dashboard
- [data/textbooks/00_Grammar_Overview.md](./data/textbooks/00_Grammar_Overview.md)：导读与五大句型教材
- [data/textbooks/01_Verb.md](./data/textbooks/01_Verb.md)：动词体系教材

## 下一步建议

如果继续沿当前架构推进，最值得优先做的是：

1. 把 `SVO / SVOO / SVOC / SVC` 也迁成显式阶段脚本
2. 把导读和正式知识点的白板 / 字幕节奏继续对齐
3. 把前端 CDN 依赖收束成本地可控资源
4. 继续统一教材里的人设话术，避免出现和课堂主风格不一致的比喻
