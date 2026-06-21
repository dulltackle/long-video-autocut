# 第六批：skill 调度器与最小协作闭环

第六批覆盖“skill 调度器”的最小可交付范围。目标是在第五批已经具备 reviewed `live` 非 dry-run 完整交付包（`transcript.srt`、`plan.json`、`metadata.json`、`clips/`、`subtitles/`、`拆条报告.md`）的前提下，创建一个**薄调度器 skill**，围绕现有 CLI 底座提供面向用户的协作流程：环境预检、课程上下文收集、调用 `video-auto-editor live`、解释产物、提示修复与二次运行。

skill 只做编排和解释，不承载候选算法、不直接剪视频、不绕过 CLI 写产物。

范围边界：

- 创建一个新的 skill 目录与 `SKILL.md`，描述触发场景、输入、流程和产物解释。
- 环境预检：检查 `video-auto-editor` CLI 可用、`ffmpeg`、`ffprobe` 可执行、`STEPFUN_API_KEY` 是否存在、ASR provider 与评审模型配置是否完整。
- 课程上下文收集：把用户提供的课程信息整理成符合 `CourseContext` 已知字段契约的 JSON 文件，作为 `--context-file` 传入。
- 调用 CLI：以默认 reviewed 非 dry-run 或用户显式 dry-run 调用 `video-auto-editor live ...`，不重写参数语义。
- 产物解释：读取 `plan.json`、`metadata.json` 和 `拆条报告.md`，向用户结构化解释导出清单、未导出原因、人工复核项、边界补救建议和同主题系列。
- 失败处理：根据 `warnings`、退出码和缺失产物，区分 ASR 失败、评审失败/关闭/缺少 API Key、无发布就绪候选等情况，并给出可执行的修复或二次运行建议。
- 提供一个可被 skill 调用、不依赖真实外部服务的预检脚本，把环境检查逻辑沉淀为可测试单元。
- 建立最小 skill 编排测试闭环：用 fake CLI 和 fake 产物验证预检、上下文生成和产物解释逻辑。

不在第六批范围：

- 不改变候选生成、去重、评分、评审或导出算法。
- 不新增 CLI 子命令或修改 `live` 参数语义（如确需新增 `--context-file` 校验属于 CLI 既有能力，不在本批扩展）。
- 不实现发布平台上传、封面生成、标题 A/B 测试或社媒发布。
- 不让 skill 直接调用 ffmpeg、StepAudio 或评审模型，所有重活仍由 CLI 完成。
- 不在 skill 内复制候选时间计算或边界补救逻辑。

## 提交硬性要求

第六批必须按小任务拆分提交。每完成一个任务并通过该任务列出的验证命令后，必须立即执行一次 `git commit`，不得等整批完成后一次性提交。

每次提交前必须确认：

- 只包含当前小任务范围内的改动。
- 不修改测试代码来规避失败。
- 失败测试只能通过修复生产代码或脚本解决。
- `git status --short` 中没有意外文件混入提交范围。

## 任务 1：实现可测试的环境预检脚本

建议新增或扩展文件：

- `video_auto_editor/preflight.py`
- `tests/test_preflight.py`

执行项：

- 实现一个纯函数式的预检入口，输入为可注入的环境探测结果（命令是否存在、环境变量是否存在、配置字段是否完整），输出为结构化预检结果。
- 至少覆盖以下检查项，每项给出 `name`、`status`（`ok` / `warn` / `error`）、`detail` 和可执行的 `hint`：
  - `video-auto-editor` CLI 是否可调用。
  - `ffmpeg` 是否可执行。
  - `ffprobe` 是否可执行。
  - `STEPFUN_API_KEY` 是否设置（默认 ASR 和默认评审都依赖）。
  - ASR provider 配置是否完整（默认 `stepaudio`，可选 `whisper`）。
  - 主题评审配置是否完整（`topic_review_enabled`、`topic_review_provider`、`topic_review_model` 等）。
