---
name: live-autocut
description: >-
  把整段直播课/长直播按主题拆成发布就绪短视频的协作调度器。当用户说“把这节直播课拆条”“按主题切短视频”
  “给这段直播课生成发布就绪短视频”“直播回放剪成几条精华”时使用。本 skill 只做编排与解释：环境预检、
  课程上下文采集、调用 video-auto-editor live、解释产物、失败诊断与二次运行建议；不直接剪视频、不绕过
  CLI 写产物、不承载候选/评分/评审/导出算法，重活全部由 CLI 完成。
---

# 直播拆条调度器（live-autocut）

把一整段直播课拆成多条发布就绪短视频。本 skill 是**薄调度器**：围绕现有
`video-auto-editor live` CLI 底座提供面向用户的协作流程，自己不剪视频、不算候选、
不调用 ffmpeg/StepAudio/评审模型。

## 职责边界（务必遵守）

- ✅ 编排预检、上下文、调用、解释、诊断五步。
- ✅ 只读 `plan.json`、`metadata.json`、`拆条报告.md` 等已有产物做解释。
- ❌ 不直接调用 ffmpeg / StepAudio / 评审模型。
- ❌ 不绕过 CLI 直接写 `clips/`、`subtitles/`、`metadata.json` 等产物。
- ❌ 不在 skill 内复制候选时间计算、去重、评分、评审或边界补救逻辑。

所有重活（ASR、候选、去重、评分、主题评审、导出、报告）都由 CLI 完成。

## 协作流程

### 1. 环境预检

调用 `video_auto_editor.preflight.run_preflight()` 确认依赖齐备：

```python
from video_auto_editor.preflight import run_preflight
result = run_preflight()
```

- `result.ready is False`（存在 `error`）时，先把 `result.errors` 的 `detail` 与
  `hint` 念给用户，等修复后再继续，**不要带着 error 调用 CLI**。
- 仅有 `warn`（如评审关闭、缺少评审 Key）时可继续，但要提示将走未评审降级路径。

常见 error 与修复：
- 缺少 `STEPFUN_API_KEY` → `export STEPFUN_API_KEY=sk-...`
- 缺少 `ffmpeg` / `ffprobe` → 安装 ffmpeg 套件（`sudo apt install ffmpeg` / `brew install ffmpeg`）
- 缺少 `video-auto-editor` 命令 → 在仓库根目录 `pip install -e .`

### 2. 采集课程上下文

把用户提供的课程信息整理为合法上下文 JSON：

```python
from video_auto_editor.context import write_course_context
build = write_course_context(
    {
        "course_title": "...",
        "instructor": "...",
        "priority_topics": ["...", "..."],
        "excluded_topics": [...],
        "forbidden_terms": [...],
        "audience": "...",
        "notes": "...",
    },
    "out/live/course-context.json",
)
```

- 已知字段：`course_title`、`instructor`、`brand`、`audience`、`notes`（字符串），
  `priority_topics`、`excluded_topics`、`forbidden_terms`（字符串数组）。
- `build.unknown_fields` 中的字段不会写入交付 JSON；如有重要信息，提示用户改用已知字段。
- 类型非法时函数会报错，不会写出非法 JSON。课程信息不足时也可跳过上下文直接运行，但评审质量可能下降。

### 3. 调用 CLI（默认 reviewed 非 dry-run）

```bash
export STEPFUN_API_KEY=sk-...
video-auto-editor live path/to/live.mp4 \
  --output-dir out/live --work-dir work/live \
  --context-file out/live/course-context.json \
  --config-file config.json
```

支持用户显式开关，不要改写参数语义：
- `--config-file`：读取 JSON 配置文件覆盖 CLI 底座默认配置，可调整模型、阈值、ASR provider、分片时长等；API Key 仍通过 `STEPFUN_API_KEY` 等环境变量提供，不写入配置文件。
- `--dry-run`：只产出 `transcript.srt`、`plan.json`、报告，不导出视频。
- `--max-clips N`：限制导出数量。
- `--allow-unreviewed-export`：评审不可用时兼容导出未评审方案。

### 4. 解释产物

```python
from video_auto_editor.orchestration import interpret_output_dir
report = interpret_output_dir("out/live")
```

向用户结构化解释：
- `run_mode` / `run_mode_label`：运行模式（reviewed 导出 / reviewed dry-run / 未评审不导出 / 未评审兼容导出）。
- `exports`：导出清单（标题、主题、发布就绪评分、最终边界、视频与字幕相对路径）。
- `not_exported`：未导出候选与原因（`reason_label`、边界补救建议）。
- `human_review`：人工复核清单。
- `series`：同主题系列分组。
- `warnings`：告警汇总。

解释器只读产物，不重算候选、不重判发布就绪；缺少 `metadata.json`（dry-run 或失败）时按计划态解释。

### 5. 失败诊断与二次运行建议

```python
from video_auto_editor.orchestration import diagnose_run
diagnoses = diagnose_run(exit_code=code, has_transcript=transcript_exists, plan=plan)
```

按类型给出可执行建议与可复制的二次运行命令：
- `asr_failed`（无 `transcript.srt`、中止）：设置 `STEPFUN_API_KEY` 或切换 whisper 后重跑。
- `review_degraded`（评审关闭/不可用/缺 Key）：设置 Key 重跑，或 `--allow-unreviewed-export` 兼容导出。
- `no_publish_ready`（评审成功但无发布就绪候选，正常空导出）：降低阈值或换素材，`--dry-run` 先看方案。
- `missing_context`（缺课程上下文）：补充 `--context-file` 提升评审质量。

诊断只解释既有信号（退出码、warnings、缺失产物），不臆造失败原因。

## 不在本 skill 范围

发布平台上传与草稿、封面生成、标题 A/B、社媒/定时发布、跨场次系列化运营与素材库——
留待后续阶段，本 skill 不实现。
