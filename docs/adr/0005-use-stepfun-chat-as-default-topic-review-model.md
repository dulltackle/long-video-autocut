# 默认使用 StepFun Chat 进行主题评审

主题评审是文本推理任务，需要结合转写文本、课程上下文和候选片段边界判断发布就绪质量。我们决定默认使用 StepFun 的 OpenAI-compatible Chat Completions 进行主题评审，以便和 stepaudio-2.5-asr 共用平台配置；CLI 仍保留 base URL、模型名和 API Key 环境变量等兼容配置，避免把评审能力锁死在单一供应商。