- 预检结果可汇总为整体 `ready` 布尔值：存在 `error` 即 `ready=False`；只有 `warn` 时 `ready=True` 但保留警告。
- 探测命令是否存在时通过可注入接口完成，测试不依赖真实 `ffmpeg`、`ffprobe` 或网络。
- 为缺失项给出明确、可执行的中文修复提示（例如导出 `STEPFUN_API_KEY`、安装 ffmpeg）。

验收标准：

- 全部依赖齐备时返回 `ready=True` 且无 `error`。
- 缺少 `STEPFUN_API_KEY` 时返回对应 `error` 与可执行修复提示。
- 缺少 `ffmpeg` 或 `ffprobe` 时返回对应 `error`。
- 评审关闭但其它依赖齐备时返回 `warn` 而非 `error`，并说明将走未评审降级路径。
- 预检逻辑在无真实外部命令、无网络环境下稳定通过。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 新增直播拆条环境预检
```

提交前验证：

```bash
pytest tests/test_preflight.py
pytest
```

## 任务 2：实现课程上下文收集与 JSON 生成

建议新增或扩展文件：

- `video_auto_editor/context.py`
- `tests/test_context.py`

执行项：

- 提供一个把用户提供的课程信息整理为合法上下文 JSON 的辅助函数，输出结构严格符合现有 `CourseContext` 的已知字段契约（`STRING_FIELDS`、`STRING_LIST_FIELDS`）。
- 对字符串字段和字符串数组字段做类型规整：去除空白、丢弃空字符串、保持字段顺序稳定。
- 未知字段不直接写入交付 JSON，而是收集为 `unknown_fields` 提示，交由 skill 决定是否提示用户。
- 生成的 JSON 必须能被现有 `load_course_context` 正常加载且不抛错。
- 不静默吞掉类型错误：当用户给出的字段类型无法规整为契约要求时，返回明确错误而不是写出非法 JSON。

验收标准：

- 合法课程信息能生成可被 `load_course_context` 加载的 JSON。
- 字符串数组字段中的空项被清理，类型非法时报错。
- 未知字段不进入交付 JSON，但在结果中可见。
- 生成 JSON 与 `CourseContext.summary` 字段分类保持一致。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 新增课程上下文采集与生成
```

提交前验证：

```bash
pytest tests/test_context.py
pytest
```

## 任务 3：实现产物解释器

建议新增或扩展文件：

- `video_auto_editor/orchestration.py`
- `tests/test_orchestration.py`

执行项：

- 实现读取 `plan.json` 和 `metadata.json`（存在时）并生成结构化解释结果的函数，供 skill 渲染给用户。
- 解释结果至少覆盖：
  - 运行模式（reviewed dry-run / reviewed 非 dry-run / 未评审未导出 / 允许未评审导出）。
  - 导出清单（标题、主题、发布就绪评分、最终边界、视频与字幕相对路径）。
  - 未导出候选清单（未导出原因、淘汰原因、人工复核原因、边界补救建议）。
  - 人工复核清单。
  - 同主题系列分组。
  - `warnings` 汇总与对应可读解释。
- 解释器只读已有产物，不重新计算候选时间、不重新判定发布就绪。
- 缺失 `metadata.json`（dry-run 或失败）时按计划态解释，不伪造已生成文件。
- 解释结果不渲染 `None`，对缺失字段给出稳定占位。

验收标准：

