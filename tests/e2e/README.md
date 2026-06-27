# 全流程端到端真实跑通测试（live 拆条）

本目录提供一个**真实跑通**的端到端测试，用真实素材 + 真实 `config.local.json` +
真实 StepFun API，完整跑通 `video-auto-editor live` 直到真实导出，并校验标准交付物。

与 `tests/` 下其它 mock 版 e2e 不同：这里**不 mock** ffmpeg / StepAudio ASR /
StepFun 评审，会真实调用 StepFun API（消耗额度）并执行 ffmpeg 真实剪辑。

## 文件

| 文件 | 作用 |
| --- | --- |
| `run_live_e2e.sh` | 真实跑通脚本：预检 → 调 CLI 完整导出 → 调校验器 |
| `verify_live_deliverables.py` | 只读 `out/` 产物与配置，断言标准交付物和短视频字幕契约自洽 |
| `test_verify_live_deliverables.py` | 离线构造交付目录，覆盖校验器的字幕功能断言 |

## 前置条件

1. **设置 API Key**（ASR 与主题评审都依赖）：
   ```bash
   export STEPFUN_API_KEY=sk-...
   ```
2. **安装 ffmpeg / ffprobe**：`sudo apt install ffmpeg` 或 `brew install ffmpeg`。
3. **安装 CLI**（在仓库根目录）：`pip install -e .`。
   未安装时脚本会自动回退到 `python -m video_auto_editor`。
4. 仓库根目录存在输入视频和 `config.local.json`（两者都被 `.gitignore` 忽略，需本地自备）。

## 运行

```bash
export STEPFUN_API_KEY=sk-...
bash tests/e2e/run_live_e2e.sh
```

可用环境变量覆盖默认值：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `E2E_VIDEO` | 根目录的宣讲 mp4 | 输入直播视频 |
| `E2E_CONFIG` | `config.local.json` | 配置文件 |
| `E2E_OUT` | `out` | 输出目录 |
| `E2E_WORK` | `work` | 工作目录（复用已有 ASR 缓存，避免重复转写） |
| `E2E_MAX_CLIPS` | 空 | 可选，传递给 `--max-clips`，便于快速验证导出链路 |
| `E2E_ALLOW_UNREVIEWED_EXPORT` | 空 | 可选，仅用于人工兼容验证；标准校验仍要求 `reviewed` |
| `E2E_EXTRA_ARGS` | 空 | 可选，追加 CLI 参数；谨慎使用，不改变默认流程 |

成功时校验器打印 `E2E PASS`，`out/` 下产出标准交付物。

推荐复跑命令：

```bash
E2E_VIDEO="妇美·愉悦技术规范宣讲——张铃院长（2026年6月13日）.mp4" \
E2E_CONFIG="config.local.json" \
E2E_OUT="out/e2e-real-$(date +%Y%m%d-%H%M%S)" \
E2E_WORK="work" \
bash tests/e2e/run_live_e2e.sh
```

复跑后记录以下信息，便于区分缓存命中与真实请求：

| 项目 | 记录口径 |
| --- | --- |
| 转写文本 | 终端 `Loaded ... transcript chunks from cache/stepaudio`，说明来自缓存还是真实 ASR |
| 主题评审 | 查看终端 Step 6、`plan.json` warnings 和 `work/<video_name>/topic_review_cache/`，记录是真实请求还是命中主题评审缓存 |
| 标准交付物 | 记录是否生成 `metadata.json`、`clips/`、`subtitles/`，以及校验器是否输出 `E2E PASS` |

## 标准交付物（校验器会逐项断言）

| 交付物 | 校验要点 |
| --- | --- |
| `transcript.srt` | 存在、非空、含 SRT 时间轴行（`-->`） |
| `plan.json` | `status == reviewed`、`dry_run == false`、`candidates`/`exports` 非空、`export_count >= 1` |
| `metadata.json` | `status == reviewed`、`clips` 非空，每个 clip 有 `title`/`summary`/`output_path` 且文件存在非空 |
| `clips/*.mp4` | 数量 >= 1，文件非空 |
| `subtitles/*.srt` | 数量 >= 1，且与 `metadata.json` / `plan.json` 引用路径完全一致 |
| 短视频字幕内容 | 每个 SRT 可解析、cue 序号连续、时间轴不倒退、时间上界不超过带 buffer 的片段时长 |
| 字幕后处理 | `subtitles/*.srt` 不含纯语气词 token；每条 cue 的行数和单行长度符合 `subtitle_max_lines` / `subtitle_max_chars_per_line` |
| `拆条报告.md` | 含「非 dry-run 交付包」、`metadata.json` 交付物 yes 行，以及与配置一致的「字幕烧录」状态 |
| 计数一致 | `metadata.clips` 数 == `clips/*.mp4` 数 == `subtitles/*.srt` 数；不允许陈旧 `clips/*.mp4` / `subtitles/*.srt` 混入 |

也可单独跑校验器（针对已存在的产物目录）：

```bash
python tests/e2e/verify_live_deliverables.py out --config-file config.local.json
```

## 常见失败与处理

- **缺少 `STEPFUN_API_KEY`** → 预检在 `STEPFUN_API_KEY` / `asr_provider` 处报 `error`，
  脚本非 0 退出。按提示 `export STEPFUN_API_KEY=sk-...` 后重跑。
- **主题评审失败 / 评审关闭** → `plan.json` 的 `status` 退化为 `unreviewed`，默认不导出
  发布就绪短视频，校验器会因 `status != reviewed` 判 FAIL。检查评审模型配置与额度，
  或确认确实需要发布就绪导出。
- **缺少 `ffmpeg` / `ffprobe`** → 预检报 `error`，安装 ffmpeg 套件后重跑。
- **未安装 CLI** → 脚本自动回退 `python -m video_auto_editor`，不影响跑通。
