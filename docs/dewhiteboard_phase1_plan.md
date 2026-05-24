# 去白板化与 PPT 接入第一阶段工程方案

> 审计日期：2026-05-23
> 状态：方案阶段，未改代码

---

## 1. 总体结论

**可行性：高。** 白板链路在前端和后端都有清晰的隔离边界，可以软禁用而非删除。题目/反馈/任务推进状态机完全不依赖白板 DOM，只依赖 `[TASK_COMPLETED]` / `[RETRY_REQUIRED]` / `CLASS_DB` 标记和 `pending_question_state`。将白板替换为 PPT 课件播放器，风险可控，改动集中在 3 个文件内。

核心策略：**复用 `[SYSTEM:*]` 标签通道**，新增 `[SYSTEM:SLIDE:GOTO:id]` / `[SYSTEM:SLIDE:QUESTION:text]` / `[SYSTEM:SLIDE:REVEAL_ANSWER:text]` 三种指令，后端在 `stream_lesson_stages()` 中推送 slide 事件替代白板事件。

---

## 2. 当前白板链路定位

### 2.1 前端白板 DOM 位置

**文件**: `frontend/index.html`

| 区域 | 行号 | 说明 |
|------|------|------|
| 白板容器 | L360-455 | `<section class="stage-shell">` 整块区域 |
| 顶部状态栏 | L362-368 | "Whiteboard Stage" + 页码 + 未读红点 |
| 前进/后退按钮 | L370-386 | `goToPrevWhiteboardPage` / `goToNextWhiteboardPage` |
| 题目抽屉 | L388-428 | 悬浮题目 overlay（展开/收起） |
| 白板内容区 | L430-452 | `currentWhiteboardPage.title` + `.contents` 列表 |
| 空状态提示 | L454 | "等待老师板书注入..." |

### 2.2 前端白板事件解析函数

| 函数 | 行号 | 职责 |
|------|------|------|
| `extractWhiteboardPayload()` | L3865 | 从流式文本中提取 `<WBEVENT>JSON</WBEVENT>` 并解析 |
| `enqueueWhiteboardPayload()` | L3321 | 按 action 类型路由 payload（new_page 入队，其余直发） |
| `applyWhiteboardUpdate()` | L3348 | 执行 new_page / append / question / replace 的 DOM 状态变更 |
| `activatePendingWhiteboardStage()` | L3105 | 字幕开始流出时，激活对应 stage 的白板内容 |
| `appendClassStreamChunk()` | L3946 | 流式数据入口：解析 `[SYSTEM:]` 标签 → 白板提取 → 字幕分流 |
| `normalizeWhiteboardPage()` | L3263 | 规范化白板页面数据结构 |
| `findWhiteboardPageIndex()` | L3287 | 按 pageKey 或 nodeKey 定位页面 |
| `createWhiteboardFallbackPage()` | L3303 | 自愈机制：收到 append 但找不到目标页时创建占位页 |
| `goToPrevWhiteboardPage()` / `goToNextWhiteboardPage()` | L3475/L3483 | 手动翻页 |
| `syncWhiteboardUnreadState()` | L3032 | 更新未读红点状态 |
| `toggleQuestionDrawer()` | L2522 | 题目抽屉展开/收起切换 |
| `openQuestionDrawer()` / `collapseQuestionDrawer()` | L2489/L2484 | 题目抽屉状态控制（含 autoHide 定时器） |
| `triggerWhiteboardBlockFlash()` | L3247 | 新增白板块入场高亮动画 |
| `clearWhiteboardFxTimers()` / `clearQuestionDrawerTimers()` | L2468/L2474 | 定时器清理 |

关键响应式变量：

| 变量 | 用途 |
|------|------|
| `whiteboardPages` (ref array) | 所有白板页面 |
| `currentPageIndex` (ref int) | 当前展示页索引 |
| `currentWhiteboardPage` (computed) | 当前页对象 |
| `hasUnreadWhiteboardUpdate` (ref bool) | 未读红点标记 |
| `freshWhiteboardBlockKey` (ref str) | 新增块闪烁 key |
| `questionDrawerOpen` / `questionDrawerActiveKey` (ref) | 题目抽屉状态 |
| `isQuestionDrawerAvailable` / `isQuestionDrawerOpen` (computed) | 题目抽屉可用/可见 |
| `currentQuestionDisplayKey` (computed) | 当前题目的唯一标识 |
| `pendingWhiteboardStages` (array) | 已收到 event 但未激活的 stage 队列 |
| `activatedWhiteboardStageKeys` (Set) | 已激活过的 stage key 集合 |

