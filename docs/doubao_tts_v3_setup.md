# Doubao TTS V3 Setup

AI Teacher 的老师语音输出主路径已经从本地 Melo/OpenVoice 迁移到豆包 V3 双向流式 TTS。旧 Melo/OpenVoice 和 Kokoro 只作为 legacy fallback / 归档对象保留。

## 为什么切换到豆包 V3

- 本地 Melo/OpenVoice 可以跑通，但中文长句、英文长句和中英混讲的自然度、延迟和音色一致性都不稳定。
- 豆包 V3 双向流式接口适合“大模型流式文本输出 + 实时语音播报”的场景。
- 当前验证音色 `S_D9gzp4Q12` 已经能通过 V3 bidirection WebSocket 输出清晰中文，英文例句和中文讲解也保持同一声线。

## 为什么不用 V1

旧 V1 HTTP 接口 `https://openspeech.bytedance.com/api/v1/tts` 对新版声音资源连续返回 `code=3031` / `Init Engine Instance failed`。这说明新版音色不适合作为 V1 主路径。

当前主路径使用：

```text
wss://openspeech.bytedance.com/api/v3/tts/bidirection
```

## 为什么 Resource ID 是 seed-icl-2.0

测试结果：

- `seed-tts-2.0` 返回 `resource ID is mismatched with speaker related resource`
- `volc.seedtts.default` 返回同样的 resource mismatch
- `seed-icl-2.0` 验证成功，并生成可播放的 `doubao_v3_test.mp3`

因此当前项目使用：

```env
DOUBAO_TTS_RESOURCE_ID=seed-icl-2.0
DOUBAO_TTS_VOICE_TYPE=S_D9gzp4Q12
```

## .env 配置

真实密钥只写入 `.env`，不要提交到 Git。

```env
TTS_ENABLED=true
TTS_PROVIDER=doubao
TTS_DEFAULT_LANG=zh
TTS_DEFAULT_SPEED=1.0
TTS_TIMEOUT_SECONDS=120

DOUBAO_TTS_API_VERSION=v3
DOUBAO_AUTH_MODE=api_key
DOUBAO_API_KEY=your_real_api_key
DOUBAO_TTS_V3_ENDPOINT=wss://openspeech.bytedance.com/api/v3/tts/bidirection
DOUBAO_TTS_RESOURCE_ID=seed-icl-2.0
DOUBAO_TTS_RESOURCE_ID_HEADER=X-Api-Resource-Id
DOUBAO_TTS_VOICE_TYPE=S_D9gzp4Q12
DOUBAO_TTS_ENABLE_STREAM=true
TTS_FALLBACK_ON_ERROR=false
```

Legacy 本地 Melo/OpenVoice 回退：

```env
TTS_PROVIDER=local_melo
TTS_BASE_URL=http://127.0.0.1:8012
```

豆包 TTS 主路径不需要启动 8012 的 Melo/OpenVoice 服务。

## 验证脚本

```powershell
conda activate ai_teacher
cd D:\AIEnglish_grammar_teacher
python scripts/verify_doubao_tts_v3.py
```

成功标志：

- 输出多个 `audio chunk`
- 生成 `doubao_v3_test.mp3`
- 文件大小大于 0
- 音频可以正常播放

## 测试 /api/tts/speak

先启动主后端：

```powershell
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

再测试：

```powershell
$body = '{"text":"你好，欢迎来到 AI Teacher。今天我们测试豆包语音。","voice":"doubao_default","lang":"zh","speed":1.0}'
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/tts/speak" -Method POST -ContentType "application/json" -Body $body -OutFile "doubao_v3_api_test.mp3"
```

成功标志：

- 生成 `doubao_v3_api_test.mp3`
- 文件大小大于 0
- 音频可以正常播放

## 测试前端

打开：

```text
http://127.0.0.1:8000/frontend/index.html
```

输入：

```text
请用中文介绍一下什么是定语从句，并给我一个英文例句。
```

预期：

- 老师语音自动播放
- 中文清晰
- 英文例句和中文讲解保持同一声线
- 新输入会打断旧语音

## 常见错误

`code=3031` / `Init Engine Instance failed`

表示新版音色不适合继续走 V1 HTTP 接口，应使用 V3。

`resource ID is mismatched with speaker related resource`

表示 `X-Api-Resource-Id` 与当前 `speaker` / `voice_type` 不匹配。当前已验证 `S_D9gzp4Q12` 使用 `seed-icl-2.0`。

`decode ws request failed`

表示 WebSocket 二进制协议封包错误，例如直接发送普通 JSON，或 frame 中缺少 session id / payload size。

`no audio bytes returned`

表示连接和请求可能成功，但服务端没有返回音频 chunk。检查 `speaker`、Resource ID、文本内容和日志中的协议事件。

## 当前已知状态

- 豆包 TTS：已接入并通过第一阶段验收。
- 豆包 ASR：尚未接入。
- 语音输入：仍使用 LiveKit + FunASR + Silero VAD。
- Melo/OpenVoice：暂时保留为 legacy fallback / 归档对象。
- Kokoro：外部工具链保留为 legacy 实验对象。

## 后续清理建议

等豆包 TTS 连续稳定 1 周后，再考虑归档或删除：

- `temp_audio_melo/`
- `doubao_v3_test.mp3`
- `doubao_v3_api_test.mp3`
- `*_probe.wav`
- `ai_teacher_tts_proxy.wav`
- 旧 Kokoro 测试音频
- 旧 Melo/OpenVoice 模型目录

本轮不删除旧文件。