- reviewed 非 dry-run 产物能解释出实际导出清单与交付物。
- reviewed dry-run 产物能解释出计划导出清单且标注未生成视频。
- 未评审产物能解释出未导出原因与 warning，不伪造发布就绪结论。
- 同主题系列与人工复核项不丢失。
- 解释器在缺失 `metadata.json` 时不抛错。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 新增直播拆条产物解释器
```

提交前验证：

```bash
pytest tests/test_orchestration.py
pytest
```

## 任务 4：实现失败诊断与二次运行建议

改动文件：

- `video_auto_editor/orchestration.py`
- `tests/test_orchestration.py`

执行项：

- 基于 CLI 退出码、`warnings` 和缺失产物，产出结构化失败诊断结果，区分至少以下情况：
  - ASR 不可用或识别失败（直接中止，无 `transcript.srt`）。
  - 评审关闭、不可用或缺少 API Key（输出未评审方案）。
  - 评审成功但无发布就绪候选（正常结束、导出为空）。
  - 缺少课程上下文导致评审质量下降的提示。
- 每种情况给出可执行的中文修复建议与对应二次运行命令（例如设置 `STEPFUN_API_KEY` 后重跑、`--allow-unreviewed-export` 兼容导出、`--dry-run` 先看方案）。
- 诊断只解释既有信号，不臆造未在产物或退出码中体现的失败原因。

验收标准：

- ASR 失败被识别为中止类失败并给出修复建议。
- 评审缺少 API Key 被识别为降级类，并提示设置 Key 或显式允许未评审导出。
- 无发布就绪候选被识别为成功但空导出，而非失败。
- 诊断建议附带可直接复制的二次运行命令。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 新增直播拆条失败诊断建议
```

提交前验证：

```bash
pytest tests/test_orchestration.py
pytest
```

## 任务 5：创建薄调度器 skill

建议新增文件：

- `.claude/skills/live-autocut/SKILL.md`
- 必要时 `.claude/skills/live-autocut/references/` 下的流程参考文档

执行项：

- 编写 `SKILL.md`，`description` 覆盖典型触发场景（“把这节直播课拆条”“按主题切短视频”“给这段直播课生成发布就绪短视频”等）。
- 在 skill 流程中按顺序编排：
  1. 调用预检（任务 1），不通过则先提示用户修复。
  2. 收集课程上下文并生成 JSON（任务 2）。
  3. 调用 `video-auto-editor live <video> --output-dir ... --work-dir ... --context-file ...`，默认 reviewed 非 dry-run，支持用户显式 `--dry-run`、`--max-clips`、`--allow-unreviewed-export`。
  4. 用产物解释器（任务 3）解释结果。
  5. 失败时用失败诊断（任务 4）给出修复或二次运行建议。
- 在 `SKILL.md` 中明确边界：skill 不直接剪视频、不绕过 CLI 写产物、不承载候选算法、不调用 ffmpeg/StepAudio/评审模型。
- 文档使用中文，命令示例与第五批运行说明保持一致。

验收标准：