### 2.3 后端白板推送位置

**文件**: `main.py`

| 函数 | 行号 | 推送内容 |
|------|------|------|
| `_serialize_whiteboard_update()` | L885 | 将 dict 包装为 `<WBEVENT>JSON</WBEVENT>\n` |
| `_iter_whiteboard_stage_events()` | L812 | 按需生成 new_page / append (formula) / append (example) / question 事件 |
| `_iter_whiteboard_events()` | L893 | 旧版白板事件生成器（已基本被 stage_plan 替代） |
| `_build_whiteboard_stage_system_notice()` | L807 | 生成 `[SYSTEM:WB_STAGE_READY::page_key]` |
| `stream_lesson_stages()` | L2054 | 主驱动函数：遍历 stage_plan → 推送白板 → 讲解 → 挂题 |
| `stream_teach_stage()` | L2025 | 单阶段讲解：先发 `WB_STAGE_READY` → 流式讲解 → 追加到 class_history |
| `handle_class_interaction_stream()` | L1979 | `/class_chat_stream` 端点入口 |

**白板事件流的完整路径**：

```
_build_runtime_course_task()
  → _build_stage_plan_from_reference()      // 从教材解析 [STAGE:*] 段落
  → stream_lesson_stages()                   // 端点内闭包
    → _serialize_whiteboard_update()         // new_page → SSE
    → _serialize_whiteboard_update()         // append content → SSE
    → stream_teach_stage()                   // [SYSTEM:WB_STAGE_READY::...] → 讲解
    → _serialize_whiteboard_update()         // question → SSE
    → _arm_pending_question()               // 挂起等待
```

---

## 3. 可复用机制

### 3.1 可直接复用的机制

以下机制与白板解耦，**零改动复用**：

| 机制 | 说明 |
|------|------|
| `[SYSTEM:*]` 标签解析 | 前端 `appendClassStreamChunk()` L3964 已有正则 `/\[SYSTEM:\s*([^\]]+)\]/g`，新增 slide 标签自动兼容 |
| `[TASK_COMPLETED]` / `[RETRY_REQUIRED]` | 流式标记过滤（`TaskCompletedBuffer`），不依赖白板 |
| `CLASS_DB` 隐藏日志 | `AnalyzeDBLogBuffer` 提取，写入 ErrorBook，不依赖白板 |
| `_arm_pending_question()` / `_reset_pending_question_state()` | 纯状态机，不涉及白板 |
| `_should_auto_advance_class_task()` | 纯文本匹配判断，不涉及白板 |
| `student_state` 状态机 | `current_task_index` / `awaiting_answer` / `class_history` 完全独立 |
| `COURSE_TASKS` 定义 | 只需增加 `slide_id` 字段 |
| `_build_runtime_course_task()` | 核心流程不变，只需增加 slide_id 注入 |
| `_build_stage_plan_from_reference()` | 教材解析不变，stage_plan 仍驱动教学节奏 |
| `generate_agent_class_reply_stream()` | LLM 调用完全不变，只是 prompt 改了 |
| 字幕/TTS 管道 | 完全不变 |

### 3.2 需要适配的机制

| 机制 | 适配方式 |
|------|----------|
| `stream_lesson_stages()` | 将 `_serialize_whiteboard_update()` 调用替换为 slide 标签输出 |
| `stream_teach_stage()` | 将 `WB_STAGE_READY` 改为 `SLIDE:GOTO` |
| `appendClassStreamChunk()` | 新增 slide 标签的 case 分支（与现有 `WB_STAGE_READY` 并列） |

---

## 4. 不应删除的逻辑

### 4.1 绝对不能动的逻辑

