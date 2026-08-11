export const STAGES = [
  { key: "initialized", label: "初始化", success: "直播拆条运行已初始化", duration: null, facts: ["CLI 底座 0.8.0"] },
  { key: "preflight", label: "预检", success: "预检通过", duration: "1.8 秒", facts: ["Ubuntu 24.04 · Python 3.12", "FFmpeg、FFprobe 与字体可用", "供应商能力与模型已选择"] },
  { key: "source_analysis", label: "素材分析", success: "素材分析完成", duration: "18 秒", facts: ["素材时长 02:16:48", "文件大小 7.4 GiB", "已提供课程上下文"] },
  { key: "transcription", label: "语音识别", success: "整场转写文本已形成", duration: "23 分 41 秒", facts: ["1,284 个句级块", "语音覆盖补转 2 次", "整场转写缓存未命中"] },
  { key: "candidate_planning", label: "候选规划", success: "已形成 12 个候选片段", duration: "6.4 秒", facts: [] },
  { key: "topic_review", label: "主题评审", success: "已完成 12 个候选片段的主题评审", duration: "4 分 12 秒", facts: ["处理缓存命中 2 次、未命中 4 次", "供应商请求完成 4 次", "重试 1 次"] },
  { key: "delivery_build", label: "交付构建", success: "标准交付物已构建，共 3 条短视频", duration: "8 分 09 秒", facts: ["短视频、转写文本与元数据已形成", "字幕优化缓存命中 1 次", "字幕优化模型请求 2 次"] },
  { key: "delivery_verification", label: "交付验证", success: "标准交付物验证通过", duration: "11 秒", facts: ["已验证 7 个项目", "其中短视频 3 条"] },
  { key: "publishing", label: "发布", success: "标准交付物发布成功", duration: "2.1 秒", facts: [] },
];

export const RUNS = [
  { id: "7d41…82ce", scenario: "live", startedAt: "今天 11:42", status: "状态未闭合", stage: "语音识别", duration: "14 分", lastEvent: "12 秒前", integrity: "完整" },
  { id: "6a10…d771", scenario: "success", startedAt: "昨天 16:08", status: "已成功", stage: "发布", duration: "38 分", lastEvent: "昨天 16:46", integrity: "完整" },
  { id: "934f…1a20", scenario: "failed", startedAt: "8 月 8 日 09:17", status: "已失败", stage: "主题评审", duration: "21 分", lastEvent: "8 月 8 日 09:38", integrity: "完整" },
  { id: "2bd0…c442", scenario: "corrupt", startedAt: "8 月 7 日 20:04", status: "记录损坏", stage: "交付构建", duration: "—", lastEvent: "8 月 7 日 20:31", integrity: "不完整" },
  { id: "e218…19b5", scenario: "interrupted", startedAt: "8 月 6 日 14:22", status: "已中断", stage: "交付构建", duration: "36 分", lastEvent: "8 月 6 日 14:58", integrity: "不完整" },
];

