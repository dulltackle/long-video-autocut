# 默认使用 stepaudio-2.5-asr 进行语音识别

Status: Superseded
Date: 2026-06-19
Superseded by: [采用供应商无感知的语音识别模块与覆盖账本](0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md)

直播课拆条依赖长视频转写质量和时间戳稳定性，语音识别服务会影响主题边界、字幕和后续主题评审。我们决定默认使用 stepaudio-2.5-asr 作为语音识别服务，并把 Whisper 保留为可选替代；skill 负责检查 API Key 和服务配置，底层 CLI 负责调用语音识别并缓存转写文本。