| 逻辑 | 原因 |
|------|------|
| `COURSE_TASKS` 数组 | 课程骨架，PPT 课件仍需要它来驱动教学顺序 |
| `student_state` 全部字段 | 状态机核心，任务推进、题目挂起、历史管理全靠它 |
| `_arm_pending_question()` | 题目挂起是教学交互的核心，PPT 模式下仍需要 |
| `_reset_pending_question_state()` | 同上 |
| `_should_auto_advance_class_task()` | 判断学生输入是否为题目作答，PPT 模式同样需要 |
| `TaskCompletedBuffer` | 流式标记检测，PPT 模式仍然需要 `[TASK_COMPLETED]` |
| `AnalyzeDBLogBuffer` | 错误日志入库，PPT 模式仍然需要 |
| `_build_runtime_course_task()` | 任务数据组装，PPT 模式仍然需要（需扩展 slide_id） |
| `_build_stage_plan_from_reference()` | 教材阶段解析，PPT 模式仍需要（驱动讲解顺序） |
| `_build_agent_class_messages()` | LLM 消息组装，PPT 模式只需改 prompt 内容 |
| `generate_agent_class_reply_stream()` | LLM 流式调用，完全不变 |
| `ErrorBook` / `KnowledgeMastery` | 数据持久化，完全不变 |
| `_save_error_book_entry()` / `_update_knowledge_mastery()` | 掌握度更新，完全不变 |
| `_resolve_course_mastery_point()` | 掌握度关联，完全不变 |

### 4.2 可以软禁用的逻辑（保留代码，不调用）

| 逻辑 | 禁用方式 |
|------|----------|
| `_serialize_whiteboard_update()` | 不再调用，保留函数 |
| `_iter_whiteboard_events()` | 不再调用，保留函数 |
| `_iter_whiteboard_stage_events()` | 不再调用，保留函数 |
| `_build_whiteboard_contents()` 系列 | 不再调用，保留函数 |
| `_build_whiteboard_update_payload()` | 不再调用，保留函数 |
| `_build_whiteboard_stage_system_notice()` | 不再调用，保留函数 |
| `extractWhiteboardPayload()` | 前端不再调用，保留函数 |
| `enqueueWhiteboardPayload()` | 前端不再调用，保留函数 |
| `applyWhiteboardUpdate()` | 前端不再调用，保留函数 |
| `activatePendingWhiteboardStage()` | 前端不再调用，保留函数 |
| 前端 `whiteboardPages` / `currentPageIndex` 相关 state | 保留变量声明，不渲染 |

---

## 5. 新 PPT 课件播放器设计

### 5.1 幻灯片资源约定

```
frontend/slides/
  ├── manifest.json          # 幻灯片映射表
  ├── sp_001_opening.png     # 课程导读与开场白
  ├── sp_002_sv.png          # 主谓结构（SV）
  ├── sp_003_svo.png         # 主谓宾结构（SVO）
  └── ...
```

### 5.2 manifest.json 格式

```json
{
  "slides": [
    {
      "slide_id": "sp_001_opening",
      "file": "sp_001_opening.png",
      "task_name": "课程导读与开场白",
      "label": "导读 · 五大基本句型总览"
    },
    {
      "slide_id": "sp_002_sv",
      "file": "sp_002_sv.png",
      "task_name": "主谓结构（SV）",
      "label": "主谓结构 · 不及物动词"
    }
  ]
}
```

### 5.3 前端 PPT 播放器 DOM 设计

**放在白板容器的同一位置** (`<section class="stage-shell">`)，替换内部内容：

```html
<!-- PPT 课件展示区（替换原 L360-455 的白板区域） -->
<section class="stage-shell w-11/12 max-w-6xl h-[65vh] mx-auto mt-6 ...">
  <!-- 顶部状态栏 -->
  <div class="absolute inset-x-10 top-8 flex items-center justify-between ...">
    <span>Slide Viewer</span>
    <span>{{ currentSlideLabel || 'Ready' }}</span>
  </div>

  <!-- PPT 图片主体 -->
  <div class="relative z-10 flex h-full items-center justify-center px-4 pb-16 pt-20">
    <img v-if="currentSlideSrc"
         :src="currentSlideSrc"
         :alt="currentSlideLabel"
         class="max-h-full max-w-full object-contain rounded-2xl shadow-2xl"
         style="background: #000;" />
    <div v-else class="text-slate-300 text-2xl">等待课件加载...</div>
  </div>

  <!-- 题目 overlay（保留现有题目抽屉逻辑，复用 CSS） -->
  <div v-if="slideQuestionVisible" class="...题目overlay...">
    {{ currentSlideQuestion }}
  </div>

  <!-- 答案 overlay -->
  <div v-if="slideAnswerVisible" class="...答案overlay...">
    {{ currentSlideAnswer }}
  </div>
</section>
```

