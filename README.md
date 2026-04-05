# AI English Teacher

一个以 `FastAPI + 前端直播间 UI + LiveKit + FunASR` 为核心的 AI 英语老师项目。

当前仓库已经不只是传统聊天页，而是同时支持：

- 文本闲聊 / 答疑
- 微课白板讲解
- 实时语音输入转写
- 语音结果回流到原有聊天链路

## 项目概览

这个项目面向“AI 英语老师”场景，核心思路是把几条能力链路合在一起：

- `POST /chat_stream`
  处理普通聊天、问答、翻译、语法说明等文本请求
- `POST /class_chat_stream`
  处理带白板节奏的微课讲解
- `POST /api/livekit/token`
  为浏览器语音模式签发 LiveKit token，并确保房间级语音 worker 已启动
- `GET /api/livekit/worker-status`
  返回房间语音 worker 的状态和最近一次关键事件

语音模式下，浏览器麦克风音频会先进入 LiveKit 房间，再由后端 worker 订阅、做 VAD 切分、送入 FunASR，最终把识别出的 `partial` / `final` 文本重新送回前端，并把 `final` 文本提交到原有聊天链路。

## 当前能力

- 文本聊天：支持流式返回 AI 回复
- 微课模式：支持教材驱动、白板事件驱动和阶段化讲解
- 语音模式：支持浏览器直接说话，实时显示 partial / final transcript
- 语音回流：最终识别结果会自动作为学生输入进入聊天区
- 调试能力：前端有语音调试面板，后端有结构化语音日志
- 一键启动：提供 Windows 启动脚本 [Open_AI_teacher.bat](/d:/AIEnglish_grammar_teacher/Open_AI_teacher.bat)

## 目录结构

```text
.
├─ frontend/                前端页面与语音 UI
├─ voice/                   LiveKit + VAD + FunASR 语音链路
├─ data/                    数据库与教材数据
├─ database/                数据层代码
├─ scripts/                 验证和辅助脚本
├─ tools/                   工具文件
├─ main.py                  FastAPI 入口
├─ llm_wrapper.py           LLM prompt 与流式包装
├─ config.py                环境变量与运行参数
├─ livekit_utils.py         LiveKit token 与配置辅助
├─ requirements.txt         Python 依赖
└─ Open_AI_teacher.bat      Windows 一键启动脚本
```

## 技术栈

- 后端：FastAPI、Uvicorn、SQLAlchemy
- 大模型接入：OpenAI 兼容接口
- 实时语音：LiveKit
- 语音识别：FunASR WebSocket
- 语音切分：Silero VAD
- 前端：HTML、CSS、JavaScript、Vue 3 CDN 版

## 运行前准备

### 1. Python 依赖

```bash
pip install -r requirements.txt
```

当前 [requirements.txt](/d:/AIEnglish_grammar_teacher/requirements.txt) 主要依赖：

- `fastapi`
- `uvicorn`
- `openai`
- `sqlalchemy`
- `python-dotenv`
- `livekit-api`
- `livekit`
- `websockets`
- `silero-vad`
- `onnxruntime`

### 2. 环境变量

先复制 `.env.example`：

```bash
copy .env.example .env
```

最少需要关注这些配置：

```env
DEEPSEEK_API_KEY=your_api_key_here
DATABASE_URL=sqlite:///data/ai_teacher.db
DEFAULT_STUDENT_ID=TestUser

LIVEKIT_WS_URL=wss://your-livekit-host
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret
VOICE_DEFAULT_ROOM=ai-teacher-room

FUNASR_WS_URL=wss://127.0.0.1:10095
FUNASR_MODE=2pass
FUNASR_MODEL_NAME=

FUNASR_FINAL_WAIT_OFFLINE_MS=5000
FUNASR_FINAL_WAIT_FALLBACK_MS=1000
FUNASR_FINAL_DRAIN_WINDOW_MS=4000
FUNASR_FINAL_RESCUE_WAIT_MS=6000
FUNASR_LATE_FINAL_GRACE_MS=12000

SILERO_VAD_THRESHOLD=0.38
SILERO_MIN_SILENCE_MS=1200
SILERO_PRE_SPEECH_MS=480
SILERO_MIN_SPEECH_MS=320
SILERO_SPEECH_END_HOLD_MS=720
```

完整示例见 [.env.example](/d:/AIEnglish_grammar_teacher/.env.example)。

## 启动方式

### 方式一：手动启动

先启动后端：

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

前端访问：

```text
http://127.0.0.1:8000/frontend/index.html
```

如果要使用语音模式，还需要额外准备：

- LiveKit 服务
- FunASR WebSocket 服务
- 本机浏览器麦克风权限

### 方式二：Windows 一键启动

仓库已提供 [Open_AI_teacher.bat](/d:/AIEnglish_grammar_teacher/Open_AI_teacher.bat)。

这个脚本会按顺序：

1. 校验 Conda、项目目录和外部工具路径
2. 激活指定 Conda 环境
3. 检查并启动 LiveKit
4. 检查并启动 FunASR
5. 检查并启动 FastAPI 后端
6. 自动打开前端页面

脚本顶部需要按你本机环境修改这些路径和参数：

- `CONDA_ACTIVATE_BAT`
- `CONDA_ENV_NAME`
- `PROJECT_DIR`
- `LIVEKIT_EXE`
- `FUNASR_WORKDIR`
- `FUNASR_SCRIPT`
- `FUNASR_SSL_CERT`
- `FUNASR_SSL_KEY`

