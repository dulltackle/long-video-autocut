# 第一批：CLI 打包与配置层

第一批只覆盖“CLI 打包与配置层”，目标是先稳定命令入口、课程上下文输入、dry-run 和 `plan.json` 契约。

## 任务 1：完善打包配置

改动文件：

- `pyproject.toml`

执行项：

- 增加 `[build-system]`。
- 增加 `[project]`：项目名、版本、描述、Python 版本要求、依赖。
- 增加 `[project.scripts]`：`video-auto-editor = "video_auto_editor.cli:main"`。
- 保留现有 pytest 配置。

验收标准：

- `python -m video_auto_editor --help` 仍可运行。
- 安装后可以通过 `video-auto-editor --help` 访问同一 CLI。
- 现有测试不因打包配置变化失败。

## 建议提交点 1

提交内容：仅包含打包配置和 CLI 入口声明。

建议 commit message：

```text
chore: 增加 video-auto-editor CLI 打包入口
```

提交前验证：

```bash
python -m video_auto_editor --help
pytest
```

## 任务 2：引入课程上下文解析

建议新增文件：

- `video_auto_editor/context.py`
- `tests/test_context.py`

执行项：

- 定义课程上下文的已知字段：`course_title`、`instructor`、`brand`、`audience`、`priority_topics`、`excluded_topics`、`forbidden_terms`、`notes`。
- 提供 `load_course_context(path)`。
- 文件必须是合法 JSON object。
- 已知字段类型错误时返回明确错误。
- 未知字段允许保留，避免后续扩展受阻。
- 提供摘要方法，不在终端完整打印敏感内容。

验收标准：

- 合法 JSON 可加载。
- 非对象 JSON 会失败。
- 已知字段类型错误会失败。
- 未知字段不会失败。

## 建议提交点 2

提交内容：仅包含课程上下文解析、摘要和对应测试，不接入 live 流程。

建议 commit message：

```text
feat: 增加课程上下文 JSON 解析
```

提交前验证：

```bash
pytest tests/test_context.py
pytest
```

## 任务 3：扩展 live CLI 参数

改动文件：

- `video_auto_editor/cli.py`
- `tests/test_media_report_cli.py`

执行项：

- 为 `live` 增加 `--dry-run`。
- 为 `live` 增加 `--context-file`。
- 为 `live` 增加 `--allow-unreviewed-export`。
- 保留 `--max-clips`，但参数层语义调整为可选上限。
- 在进入处理前加载并校验课程上下文。

验收标准：

- 不传新参数时，现有 live 行为保持兼容。
- `--context-file` 指向非法 JSON 时，CLI 给出清晰错误并停止。
- `--dry-run` 参数被解析，但此提交不要求完成 dry-run 行为。

## 建议提交点 3

提交内容：CLI 参数解析和课程上下文接入入口，不包含 `plan.json` 和 dry-run 行为实现。

建议 commit message：

```text
feat: 扩展 live 子命令基础参数
```

提交前验证：

```bash
python -m video_auto_editor live --help
pytest tests/test_media_report_cli.py
pytest
```

## 任务 4：新增 plan.json 输出

建议新增或扩展文件：

- `video_auto_editor/plan.py`
- `tests/test_plan.py`
- 必要时扩展 `video_auto_editor/report.py`

第一版结构：

```json
{
  "source_video": "example.mp4",
  "status": "unreviewed",
  "context": {
    "loaded": true,
    "summary": {}
  },
  "candidates": [],
  "selected": [],
  "warnings": []
}
```

执行项：

- 在 live 流程完成候选生成、去重和选择后写出 `plan.json`。
- `status` 第一版固定为 `unreviewed`。
- `context.loaded` 反映是否传入课程上下文。
- `candidates` 包含候选片段基础信息。
- `selected` 包含第一版选择结果。
- `warnings` 包含未接入主题评审、未达到发布就绪保证等提示。

验收标准：