### 5.4 前端 PPT 状态变量（新增）

```javascript
const slideManifest = ref([]);           // 从 manifest.json 加载
const currentSlideId = ref('');          // 当前 slide_id
const currentSlideSrc = computed(...);   // 根据 slide_id 计算图片 URL
const currentSlideLabel = computed(...); // 当前 slide 标签
const slideQuestionVisible = ref(false); // 题目 overlay 是否可见
const slideQuestionText = ref('');       // 当前题目文本
const slideAnswerVisible = ref(false);   // 答案 overlay 是否可见
const slideAnswerText = ref('');         // 当前答案文本
```

---

## 6. Slide 事件协议设计

### 6.1 协议定义

复用现有 `[SYSTEM:*]` 标签通道，新增三种指令：

```
[SYSTEM:SLIDE:GOTO:slide_id]
[SYSTEM:SLIDE:QUESTION:question_text]
[SYSTEM:SLIDE:REVEAL_ANSWER:answer_text]
```

**设计原则**：
- 纯文本标签，不需要 JSON 解析
- 利用现有 `[SYSTEM:` 前缀 → 前端正则已支持
- `GOTO` 触发图片切换
- `QUESTION` 触发题目 overlay
- `REVEAL_ANSWER` 触发答案 overlay

### 6.2 前端解析扩展

在 `appendClassStreamChunk()` 的 `[SYSTEM:]` 解析分支中（L3989 附近），新增 case：

```javascript
// 现有代码 L3989：
if (normalizedStatus.startsWith('WB_STAGE_READY::')) {
    // ... 现有白板逻辑，保留不动
}
// 新增：
else if (normalizedStatus.startsWith('SLIDE:GOTO:')) {
    const slideId = normalizedStatus.slice('SLIDE:GOTO:'.length).trim();
    handleSlideGoto(slideId);
}
else if (normalizedStatus.startsWith('SLIDE:QUESTION:')) {
    const question = normalizedStatus.slice('SLIDE:QUESTION:'.length).trim();
    handleSlideQuestion(question);
}
else if (normalizedStatus.startsWith('SLIDE:REVEAL_ANSWER:')) {
    const answer = normalizedStatus.slice('SLIDE:REVEAL_ANSWER:'.length).trim();
    handleSlideRevealAnswer(answer);
}
```

### 6.3 后端发送 Slide 事件

在 `stream_lesson_stages()` 中，将白板事件生成替换为 slide 事件：

```python
# 原来：
yield _serialize_whiteboard_update({...})  # new_page
yield _serialize_whiteboard_update({...})  # append content

# 改为：
slide_id = task_info.get("slide_id", "")
if slide_id:
    yield f"[SYSTEM:SLIDE:GOTO:{slide_id}]\n"
```

在 `stream_teach_stage()` 中：

```python
# 原来：
yield _build_whiteboard_stage_system_notice(task_info, stage_kind)

# 改为：
slide_id = task_info.get("slide_id", "")
if slide_id:
    yield f"[SYSTEM:SLIDE:GOTO:{slide_id}]\n"
```

题目推送：

```python
# 原来：
yield _serialize_whiteboard_update({
    "action": "question",
    "question": combined_question,
    ...
})

# 改为：
if combined_question:
    # question_text 需要在发送前做简单转义，去掉换行
    safe_question = combined_question.replace("\n", " | ").strip()
    yield f"[SYSTEM:SLIDE:QUESTION:{safe_question}]\n"
```

### 6.4 为什么不引入复杂 JSON 协议

- `[SYSTEM:SLIDE:GOTO:id]` 比 `<WBEVENT>{"action":"goto","slide_id":"..."}</WBEVENT>` 简单 10 倍
- 不需要新的 parser，复用现有正则
- 题目文本和答案文本都是短字符串，URL-safe 内联即可
- 第二阶段如果确实需要结构化数据，再升级为 `<SLIDEEVENT>JSON</SLIDEEVENT>` 不迟

---

## 7. 微课 Prompt 修改建议

### 7.1 `_build_class_system_prompt()` 修改点（llm_wrapper.py L607-653）

