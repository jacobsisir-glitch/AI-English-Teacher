# AI English Teacher

一个以 `FastAPI + 前端直播间 UI + LiveKit` 为核心的 AI 英语老师项目，当前同时保留 legacy 语音链路和 Qwen Omni Realtime 原生语音链路。

当前仓库已经不只是传统聊天页，而是同时支持：

- 文本闲聊 / 答疑
- 微课白板讲解
- 实时语音输入转写
- Legacy 语音结果回流到原有聊天链路
- 老师语音输出：legacy 使用豆包 V3 双向流式 TTS，Qwen 模式使用 Qwen 原生语音

## 项目概览

这个项目面向“AI 英语老师”场景，核心思路是把几条能力链路合在一起：

- `POST /chat_stream`
  处理普通聊天、问答、翻译、语法说明等文本请求
- `POST /class_chat_stream`
  处理带白板节奏的微课讲解
- `POST /api/livekit/token`
  为浏览器语音模式签发 LiveKit token，并确保房间级语音 worker 已启动；会根据 `VOICE_CONVERSATION_PROVIDER` 选择 legacy 或 Qwen Realtime
- `GET /api/livekit/worker-status`
  返回房间语音 worker 的状态和最近一次关键事件
- `GET /api/tts/voices`
  返回当前 TTS provider 和可用音色信息
- `POST /api/tts/speak`
  兼容接口：通过当前 TTS provider 生成完整音频
- `WebSocket /api/tts/stream`
  legacy 老师语音输出主路径：豆包 V3 bidirection WebSocket 音频转发

legacy 语音模式下，浏览器麦克风音频会先进入 LiveKit 房间，再由后端 worker 订阅、做 VAD 切分、送入 FunASR，最终把识别出的 `partial` / `final` 文本重新送回前端，并把 `final` 文本提交到原有聊天链路。

Qwen Realtime 模式下，学生麦克风音频仍经 LiveKit 进入后端，但后端会直接把 `16k PCM` 推给 Qwen Omni Realtime；Qwen 同时生成老师文本和 `24k PCM` 原生语音，后端再把老师语音发布成 LiveKit 音轨给浏览器播放。

## 当前能力

- 文本聊天：支持流式返回 AI 回复
- 微课模式：支持教材驱动、白板事件驱动和阶段化讲解
- Legacy 语音模式：支持浏览器直接说话，实时显示 FunASR partial / final transcript
- Qwen Realtime 模式：支持学生语音直达 Qwen，实时显示学生识别与 Lumina 回复字幕
- 语音回流：legacy final 会自动作为学生输入进入聊天区；Qwen 模式禁止重复调用旧聊天接口
- 语音输出：legacy 使用豆包 V3 双向流式 TTS；Qwen 模式使用 Qwen 原生音色并通过 LiveKit 音轨播放
- 中英同声线：中文讲解与英文例句使用同一豆包音色
- 打断能力：学生新输入或开始语音输入时会中断旧播报
- 调试能力：前端有语音调试面板，后端有结构化语音日志
- 一键启动：提供 Windows 启动脚本 [Open_AI_teacher.bat](/d:/AIEnglish_grammar_teacher/Open_AI_teacher.bat)

## 语音架构

### Legacy：FunASR + LLM + 豆包 TTS

legacy 模式保留原有“FunASR -> LLM -> 豆包 V3 双向流式 TTS”链路：

```text
frontend/index.html
→ /api/tts/stream 或 /api/tts/speak
→ main.py
→ tts_client.py
→ speech_providers/doubao_tts.py
→ 豆包 V3 bidirection WebSocket
```

当前验收通过的关键配置：

```env
TTS_PROVIDER=doubao
DOUBAO_TTS_API_VERSION=v3
DOUBAO_AUTH_MODE=api_key
DOUBAO_TTS_V3_ENDPOINT=wss://openspeech.bytedance.com/api/v3/tts/bidirection
DOUBAO_TTS_RESOURCE_ID=seed-icl-2.0
DOUBAO_TTS_VOICE_TYPE=S_D9gzp4Q12
DOUBAO_TTS_ENABLE_STREAM=true
TTS_FALLBACK_ON_ERROR=false
```

旧 Melo/OpenVoice 已经变成 `local_melo` legacy fallback，不再是主路径。旧 Kokoro 也属于 legacy 实验链路。使用豆包 TTS 时，不需要再启动 `127.0.0.1:8012` 的 Melo/OpenVoice 服务。

更详细的配置、错误排查和验证步骤见 [docs/doubao_tts_v3_setup.md](/d:/AIEnglish_grammar_teacher/docs/doubao_tts_v3_setup.md)。