## 语音链路说明

当前语音链路位于 [voice/](/d:/AIEnglish_grammar_teacher/voice)：

- [voice/livekit_room_bridge.py](/d:/AIEnglish_grammar_teacher/voice/livekit_room_bridge.py)
  管理房间、参与者、音频订阅和整条实时识别流程
- [voice/funasr_client.py](/d:/AIEnglish_grammar_teacher/voice/funasr_client.py)
  负责与 FunASR WebSocket 通信，接收 `partial` / `final`
- [voice/vad_controller.py](/d:/AIEnglish_grammar_teacher/voice/vad_controller.py)
  用 Silero 做语音起止检测
- [voice/audio_buffer.py](/d:/AIEnglish_grammar_teacher/voice/audio_buffer.py)
  做音频缓存、归一化和预缓冲
- [voice/transcript_publisher.py](/d:/AIEnglish_grammar_teacher/voice/transcript_publisher.py)
  把识别结果发布回 LiveKit 文本流
- [voice/session_state.py](/d:/AIEnglish_grammar_teacher/voice/session_state.py)
  负责结构化日志和会话状态

语音模式的大致流程：

1. 浏览器请求 `POST /api/livekit/token`
2. 后端签发 token，并确保房间 worker 已启动
3. 浏览器加入 LiveKit 房间并发布麦克风
4. 后端 worker 订阅音轨并转成 `16k / mono`
5. Silero VAD 识别 `speech_start` / `speech_end`
6. 音频片段流式发送给 FunASR
7. FunASR 返回 `partial` 和 `final`
8. `final` 文本发布到前端并自动提交到聊天链路

## 最近这批语音更新

这次上传的更新重点在“长句语音识别稳定性”和“前端流式显示链路”：

- 增加了更长的 FunASR `offline final` 等待与补救窗口
- 增加了 late final recovery，尽量救回超时后才到达的 `2pass-offline`
- 对过短 partial fallback 做了更严格的抑制
- 增加了 VAD 结束保持时间，减少一句话被切成多段
- 前端增加了更多语音与聊天流式调试日志
- 语音转文本结果现在会更稳定地回流到聊天消息区

本次关键配置位于：

- [config.py](/d:/AIEnglish_grammar_teacher/config.py)
- [.env.example](/d:/AIEnglish_grammar_teacher/.env.example)
- [Open_AI_teacher.bat](/d:/AIEnglish_grammar_teacher/Open_AI_teacher.bat)

## 已知限制

当前项目里，启动脚本默认拉起的 FunASR 还是中文模型配置：

- `speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404`
- `speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online`

这意味着：

- 中文长句通常更稳
- 英文长句能识别，但更容易出现拼写错误、碎片化 partial 和更慢的 final
- 中英混说会比纯中文更依赖模型适配

如果后续要重点优化英文长句，优先级通常是：

1. 更换更适合英文或中英混说的 ASR 模型
2. 继续拉长极端慢 final 的恢复窗口
3. 结合浏览器控制台日志排查“后端成功了但前端没显示”的场景

## 前端调试建议

前端页面已经内置语音调试面板，建议重点观察：

- `Voice`
- `STT`
- `Partial`
- `Final`
- `worker 入房`
- `收到第一帧音频`
- `最近 speech_start`
- `最近 speech_end`
- `最近 partial`
- `最近 final`

如果后端日志里已经出现：

- `funasr.final.publish`
- `POST /chat_stream 200 OK`

但页面上仍然没有显示结果，那么问题更可能在前端流式渲染链路，而不是 ASR 本身。

## 常见排障

### 1. 语音按钮能点，但没有识别结果

优先检查：

- LiveKit 是否成功启动
- FunASR WebSocket 是否可连
- 浏览器是否真的发布了麦克风
- `worker-status` 是否返回 `worker.connected`

### 2. 日志里只有很多 `asr.audio_chunk_sent`

这通常说明：

- 音频已经送到了后端
- 但 FunASR 还没及时给出可用 `final`

这时要继续看是否出现：

- `funasr.final_timeout.begin`
- `funasr.final.publish`
- `voice.speech_end_without_final`
- `late_timeout_final.accepted`

### 3. 英文长句输出质量差

优先怀疑当前 ASR 模型适配，而不是前端按钮或聊天链路。

### 4. 一键启动脚本报路径错误

请先检查 [Open_AI_teacher.bat](/d:/AIEnglish_grammar_teacher/Open_AI_teacher.bat) 顶部用户配置区是否与你的本机环境一致。

## 开发建议

- 语音链路改动后，优先观察结构化日志是否能走到 `funasr.final.publish`
- 前端显示问题，不要只看网络面板，也要看控制台里新增的 `[voice]` 和 `[chat]` 调试日志
- 如果准备做英文优化，建议把“模型切换”和“超晚 final 恢复”分开验证

## 入口文件

- 后端入口：[main.py](/d:/AIEnglish_grammar_teacher/main.py)
- 前端页面：[frontend/index.html](/d:/AIEnglish_grammar_teacher/frontend/index.html)
- 语音配置：[config.py](/d:/AIEnglish_grammar_teacher/config.py)
- 一键启动：[Open_AI_teacher.bat](/d:/AIEnglish_grammar_teacher/Open_AI_teacher.bat)

## License

当前仓库未单独声明开源许可证；如果准备公开分发，建议补充 `LICENSE` 文件。