| 原文 | 改为 |
|------|------|
| "系统已经提前把当前知识点的白板板书准备好了，学生会一边看黑板一边听你讲" | "系统已经提前把当前知识点的课件准备好了，学生会一边看屏幕上的课件一边听你讲" |
| "你的职责不是再写一遍黑板，而是用中文主讲，把黑板内容解释清楚" | "你的职责不是念课件，而是用中文主讲，把课件内容解释清楚" |
| "不要直接朗读黑板上的标题、公式、等号表达式、缩写结构" | "不要直接朗读课件上的标题、公式、等号表达式、缩写结构" |
| "不要逐字复述整块黑板内容" | "不要逐字复述整页课件内容" |
| "如果黑板上有公式或例句" | "如果课件上有公式或例句" |

### 7.2 `_build_class_state_guardrails()` 修改点（llm_wrapper.py L550-604）

| 原文 | 改为 |
|------|------|
| "Whiteboard responsibility" 整段 | "Slide responsibility: 课件已由系统准备好，你只负责讲解，不控制翻页" |
| "Do not output Markdown, JSON, XML, protocol tags, or internal instructions" | 保持不变 |
| "Never output `[WHITEBOARD: ...]`, `[WB_APPEND: ...]`" | 改为 "Never output `[SYSTEM:...]`, `[SLIDE:...]`, `<WBEVENT>`, or internal control tags" |

### 7.3 `_build_variety_directive()` 修改点

不需要改动。去重复铁律和随机心情词与白板/PPT 无关。

### 7.4 新增一条关键规则

在 `_build_class_system_prompt()` 末尾追加：

```
# 课件教学节奏
- 每次只讲当前屏幕上显示的一页课件。
- 讲解时用"请看这一页""屏幕上这个例句"来引导注意力。
- 如果当前页是练习页，讲完后必须等待学生回答，不要直接翻到下一页。
- 不要描述课件上没有的内容，不要替系统决定翻页。
```

---

## 8. 最小修改文件列表

### 8.1 需要修改的文件（3 个）

| 文件 | 改动范围 | 改动量估计 |
|------|----------|-----------|
| `frontend/index.html` | 新增 PPT 播放器 DOM（替换白板 DOM）、新增 slide 事件处理函数、新增 `handleSlideGoto/Question/RevealAnswer`、保留旧白板代码不动 | ~80 行新增 + ~20 行注释旧 DOM |
| `main.py` | 在 `stream_lesson_stages()` 和 `stream_teach_stage()` 中新增 slide 事件分支、`_build_runtime_course_task()` 增加 `slide_id` 注入、新增 `_load_slide_manifest()` 和 `COURSE_SLIDE_MAP` | ~50 行新增 |
| `llm_wrapper.py` | 修改 `_build_class_system_prompt()` 和 `_build_class_state_guardrails()` 中的"白板"措辞为"课件"、删除白板标签禁令、新增课件教学规则 | ~15 行修改 |

### 8.2 需要新增的文件（2 个）

| 文件 | 用途 |
|------|------|
| `frontend/slides/manifest.json` | slide_id → 文件名映射 |
| `frontend/slides/*.png` | PPT 导出的幻灯片图片（用户手工放入） |

### 8.3 绝对不要动的文件

| 文件 | 原因 |
|------|------|
| `voice/` 目录下所有文件 | LiveKit、FunASR、VAD，与白板无关 |
| `speech_providers/` 目录下所有文件 | TTS，与白板无关 |
| `tts_client.py` | TTS 路由，与白板无关 |
| `livekit_utils.py` | LiveKit 令牌，与白板无关 |
| `database/models.py` | 数据库结构，与白板无关 |
| `database/database.py` | 数据库连接，与白板无关 |
| `tools/textbook_tool.py` | 教材工具，与白板无关 |
| `config.py` | 配置加载，与白板无关 |
| `data/textbooks/` | 教材内容，与白板无关 |
| `.env` / `.env.example` | 环境变量，与白板无关 |

---

## 9. 风险与回滚

### 9.1 风险点

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| PPT slide_id 与 stage_plan 阶段不同步 | 中 | 前端 `handleSlideGoto` 幂等，重复切同一 slide 无副作用 |
| 题目 overlay 与字幕不同步出现 | 低 | 复用现有题目抽屉的 `autoHideMs` 定时器逻辑 |
| 用户忘放 slide 图片导致空白 | 中 | 前端显示 fallback："等待课件加载..."，同时保留后端纯讲解能力 |
| prompt 修改后模型仍输出"白板"措辞 | 低 | prompt 中多给 2-3 个"课件/屏幕/这一页"的同义词 |
| manifest.json 与 COURSE_TASKS 不一致 | 低 | 后端启动时做一次校验，打印 warning |
| 旧白板代码被意外触发 | 低 | 所有旧函数保留但不再调用，加注释 `# DEPRECATED phase1` |