- `SKILL.md` 的 `description` 能在直播拆条请求时触发。
- skill 流程清晰编排预检、上下文、调用、解释、诊断五步。
- skill 文档明确不越界做 CLI 已承担的工作。
- 命令示例与第五批默认行为一致。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 新增直播拆条调度器 skill
```

提交前验证：

```bash
pytest
```

## 任务 6：补充 skill 编排最小测试闭环

建议新增或扩展文件：

- `tests/test_orchestration_e2e.py`

测试目标：

- 不依赖真实 `video-auto-editor` 子进程、真实 ffmpeg/ffprobe、真实 StepAudio 或真实评审模型。
- 用 fake 环境探测覆盖预检的 ready 与 not-ready 两类路径。
- 用样例课程信息驱动上下文生成，并断言生成 JSON 可被 `load_course_context` 加载。
- 用样例 `plan.json` 与 `metadata.json`（reviewed 非 dry-run、reviewed dry-run、未评审三种）驱动产物解释器，断言关键解释字段。
- 用样例 `warnings` 与退出码驱动失败诊断，断言对应建议与二次运行命令。
- 串联预检→上下文→（fake 调用产出 fake 产物）→解释→诊断的最小协作闭环。

验收标准：

- 测试断言预检 not-ready 时流程在调用 CLI 前停下并给出修复提示。
- 测试断言生成的上下文 JSON 合法可加载。
- 测试断言三种产物场景的解释结果正确区分模式与导出清单。
- 测试断言失败诊断为不同失败类型给出不同建议。
- 测试在无网络、无真实外部命令、无真实视频处理环境中稳定通过。

完成本任务后必须 git commit。

建议 commit message：

```text
test: 补充 skill 编排最小闭环
```

提交前验证：

```bash
pytest tests/test_orchestration_e2e.py
pytest
```

## 任务 7：补齐第六批文档与运行说明

改动文件：

- `docs/implementation-plan.md`
- `docs/implementation-batch-6-skill-orchestrator.md`
- 必要时补充 `CONTEXT.md`

执行项：

- 在整体计划中登记第六批执行文档。
- 记录第六批完成后的 skill 协作流程：预检、上下文、调用、解释、诊断。
- 记录 skill 与 CLI 的职责边界。
- 记录环境预检脚本、上下文生成、产物解释器、失败诊断的入口与用途。
- 说明 skill 不实现发布平台上传、封面生成、社媒发布等后续阶段能力。

验收标准：

- 文档能指导下一位开发者从第五批继续执行第六批。
- 文档明确每个任务完成后都必须提交一次。
- 文档没有承诺第六批不会实现的发布平台上传、封面生成或社媒发布能力。

完成本任务后必须 git commit。

建议 commit message：

```text
docs: 补充第六批 skill 调度器执行方案
```

提交前验证：

```bash
pytest
```

## 第六批完成定义

第六批完成时，应满足：

- 存在一个薄调度器 skill，能在直播拆条请求时触发并编排完整协作流程。
- 环境预检能在调用 CLI 前发现缺失依赖并给出可执行修复提示。
- 课程上下文采集能生成合法、可被 `load_course_context` 加载的 JSON。
- 产物解释器能从 `plan.json` 和 `metadata.json` 解释导出清单、未导出原因、人工复核项、边界补救建议和同主题系列。
- 失败诊断能区分 ASR 失败、评审降级、无发布就绪候选等情况并给出二次运行建议。
- skill 不直接剪视频、不绕过 CLI 写产物、不承载候选算法。
- 有最小 skill 编排测试闭环，且不依赖真实外部服务、网络、真实评审模型或真实视频处理。
- 每个小任务都有独立 git commit。
- 全量测试通过。

## 第六批完成后的协作说明

第六批完成后，面向用户的直播拆条协作由 skill 调度，重活仍由 CLI 完成：

1. **预检**：skill 先调用 `video_auto_editor.preflight`，确认 `video-auto-editor`、`ffmpeg`、`ffprobe`、`STEPFUN_API_KEY`、ASR 与评审配置齐备；不齐备时先给出修复提示。
2. **上下文**：skill 用 `video_auto_editor.context` 把课程信息整理为合法上下文 JSON。
3. **调用**：skill 调用 CLI，默认 reviewed 非 dry-run：

   ```bash
   export STEPFUN_API_KEY=sk-...
   video-auto-editor live path/to/live.mp4 \
     --output-dir out/live --work-dir work/live \
     --context-file out/live/course-context.json
   ```

4. **解释**：skill 用 `video_auto_editor.orchestration` 读取 `plan.json`、`metadata.json`、`拆条报告.md`，向用户解释导出清单、未导出原因、人工复核项、边界补救建议和同主题系列。
5. **诊断**：失败、降级或空导出时，skill 给出修复建议与可复制的二次运行命令（如设置 `STEPFUN_API_KEY` 重跑、`--allow-unreviewed-export` 兼容导出、`--dry-run` 先看方案）。

### 后续阶段边界

第六批不实现以下能力，留到后续阶段：

- 发布平台上传与草稿创建。
- 封面生成与标题 A/B 测试。
- 社媒发布与定时发布。
- 跨场次系列化运营与素材库管理。
