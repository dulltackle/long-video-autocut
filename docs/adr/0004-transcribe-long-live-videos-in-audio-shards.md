# 长直播默认分片识别后合并时间戳

Status: Superseded
Date: 2026-06-19
Superseded by: [采用供应商无感知的语音识别模块与覆盖账本](0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md)

几小时直播课音频如果一次性提交给语音识别服务，请求体、失败重试和缓存粒度都会变得不可控。我们决定默认先把原始音频切成连续识别分片，逐片调用 stepaudio-2.5-asr，再把每片返回的时间戳按分片偏移合并为整场直播课的转写文本。