### 9.2 回滚方案

回滚只需 2 步：

1. Git revert 本次 commit
2. 前端重新显示白板 DOM（因为旧 DOM 只是注释掉，没有删除）

或者更简单：如果修改采用**功能开关**方式（在 `frontend/index.html` 中用一个 `usePptMode = ref(true)` 控制），回滚只需改一个布尔值。

### 9.3 建议功能开关

```javascript
// frontend/index.html 顶部配置区
const FEATURE_PPT_MODE = true;  // false = 回退到白板模式
```

```python
# main.py 顶部配置区
PPT_MODE_ENABLED = True  # False = 回退到白板模式
```

前端根据 `FEATURE_PPT_MODE` 渲染不同的 DOM（白板 vs PPT 播放器）。后端根据 `PPT_MODE_ENABLED` 决定发送 `<WBEVENT>` 还是 `[SYSTEM:SLIDE:*]`。

这样彻底消除回滚风险。

---

## 10. 下一步可执行的代码修改提示词

如果方案通过，给 Claude Code 的执行指令：

> 请按照 `docs/dewhiteboard_phase1_plan.md` 方案执行第一阶段代码修改：
>
> 1. 在 `main.py` 中：
>    - 新增 `PPT_MODE_ENABLED = True` 开关
>    - 新增 `COURSE_SLIDE_MAP` dict（task_name → slide_id）
>    - 新增 `_load_slide_manifest()` 函数，从 `frontend/slides/manifest.json` 加载
>    - 在 `stream_lesson_stages()` 中，如果 `PPT_MODE_ENABLED`，用 `[SYSTEM:SLIDE:GOTO:id]` 替代 `_serialize_whiteboard_update(new_page/append)`
>    - 在 `stream_teach_stage()` 中，用 `[SYSTEM:SLIDE:GOTO:id]` 替代 `_build_whiteboard_stage_system_notice()`
>    - 在题目推送处，用 `[SYSTEM:SLIDE:QUESTION:text]` 替代 `_serialize_whiteboard_update(question)`
>    - `_arm_pending_question()` 保持不变
>    - 所有旧白板函数保留不删，加 `# phase1: soft-deprecated` 注释
>
> 2. 在 `llm_wrapper.py` 中：
>    - 修改 `_build_class_system_prompt()` 中的"白板/黑板"措辞为"课件/屏幕"
>    - 修改 `_build_class_state_guardrails()` 中的白板责任段为课件责任段
>    - 新增"课件教学节奏"规则
>
> 3. 在 `frontend/index.html` 中：
>    - 新增 `FEATURE_PPT_MODE = true` 开关
>    - 新增 PPT 播放器 DOM（在 stage-shell 内，用 `v-if="pptMode"` 控制）
>    - 旧白板 DOM 改为 `v-if="!pptMode"` 包裹（保留不动）
>    - 新增 `handleSlideGoto(id)` / `handleSlideQuestion(text)` / `handleSlideRevealAnswer(text)` 函数
>    - 在 `appendClassStreamChunk()` 的 `[SYSTEM:]` 解析处新增三个 slide 分支
>    - 新增 `currentSlideId` / `currentSlideSrc` / `slideQuestionVisible` / `slideAnswerVisible` 等响应式变量
>    - 启动时 fetch `frontend/slides/manifest.json` 加载 `slideManifest`
>
> 4. 新增文件：
>    - `frontend/slides/manifest.json`（示例，含 2-3 个 slide 映射）
>    - 创建一个占位说明文件 `frontend/slides/README.md`，告诉用户如何放入 PNG
>
> 5. 不改动：
>    - `voice/`、`speech_providers/`、`tts_client.py`、`livekit_utils.py`
>    - `database/`、`config.py`、`tools/`
>    - `COURSE_TASKS` 数组结构
>    - `[TASK_COMPLETED]` / `[RETRY_REQUIRED]` / `CLASS_DB` 机制
>    - `ErrorBook` / `KnowledgeMastery` 表结构和写入逻辑
>
> 修改完成后，运行 `python main.py` 检查无导入错误，然后报告完成。
