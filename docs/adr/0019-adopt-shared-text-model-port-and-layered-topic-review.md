# 采用共享文本模型端口与分层主题评审

Status: Accepted
Date: 2026-07-25
Amends: [默认使用 StepFun Chat 进行主题评审](0005-use-stepfun-chat-as-default-topic-review-model.md)
Supersedes: [使用混合策略判定直播课主题片段](0001-use-hybrid-topic-detection-for-live-clips.md)

## 背景

既有混合主题判定把候选生成、模型评审和最终导出选择揉在同一决定中，而 StepFun 决定又把业务评审与供应商传输配置相连。这会模糊候选、评审结论和最终短视频集合分别由谁拥有，也难以让主题评审与字幕优化复用一致的文本生成失败、取消和观测契约。

## 决定

采用分层职责：`ClipPlanning.prepare()` 拥有候选生成、初始边界和评审上下文，`TopicReview` 拥有相邻候选批次、提示、业务校验、语义重试和处理缓存，`ClipPlanning.finalize()` 再根据评审事实完成边界补救、发布就绪判断和导出选择。主题评审不修改候选，也不拥有最终选择。

`TopicReview` 与 `SubtitleOptimization` 在模块内部共享同步、无会话状态的文本模型端口。端口只负责纯文本生成、传输重试、取消、中性执行事实和类型化供应商失败；业务响应解析与语义重试仍由各自模块负责。StepFun 保持为首个认证 Adapter，但不成为业务接口的一部分。当前契约见[生产就绪规格 §9.3](../production-readiness-spec.md#93-clipplanning)、[§9.4](../production-readiness-spec.md#94-主题评审与字幕优化)和[§9.5](../production-readiness-spec.md#95-文本模型端口)。

## 权衡

分层端口需要更多明确的请求、结果和失败类型，也禁止编排层直接利用供应商特性；作为交换，业务事实所有权、传输重试与语义重试的边界变得稳定，主题评审和字幕优化可以共享基础能力而不互相耦合业务规则。

## 后果

- 候选生成、主题评审和最终导出选择可分别通过公开接口测试。
- StepFun 与确定性 Adapter 共用文本模型端口契约，更换认证供应商不改变业务模块。
- 任一评审工作项失败时不返回部分评审结果；零候选仍是无需模型调用的成功空操作。
