# CLI 负责视频处理，skill 负责上层调度

Status: Amended
Date: 2026-06-19
Amended by: [采用单一组合根与业务能力深模块](0017-adopt-single-composition-root-and-deep-business-capability-modules.md)

直播课拆条需要可测试、可复现的媒体处理和评审流程，同时也需要面向用户的环境预检、上下文收集和结果解释。我们决定把媒体处理、语音识别、候选生成、主题评审调用、边界补救、导出选择、视频裁剪、字幕、元数据和报告生成放在 CLI 底座中，把 skill 限定为环境预检、参数组装、CLI 调用、结果解释和二次运行调度，避免把核心算法散落到不可测试的上层脚本里。