### Qwen Omni Realtime：原生理解与原生语音

Qwen 模式由 `VOICE_CONVERSATION_PROVIDER=qwen_omni_realtime` 启用：

```text
student microphone
→ LiveKit room
→ voice/qwen_room_processor.py
→ voice/providers/qwen_omni_realtime.py
→ Qwen Omni Realtime WebSocket
→ response.audio_transcript.delta / response.audio.delta
→ LiveKit teacher audio track
→ frontend/index.html subtitles + RemoteAudioTrack playback
```

Qwen 模式要求：

- API Key 只从后端环境变量读取，前端不接触密钥。
- 使用 `QWEN_REALTIME_WORKSPACE_ID` 构造北京地域 WebSocket 地址。
- 输入音频为 `16kHz PCM`，输出音频为 `24kHz PCM`。
- `QWEN_REALTIME_VOICE` 可填内置音色，如 `Tina`，也可填声音复刻得到的自定义 voice ID。
- `QWEN_AUDIO_PLAYBACK_BUFFER_MS` 只做每轮 response 的初始播放缓冲，不对每个 chunk sleep。
- Qwen 官方事件没有逐词时间戳，因此字幕和音频只能做句子/短语级近同步，不能声明 word-level sync。

## 目录结构

```text
.
├─ frontend/                前端页面与语音 UI
├─ voice/                   LiveKit + VAD + FunASR 语音链路
├─ data/                    数据库与教材数据
├─ database/                数据层代码
├─ speech_providers/        TTS provider：豆包 V3 与相关协议实现
├─ scripts/                 验证和辅助脚本
├─ docs/                    接入说明与运维文档
├─ tools/                   工具文件
├─ main.py                  FastAPI 入口
├─ llm_wrapper.py           LLM prompt 与流式包装
├─ config.py                环境变量与运行参数
├─ tts_client.py            TTS provider router 与 legacy fallback
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
- 语音输出：豆包 V3 双向流式 TTS
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
VOICE_CONVERSATION_PROVIDER=legacy

FUNASR_WS_URL=wss://127.0.0.1:10095
FUNASR_MODE=2pass
FUNASR_MODEL_NAME=

FUNASR_FINAL_WAIT_OFFLINE_MS=6000
FUNASR_FINAL_WAIT_FALLBACK_MS=0
FUNASR_FINAL_DRAIN_MS=1000
FUNASR_FINAL_RESCUE_WAIT_MS=6000
FUNASR_LATE_FINAL_GRACE_MS=12000
VOICE_FINAL_ACK_TIMEOUT_MS=1000
VOICE_FINAL_ACK_RETRY=2
VOICE_STOP_WAIT_FINAL_MS=8000

SILERO_VAD_THRESHOLD=0.38
SILERO_MIN_SILENCE_MS=1200
SILERO_PRE_SPEECH_MS=480
SILERO_MIN_SPEECH_MS=320
SILERO_SPEECH_END_HOLD_MS=720

TTS_PROVIDER=doubao
DOUBAO_TTS_API_VERSION=v3
DOUBAO_AUTH_MODE=api_key
DOUBAO_API_KEY=your_real_api_key_only_in_dotenv
DOUBAO_TTS_V3_ENDPOINT=wss://openspeech.bytedance.com/api/v3/tts/bidirection
DOUBAO_TTS_RESOURCE_ID=seed-icl-2.0
DOUBAO_TTS_VOICE_TYPE=S_D9gzp4Q12
DOUBAO_TTS_ENABLE_STREAM=true
TTS_FALLBACK_ON_ERROR=false

# Qwen native realtime mode. Enable with:
# VOICE_CONVERSATION_PROVIDER=qwen_omni_realtime
QWEN_REALTIME_API_KEY=your_real_qwen_key_only_in_dotenv
QWEN_REALTIME_WORKSPACE_ID=your_workspace_id
QWEN_REALTIME_MODEL=qwen3.5-omni-flash-realtime
QWEN_REALTIME_REGION=beijing
QWEN_REALTIME_VOICE=Tina
QWEN_REALTIME_VAD_MODE=semantic_vad
QWEN_AUDIO_PLAYBACK_BUFFER_MS=300
```

完整示例见 [.env.example](/d:/AIEnglish_grammar_teacher/.env.example)。

注意：真实 `DOUBAO_API_KEY`、`QWEN_REALTIME_API_KEY`、Access Token、LiveKit Secret 只能写入 `.env`，不能提交到 Git。

### Qwen Realtime 与 Legacy 语音链路