export const SCENARIOS = {
  live: {
    label: "进行中事实",
    status: "状态未闭合",
    integrity: "完整",
    currentStage: "transcription",
    lastEvent: "12 秒前",
    startedAt: "今天 11:42",
    elapsed: "约 14 分钟",
    stageHeadline: "正在形成整场转写文本",
    progressFacts: ["供应商请求完成 8 次", "当前在飞请求 1 次", "重试 2 次 · 退避等待累计 18 秒"],
    stageSignals: [
      { label: "供应商请求", value: "完成 8 次 · 当前在飞 1 次" },
      { label: "重试与退避", value: "重试 2 次 · 等待累计 18 秒" },
      { label: "缓存", value: "整场转写缓存未命中" },
    ],
  },
  failed: {
    label: "已失败",
    status: "已失败",
    integrity: "完整",
    currentStage: "topic_review",
    lastEvent: "今天 11:57",
    startedAt: "今天 11:42",
    elapsed: "15 分 09 秒",
    stageHeadline: "阶段失败",
    progressFacts: ["供应商请求完成 3 次", "重试 2 次", "处理缓存命中 1 次、未命中 3 次"],
    stageSignals: [
      { label: "供应商请求", value: "完成 3 次 · 第 3 次尝试失败" },
      { label: "重试与退避", value: "重试 2 次 · 退避已结束" },
      { label: "缓存", value: "处理缓存命中 1 次、未命中 3 次" },
    ],
    banner: { tone: "danger", title: "主题评审失败", body: "评审模型服务暂时不可用。", action: "稍后再试；完成相应处置后，可开启新的直播拆条运行。" },
  },
  interrupted: {
    label: "已中断",
    status: "已中断",
    integrity: "不完整",
    currentStage: "delivery_build",
    lastEvent: "今天 12:18",
    startedAt: "今天 11:42",
    elapsed: "36 分 22 秒",
    stageHeadline: "阶段已中断",
    progressFacts: ["已记录字幕优化模型请求 2 次", "处理缓存命中 1 次", "已请求中断"],
    stageSignals: [
      { label: "供应商请求", value: "字幕优化模型请求 2 次" },
      { label: "缓存", value: "处理缓存命中 1 次" },
      { label: "中断事实", value: "已记录中断请求" },
    ],
    banner: { tone: "neutral", title: "直播拆条运行已中断", body: "没有证据说明中断由操作员还是系统发起。", action: "中断后的恢复或清理未完整完成。" },
  },
  corrupt: {
    label: "记录损坏",
    status: "记录损坏",
    integrity: "不完整",
    currentStage: "delivery_build",
    lastEvent: "今天 12:31",
    startedAt: "今天 11:42",
    elapsed: "—",
    stageHeadline: "只保留损坏点前的可信事实",
    progressFacts: ["在 sequence 143 停止解析", "最后完整事件：字幕优化请求完成", "损坏点之后的记录未用于推导"],
    stageSignals: [
      { label: "可信事件前缀", value: "在 sequence 143 停止解析" },
      { label: "供应商请求", value: "最后完整事实为字幕优化请求完成" },
      { label: "诊断完整性", value: "损坏点之后未参与推导" },
    ],
    banner: { tone: "warning", title: "结构化事件日志记录损坏", body: "事件 sequence 143 不符合封闭模式。", action: "界面仍可查看此前已经验证的事实；不推测后续结果。" },
  },
  success: {
    label: "已成功",
    status: "已成功",
    integrity: "完整",
    currentStage: "publishing",
    lastEvent: "今天 12:20",
    startedAt: "今天 11:42",
    elapsed: "38 分 04 秒",
    stageHeadline: "标准交付物发布成功",
    progressFacts: [],
    stageSignals: [],
    delivery: { count: 3, evidence: "已发布" },
  },
};

const STAGE_INDEX = Object.fromEntries(STAGES.map((stage, index) => [stage.key, index]));

export function getStageView(stage, scenario) {
  const index = STAGE_INDEX[stage.key];
  const currentIndex = STAGE_INDEX[scenario.currentStage];
  if (index < currentIndex || scenario.status === "已成功") {
    return { lifecycle: "succeeded", headline: stage.success, facts: stage.facts, duration: stage.duration };
  }
  if (index > currentIndex) {
    return { lifecycle: "not_started", headline: "尚未观察到开始事件", facts: [], duration: null };
  }
  if (scenario.status === "已失败") return { lifecycle: "failed", headline: scenario.stageHeadline, facts: scenario.progressFacts, duration: scenario.elapsed };
  if (scenario.status === "已中断") return { lifecycle: "interrupted", headline: scenario.stageHeadline, facts: scenario.progressFacts, duration: scenario.elapsed };
  if (scenario.status === "记录损坏") return { lifecycle: "corrupt", headline: scenario.stageHeadline, facts: scenario.progressFacts, duration: scenario.elapsed };
  return { lifecycle: "in_progress", headline: scenario.stageHeadline, facts: scenario.progressFacts, duration: scenario.elapsed };
}

export const LIFECYCLE_LABEL = {
  succeeded: "已完成",
  in_progress: "进行中事实",
  failed: "阶段失败",
  interrupted: "阶段已中断",
  corrupt: "可信前缀",
  not_started: "未开始",
};