- live 正常运行时输出 `plan.json`。
- `plan.json` 是合法 JSON，且不包含绝对敏感配置。
- `plan.json` 可被 skill 调度器作为机器入口读取。

## 建议提交点 4

提交内容：`plan.json` 生成模块、live 流程写入计划文件、对应测试。

建议 commit message：

```text
feat: 为 live 流程输出拆条方案
```

提交前验证：

```bash
pytest tests/test_plan.py
pytest tests/test_media_report_cli.py
pytest
```

## 任务 5：实现 live dry-run

改动文件：

- `video_auto_editor/cli.py`
- `video_auto_editor/report.py`
- `tests/test_media_report_cli.py`

执行项：

- live dry-run 仍执行：视频信息、静音检测、整视频转写、`transcript.srt`、候选生成、基础去重、基础选择、报告、`plan.json`。
- live dry-run 跳过：`export_live_clips`、短视频裁剪、单条字幕导出、`metadata.json`。
- 报告中明确标记该结果是未评审方案，不代表发布就绪短视频。

验收标准：

- `--dry-run` 不创建 `clips/*.mp4`。
- `--dry-run` 不创建单条短视频字幕。
- `--dry-run` 会创建 `plan.json` 和报告。
- 非 dry-run 仍保留现有导出行为。

## 建议提交点 5

提交内容：dry-run 行为和报告提示，不修改 ASR、LLM 或候选算法。

建议 commit message：

```text
feat: 支持 live dry-run 拆条方案
```

提交前验证：

```bash
pytest tests/test_media_report_cli.py
pytest
```

## 任务 6：处理 `--max-clips` 过渡语义

改动文件：

- `video_auto_editor/config.py`
- `video_auto_editor/cli.py`
- `video_auto_editor/selection.py`
- `tests/test_live_selection.py`
- `tests/test_media_report_cli.py`

执行项：

- 参数层区分“用户未传 `--max-clips`”和“用户显式传入上限”。
- 未接入主题评审前，如果用户未传上限，live MVP 仍使用临时保护上限 `5`。
- 如果用户显式传入 `--max-clips N`，使用用户指定值。
- 在 `plan.json` 或报告 warnings 中记录临时保护上限。

验收标准：

- 默认 live MVP 不突然导出大量未评审片段。
- 显式 `--max-clips` 仍生效。
- 后续接入主题评审时可以移除临时保护上限。

## 建议提交点 6

提交内容：`--max-clips` 过渡语义、临时保护上限提示和对应测试。

建议 commit message：

```text
fix: 保持未评审 live 拆条默认上限
```

提交前验证：

```bash
pytest tests/test_live_selection.py
pytest tests/test_media_report_cli.py
pytest
```

## 任务 7：补充整体验收测试

建议测试文件：

- `tests/test_context.py`
- `tests/test_media_report_cli.py`
- `tests/test_plan.py`

测试覆盖：

- console script 配置可被读取。
- 课程上下文 JSON 解析成功和失败路径。
- live CLI 能接受新参数。
- dry-run 不调用导出函数。
- dry-run 写出 `plan.json`。
- 非 dry-run 现有行为保持。
- 默认 `max-clips` 过渡保护上限生效。

执行命令：

```bash
pytest
```

## 建议提交点 7

提交内容：只包含遗漏测试、测试夹具整理或文档微调，不混入新功能。

建议 commit message：

```text
test: 补充第一批 CLI 基础能力验收
```

提交前验证：

```bash
pytest
```

## 第一批完成定义

第一批完成时，应满足：

- `video-auto-editor` 作为安装后的 CLI 入口已定义。
- `python -m video_auto_editor` 仍可用。
- live 子命令接受 `--dry-run`、`--context-file`、`--allow-unreviewed-export`。
- 课程上下文 JSON 输入契约已稳定。
- `plan.json` 已成为 skill 后续读取拆条方案的机器入口。
- dry-run 不裁剪视频，但能生成基础拆条方案和报告。
- 现有测试和新增测试通过。