`VOICE_CONVERSATION_PROVIDER=legacy` 仍是默认兼容模式：

```text
student audio -> LiveKit/FunASR/Silero -> LLM -> Doubao TTS
```

`VOICE_CONVERSATION_PROVIDER=qwen_omni_realtime` 使用 Qwen 原生实时语音：

```text
student audio -> Qwen understand/reply -> Qwen native voice -> LiveKit audio track
```

Qwen 模式使用 `QWEN_REALTIME_VOICE` 选择内置音色或声音复刻得到的自定义 voice ID；Legacy 模式继续使用现有 Doubao TTS 音色配置。`QWEN_AUDIO_PLAYBACK_BUFFER_MS` 控制 Qwen 音频发布到 LiveKit 前的每轮初始缓冲，`0` 表示最低延迟，`200/300/400`ms 可按真实日志中的 `qwen.sync.*` 延迟再取舍。Qwen 官方没有提供逐词时间戳，因此这里只能做句子/短语级近同步，不能声明 word-level sync。

未来可以探索混合链路：

```text
student audio -> Qwen native understanding -> Qwen text -> Doubao TTS
```

当前未实现该混合模式。

## 启动方式

### 方式一：手动启动

当前推荐启动方式：

```bash
conda activate ai_teacher
cd D:\AIEnglish_grammar_teacher
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

如果只测试老师语音输出的豆包 TTS，不需要启动 8012 的 Melo/OpenVoice 服务。

## 豆包 TTS 验证步骤

### 1. 验证豆包 V3 provider

```bash
python scripts/verify_doubao_tts_v3.py
```

成功时会生成 `doubao_v3_test.mp3`，并打印多个 `audio chunk`。

### 2. 验证 voices 接口

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/tts/voices" -UseBasicParsing | Select-Object -ExpandProperty Content
```

### 3. 验证 speak 兼容接口

```powershell
$body = '{"text":"你好，欢迎来到 AI Teacher。今天我们测试豆包语音。","voice":"doubao_default","lang":"zh","speed":1.0}'
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/tts/speak" -Method POST -ContentType "application/json" -Body $body -OutFile "doubao_v3_api_test.mp3"
```

### 4. 前端页面测试

打开：

```text
http://127.0.0.1:8000/frontend/index.html
```

输入：

```text
请用中文介绍一下什么是定语从句，并给我一个英文例句。
```

预期：老师语音能自动播放，中文清晰，英文例句与中文讲解保持同一声线，新输入可打断旧语音。

## Qwen Realtime 验证步骤

### 1. 低成本真实握手

```powershell
python scripts\verify_qwen_realtime_connection.py
```

这个脚本只做 WebSocket 握手、`session.created`、`session.update`、`session.updated` 验证，不上传学生录音，也不会输出 API Key。没有真实 API Key 或当前环境无法访问阿里云时，不代表线上链路已失败，只能说明本机未完成真实连接验证。

### 2. Mock 事件链路

```powershell
python scripts\test_qwen_realtime_mock.py
```

覆盖官方事件字段映射、Qwen 输出文本/音频 delta、response cancel 后 late audio 丢弃、LiveKit AudioFrame 分帧、prompt 共享人设和 Qwen voice 配置。

### 3. 声音复刻 dry-run 校验

```powershell
python scripts\qwen_voice_enroll.py `
  --audio D:\path\lumina_voice.wav `
  --preferred-name lumina `
  --target-model qwen3.5-omni-flash-realtime `
  --confirm-rights
```

该脚本默认只做本地文件和参数校验，不会真实调用声音复刻接口，不复制音频进仓库，不输出音频 Base64 或密钥。成功复刻后的 voice ID 需要手动写入 `.env`：

```env
QWEN_REALTIME_VOICE=<voice ID>
```

### 4. 浏览器端验收重点

打开前端后启用语音模式，重点看控制台：

- `qwen.livekit.start_audio.success`
- `qwen.livekit.audio_track_subscribed`
- `qwen.livekit.audio_attached`
- `qwen.livekit.audio_playing`
- `qwen.sync.first_audio_delta`
- `qwen.sync.first_transcript_delta`
- `qwen.sync.audio_buffer_released`

如果 `response.audio.delta` 已经到达后端，但没有声音，优先看 LiveKit track SID、`qwen.livekit.audio_frame_captured`、浏览器 `audio_playing` 和 autoplay 错误。

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
- [voice/providers/qwen_omni_realtime.py](/d:/AIEnglish_grammar_teacher/voice/providers/qwen_omni_realtime.py)
  负责 Qwen Realtime WebSocket 协议、事件映射、字段校验、脱敏日志和共享 Lumina instructions
- [voice/qwen_room_processor.py](/d:/AIEnglish_grammar_teacher/voice/qwen_room_processor.py)
  负责 Qwen 模式下学生音频送入、老师 LiveKit 音轨发布、24k PCM 分帧和初始播放缓冲
- [voice/session_state.py](/d:/AIEnglish_grammar_teacher/voice/session_state.py)
  负责结构化日志和会话状态

legacy 语音模式的大致流程：

1. 浏览器请求 `POST /api/livekit/token`
2. 后端签发 token，并确保房间 worker 已启动
3. 浏览器加入 LiveKit 房间并发布麦克风
4. 后端 worker 订阅音轨并转成 `16k / mono`
5. Silero VAD 识别 `speech_start` / `speech_end`
6. 音频片段流式发送给 FunASR
7. FunASR 返回 `partial` 和 `final`
8. `final` 文本发布到前端并自动提交到聊天链路

Qwen 语音模式的大致流程：

1. 浏览器请求 `POST /api/livekit/token`
2. 后端签发 token，并按 `VOICE_CONVERSATION_PROVIDER=qwen_omni_realtime` 启动 Qwen worker
3. 浏览器加入 LiveKit 房间并发布学生麦克风
4. 后端只订阅目标学生的 microphone 音轨，过滤老师自己发布的音轨
5. 后端把学生音频转为 `16k PCM` 并追加到 Qwen input buffer
6. Qwen 返回学生输入转写、老师文本 delta 和 `24k PCM` 音频 delta
7. 后端把老师音频分帧发布到 LiveKit LocalAudioTrack
8. 浏览器订阅老师 RemoteAudioTrack，attach 到隐藏 audio element 并播放
9. 老师字幕实时显示在主字幕区域，完整回复只在 done 时写入左侧历史一次

## 最近这批语音更新

这次上传的更新重点在 Qwen Realtime 原生语音接入、前端字幕/音频链路，以及 legacy 长句语音识别稳定性：

- 新增 `qwen_omni_realtime` 会话 provider，默认仍保留 `legacy`
- 新增 Qwen Realtime mock 测试和最小真实连接脚本
- Qwen 模式禁止重复调用旧聊天接口和豆包 TTS
- Qwen 老师音频通过 LiveKit 音轨播放，支持 response 级初始缓冲
- 主字幕改为最新 3 行滚动显示，不再 ellipsis 截断长句
- 共享 Lumina prompt 增加 spoken-only 规则，禁止把动作、表情、舞台提示读出来
- Qwen 音色通过 `QWEN_REALTIME_VOICE` 配置，支持内置音色和自定义 voice ID
- 新增 Qwen 声音复刻 dry-run 校验脚本
- 增加了更长的 FunASR `offline final` 等待与补救窗口
- 增加了 late final recovery，尽量救回超时后才到达的 `2pass-offline`
- 对过短 partial fallback 做了更严格的抑制
- 增加了 VAD 结束保持时间，减少一句话被切成多段
- 前端增加了更多语音与聊天流式调试日志
- 语音转文本结果现在会更稳定地回流到聊天消息区

注意：前端的“结束本句”按钮目前是实验功能。当前实现仍通过
`setMicrophoneEnabled(false)` / `setMicrophoneEnabled(true)` 临时关闭再重开浏览器麦克风，
这可能触发 LiveKit track 重建。后续更稳的方案应单独实现 data topic 或 HTTP 控制信号，
由后端直接 finish 当前 utterance，而不是关闭麦克风 track。

本次关键配置位于：

- [config.py](/d:/AIEnglish_grammar_teacher/config.py)
- [.env.example](/d:/AIEnglish_grammar_teacher/.env.example)
- [Open_AI_teacher.bat](/d:/AIEnglish_grammar_teacher/Open_AI_teacher.bat)

## 已知限制

当前已知状态：

- 豆包 TTS：已接入 V3 bidirection，并通过第一阶段验收。
- 豆包 ASR：尚未接入。
- Legacy 语音输入：使用 LiveKit + FunASR + Silero VAD。
- Qwen 语音输入：使用 LiveKit + Qwen Omni Realtime；真实 API Key 和 Workspace 只在后端 `.env` 中配置。
- Qwen 字幕同步：官方没有逐词时间戳，目前只能做句子/短语级近同步。
- Qwen 声音复刻：仓库只提供 dry-run 校验脚本，不会自动创建或保存 voice ID。
- Melo/OpenVoice：暂时保留为 legacy fallback / 归档对象。
- Kokoro：外部工具链保留为 legacy 实验对象。

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
