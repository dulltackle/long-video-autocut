# 直播拆条 CLI 生产就绪规格

状态：实施基线  
语言：中文  
适用范围：`live` 直播拆条主路径  
目标平台：Ubuntu 24.04 LTS amd64

## 1. 文档目的与权威边界

本文汇编“制定直播拆条 CLI 生产就绪改进规格”地图中的全部已决结论，
定义后续实施必须满足的当前契约。实施者可以在不补做产品或架构决策的前提下，
按本文的依赖顺序修改生产代码、测试、文档、安装与发布流程。

本文规定“做什么、模块之间如何协作、如何验收”。ADR 保存难以逆转决定的理由，
`CONTEXT.md` 保存领域词汇；三者不得互相冲突。发现冲突时必须先修正文档，
不得自行选择一个隐式优先级。

本文的决策来源如下：

- [定义生产级运行状态机与失败契约](https://github.com/dulltackle/long-video-autocut/issues/2)
- [定义供应商无感知的语音识别模块契约](https://github.com/dulltackle/long-video-autocut/issues/3)
- [定义主题评审与字幕优化的模型端口契约](https://github.com/dulltackle/long-video-autocut/issues/4)
- [确定 Linux 原生安装与环境预检基线](https://github.com/dulltackle/long-video-autocut/issues/5)
- [收敛 live-only CLI 的公共接口](https://github.com/dulltackle/long-video-autocut/issues/6)
- [定义识别分片补全与重叠归并规则](https://github.com/dulltackle/long-video-autocut/issues/7)
- [定义缓存身份、版本与原子写入契约](https://github.com/dulltackle/long-video-autocut/issues/8)
- [定义标准交付物 schema 与稳定标识](https://github.com/dulltackle/long-video-autocut/issues/9)
- [定义结构化日志、运行清单与错误分类](https://github.com/dulltackle/long-video-autocut/issues/10)
- [定义敏感数据、供应商披露与留存契约](https://github.com/dulltackle/long-video-autocut/issues/11)
- [定义生产验收矩阵与发布门禁](https://github.com/dulltackle/long-video-autocut/issues/12)
- [确定生产模块组合与实施顺序](https://github.com/dulltackle/long-video-autocut/issues/14)
- [确定既有 ADR 的废止、修订与替代方案](https://github.com/dulltackle/long-video-autocut/issues/15)

## 2. 范围

### 2.1 范围内

- 单个 MP4 素材的 `live` 直播拆条。
- StepAudio 语音识别 Adapter、StepFun 文本模型 Adapter。
- 供自动验收使用的确定性 Adapter。
- Linux 原生安装、环境预检、单机单次运行内部并发。
- 内容寻址处理缓存、运行诊断、标准交付物、原子发布与上一版备份。
- 生产版本的自动门禁、真实供应商门禁、人工内容复核和签名发布证据。
- 删除已废止的旧架构、旧命令、旧配置、旧缓存和旧交付物路径。

### 2.2 范围外

- `single`、`batch` 及其兼容层。
- 旧命令、旧配置、旧缓存、旧交付物 schema 的迁移。
- 未评审导出、字幕优化降级交付、短视频旁挂字幕。
- 容器化、多租户、分布式执行和服务化运行。
- macOS 与 Windows 的生产认证。
- 性能、内存、磁盘、吞吐、并发规模和外部 API 成本预算。
- 本地数据加密、安全擦除和供应商侧留存保证。

## 3. 不可协商的生产语义

1. 只有完整主题评审、字幕优化、交付构建、完整性验证和原子发布全部成功，
   直播拆条运行才成功。
2. 没有发布就绪短视频是“有效空结果”，不是失败；它仍发布完整的标准交付物。
3. 供应商失败、主题评审失败、字幕优化失败、导出失败或验证失败均不得伪装成
   有效空结果，不得发布半成品。
4. 失败和中断保留脱敏运行诊断包及已原子写入的有效缓存，但不产生新标准交付物。
5. 生产实现不保留旧命令、旧公共接口、旧降级语义或隐藏兼容路径。
6. 模块不得修改调用方传入的配置或业务对象；跨模块只传递不可变、类型化结果。
7. 缓存、诊断、交付和发布均必须使用明确 schema、严格校验和断电安全写入。
8. 机密凭据不得持久化；用户内容一律按敏感内容处理。

## 4. 生产环境与安装

### 4.1 认证基线

| 项目 | 生产要求 |
| --- | --- |
| 操作系统 | Ubuntu 24.04 LTS，`amd64` |
| Python | CPython `>=3.12.3,<3.13`，版本化虚拟环境 |
| FFmpeg | `>=6.1,<7`，参考 deb `7:6.1.1-3ubuntu5` |
| ffprobe | 与 FFmpeg 来自同包且上游版本相同 |
| fontconfig | 参考 deb `2.15.0-1.1ubuntu2` |
| 中文字体 | `fonts-noto-cjk`，参考 deb `1:20230817+repack1-3` |
| 默认字体家族 | `Noto Sans CJK SC` |

版本号不能替代能力检查。FFmpeg 必须实际具备 `subtitles`/libass、
`libx264`、AAC、MP4 输出和 ffprobe JSON 读取能力，并完成中文字幕烧录烟测。

### 4.2 依赖与构建物

- 系统包由发布清单指定的 Ubuntu archive snapshot 安装，记录 snapshot ID 和
  全部实际 deb 版本。
- 应用使用预构建 wheel；生产机不得从 Git checkout、sdist 或 editable install
  构建或导入。
- CPython 依赖使用 `requirements-runtime.lock`。移除 Whisper 后，目标运行时
  没有第三方 PyPI 依赖；未来依赖必须精确固定全部传递依赖并附 SHA-256。
- 构建依赖使用独立的 `requirements-build.lock`。
- wheel、锁文件和安装清单分别计算并校验 SHA-256。

### 4.3 安装脚本

实现非交互、幂等的 `scripts/install-production.sh`，输入至少包括 wheel 路径、
wheel 预期 SHA-256、`apt_snapshot_id` 和安装前缀。

固定执行顺序：

1. 验证 Ubuntu 24.04、`amd64` 和系统包安装权限。
2. 从同一 snapshot 安装 `python3.12`、`python3.12-venv`、`ffmpeg`、
   `fontconfig`、`fonts-noto-cjk`、`ca-certificates`。
3. 用 `python3.12 -m venv` 创建版本化虚拟环境。
4. 校验并从本地 wheelhouse 安装带哈希运行依赖，再以 `--no-deps` 安装应用 wheel。
5. 原子切换“当前版本”符号链接，不覆盖正在使用的虚拟环境。
6. 执行严格环境预检。
7. 只有预检通过才将版本标记为可运行，并输出安装清单。

安装脚本不得使用 PPA、未记录的 apt 源、裸 `pip install`、无哈希依赖解析或
生产机源码构建。

### 4.4 严格环境预检

所有硬检查在首次远程请求前完成，并一次返回全部已发现问题：

- 平台、架构、CPython 版本、虚拟环境、应用版本和安装前缀一致性。
- FFmpeg/ffprobe 版本一致，具备 `subtitles`、`libx264`、AAC 和 MP4 能力。
- ffprobe 能以 JSON 读取容器、视频流、音频流和时长。
- `fc-list`、`fc-match` 命中生效配置指定的中文字体。
- 在临时目录完成一秒中文 SRT 的实际烧录与 ffprobe 复核。
- TLS CA 可加载；选定 Adapter 端点为 HTTPS；所需密钥环境变量存在且非空。
- workspace 可创建、可写，暂存与交付位于同一文件系统，并通过
  `fsync`、原子重命名和清理烟测。
- 配置与课程上下文已经严格校验；用户素材的 ffprobe 业务验证由后续
  `source_analysis` 阶段完成，并且仍早于任何远程请求。

预检不发供应商业务请求，不设置本规格明确排除的资源预算阈值。

## 5. 公共 CLI 契约

### 5.1 命令面

```text
video-auto-editor live SOURCE [--workspace-dir DIR] [--overwrite]
video-auto-editor cache clear WORKSPACE
```

- `live` 是唯一业务命令。
- `cache clear` 是独立管理命令，不创建直播拆条运行。
- 保留通用 `--help` 与 `--version`；两者不创建直播拆条运行。
- 删除 `single`、`batch`、`--output-dir`、`--work-dir`、`--config-file`、
  `--context-file`、`--max-clips`、`--dry-run` 和所有未评审导出入口。

### 5.2 素材、源旁文件与 workspace

对 `/media/course.mp4`：

```text
/media/course.mp4
/media/course.config.json   # 可选
/media/course.context.json  # 可选
/media/course.autocut/      # 默认 workspace
```

- `SOURCE` 只接受单个 MP4；必须用 ffprobe 验证容器、视频流、音频流和有效时长。
- 显式 `--workspace-dir` 替换默认 workspace 根。
- 操作员传入的素材或 workspace 可以是符号链接，但启动时必须解析为规范绝对路径。
- 素材最终必须是普通文件，workspace 最终必须是目录。
- 默认配置、课程上下文和默认 workspace 都以解析后的素材所在目录为基准。

workspace 固定布局：

```text
course.autocut/
├── <版本化 workspace 标记>
├── delivery/
├── delivery.previous/
└── work/
    ├── cache/
    ├── runs/
    └── <受管临时目录>
```

- 非空但无有效标记的目录不得接管、覆盖或清理。
- workspace 标记不绑定素材路径；内容变化由缓存身份区分。
- `delivery/` 与 `work/` 是兄弟目录；模块不得自行拼接受管路径。
- 新建受管目录默认权限 `0700`，敏感文件默认权限 `0600`。

### 5.3 发布与覆盖

- `delivery/` 不存在或为空时允许发布。
- 非空时默认在创建 `run_id` 和诊断包后拒绝，原内容不变。
- 只有显式 `--overwrite` 可替换；新交付物必须先完整构建并验证。
- 成功覆盖后只保留紧邻上一版 `delivery.previous/`。
- 覆盖发布失败必须恢复原 `delivery/`；不得无限累积时间戳备份。

### 5.4 缓存清理

`video-auto-editor cache clear WORKSPACE`：

- 验证 workspace 标记并取得维护锁；存在活动直播拆条运行时拒绝。
- 删除 `work/cache/` 下全部命名空间、隔离项、遗留临时文件和缓存锁状态，
  然后重建空缓存目录。
- 重复执行仍成功。
- 不触碰素材、课程上下文、`work/runs/`、`delivery/` 或
  `delivery.previous/`。

## 6. 配置与课程上下文

### 6.1 配置模型

保留 JSON 加 `dict` 的输入模型，但 `Configuration` 必须：

- 先读取认证版本的内置默认配置，再应用源旁 JSON 的严格递归覆盖。
- 要求显式、受支持的 `schema_version`。
- 拒绝 `null`、未知字段、错误类型、范围错误、跨字段冲突和不完整的 Adapter 切换。
- 为本次运行形成独立、不可变的生效配置；模块不得修改输入 `dict`。
- 在运行诊断中只记录脱敏白名单投影和配置指纹，不复制原始配置。

公共配置只允许：

- `transcription_provider` 与 `transcription_provider_config`：
  已认证 Adapter 标识、模型、HTTPS 端点、密钥环境变量名、超时、最大并发。
- `text_model_provider` 与 `text_model_provider_config`：
  已认证 Adapter 标识、HTTPS 端点、密钥环境变量名、超时、最大并发。
- `topic_review` 与 `subtitle_optimization`：
  模型、温度、推理强度、最大输出 token。
- `clip_policy`：
  最短、目标、最长时长，可选 `max_clips` 和 `0–100` 发布就绪阈值；
  必须满足 `min <= target <= max`，省略 `max_clips` 表示不限制数量。
- `subtitle_style`：
  字体、字号、描边、底部边距、每行最大字符数、最大行数。
- `delivery_build_concurrency`。

不得公开：

- 媒体编码参数。
- 静音阈值、评分权重、重复阈值、主题重叠、片段间隔和边界扩展。
- 识别分片、覆盖补全、归并、提示、校验、缓存、内部重试和调度算法版本。
- 关闭主题评审、字幕优化或字幕烧录的开关。

生产 JSON 只能选择当前安装版本注册并通过生产认证的 Adapter。确定性 Adapter
只能由测试组合根注入。密钥值只从配置指定的环境变量读取。

### 6.2 课程上下文

`course.context.json` 可不存在；存在时要求受支持的 `schema_version`，拒绝未知
字段、错误类型、空字符串以及越界长度或数量。

公共字段：

- 必填 `course_topic`。
- 可选 `attribution`。
- 可选字符串数组 `priority_topics`。
- 可选字符串数组 `excluded_content`。

删除 `notes`、`audience`、`forbidden_terms`、`course_title`、`instructor`、
`brand` 和 `excluded_topics`。

## 7. 直播拆条运行生命周期

### 7.1 运行边界

- 参数缺失、未知选项和参数类型错误由解析器退出 `2`，不创建 `run_id`。
- 参数结构解析成功后立即创建 `run_id` 并初始化运行诊断包。
- 此后的配置语义错误属于正式失败运行。
- 每次复跑创建新的 `run_id`；复用有效缓存，不恢复旧运行状态。

### 7.2 状态机

非终止阶段严格单向：

```text
initialized
  → preflight
  → source_analysis
  → transcription
  → candidate_planning
  → topic_review
  → delivery_build
  → delivery_verification
  → publishing
```

终止状态只有：

- `succeeded`：越过发布提交点；`result_kind` 为 `clips` 或 `empty`。
- `failed`：配置、环境、输入、供应商、本地处理、交付或发布失败。
- `interrupted`：发布提交点前完成处理的 `SIGINT` 或 `SIGTERM`。

重试、缓存命中、覆盖补救和中断请求是事件，不是阶段。阶段不可回退。

### 7.3 退出码

| 退出码 | 稳定含义 |
| ---: | --- |
| `0` | 成功，包括有效空结果 |
| `2` | 命令参数或配置无效 |
| `10` | 环境预检失败 |
| `20` | 输入素材不可读取或不受支持 |
| `30` | 外部供应商失败或供应商业务输出无效 |
| `40` | 本地处理失败 |
| `50` | 交付构建或完整性验证失败 |
| `60` | 原子发布失败 |
| `70` | 未分类内部错误 |
| `130` | `SIGINT` 中断 |
| `143` | `SIGTERM` 中断 |

退出码只表达粗粒度类别。稳定错误码、失败阶段、可重试性、人工动作和脱敏根因
由运行诊断表达。

### 7.4 并发、失败和中断

- 每个深模块拥有自己的并发、fail-fast、排队取消和在途请求或媒体子进程终止。
- 首个确定性失败成为主错误，停止派发新工作；竞态中已观察到的其他失败为关联错误。
- 应用拥有根取消和信号语义。首次信号进入最长 10 秒的受控清理窗口；
  再次信号立即执行最佳努力清理并退出。
- 只允许正在进行的原子缓存写入完成，不为收集更多错误继续业务工作。
- 发布提交点是单线程短临界区。提交点前信号导致回滚并终止为 `interrupted`；
  提交点后交付已完整可见，运行视为 `succeeded`。

### 7.5 失败清理

失败或中断：

- 保持运行开始前的 `delivery/` 与 `delivery.previous/` 状态。
- 删除临时音频、识别分片、未发布短视频、临时交付目录、未完成缓存文件及
  其他包含正文的受管中间文件。
- 保留已验证并原子写入的有效缓存和脱敏运行诊断包。
- 清理失败记录关联错误并设置 `recovery_incomplete`，不得为了调试保留敏感现场。
- 强制终止遗留物由下次启动在新业务工作前清理。

## 8. 生产模块组合

### 8.1 顶层接口与依赖方向

CLI 只负责语法解析、调用应用和渲染终态。唯一顶层业务 seam：

```python
LiveApplication.execute(request: LiveRunRequest) -> LiveRunOutcome
```

依赖方向固定为：

```text
CLI / 生产组合根
        ↓
LiveApplication
        ↓
业务深模块
        ↓
模块内部端口
        ↓
生产与测试 Adapter
```

生产组合根是唯一可同时导入模块接口和具体生产 Adapter 的位置。不得建立全局
依赖容器、万能上下文对象或通用文件系统抽象。

### 8.2 深模块及事实所有权

| 模块 | 对外承诺与事实所有权 |
| --- | --- |
| `Configuration` | 发现、严格校验并形成不可变生效配置与课程上下文 |
| `Workspace` | 标记、规范路径、权限、锁、遗留物清理和受管目录 capability |
| `Readiness` | 聚合环境、媒体、目录和选定 Adapter 准备情况 |
| `SourceAnalysis` | 验证素材并形成唯一不可变素材描述 |
| `SpeechRecognition` | 音频准备、识别分片、覆盖、归并、缓存和错误翻译 |
| `ClipPlanning` | 候选、边界、选择、数量上限和同主题系列 |
| `TopicReview` | 相邻候选批次、提示、业务校验、缓存和语义重试 |
| `SubtitleOptimization` | 子窗口、提示、子序列校验、时间对齐、缓存和语义重试 |
| `DeliveryBuild` | 仅从已决业务事实构建未发布标准交付物 |
| `DeliveryVerification` | 以消费者视角独立验证，不修复交付物 |
| `Publication` | 备份、提交、发布提交点、回滚和目录耐久同步 |
| `RunDiagnostics` | 事件序列、运行诊断清单、schema、脱敏和耐久写入 |

模块设计规则：

- 一个模块只有一个实际实现时，不额外创建 `Protocol + Adapter`。
- 真实存在变化的 seam 才使用 Adapter：语音识别、文本模型、缓存仓库和诊断接收。
- 外部接口保持小而深；内部 seam 不因测试需要泄漏给 `LiveApplication`。
- 类型和 schema 跟随事实所有者；不创建全局 `models.py`。
- 不创建 `utils.py`、`helpers.py`、`common.py`，不使用模糊的
  `service` 或 `manager` 命名。

### 8.3 标识与不可变数据

- `LiveApplication` 创建 `run_id`，独占生命周期、阶段、终态、主错误和根取消。
- 语音识别成功后，应用为本次运行创建 `transcript_id` 和
  `transcript_chunk_id`；缓存不保存运行标识。
- `ClipPlanning.prepare()` 创建 `plan_id` 和 `candidate_id`。
- `ClipPlanning.finalize()` 创建 `short_video_id` 和必要的 `series_id`。
- `TopicReview` 按 `candidate_id` 返回结果，不修改候选。
- `SubtitleOptimization` 按 `short_video_id` 返回字幕显示块，不修改转写或拆条方案。
- `DeliveryBuild` 只序列化既有标识，不重新执行主题、选择或字幕质量判断。
- 诊断 `operation_id`、`error_id` 与业务标识隔离，不进入缓存身份。

### 8.4 启动与装配顺序

```text
CLI 语法解析
→ 创建 run_id
→ 打开 workspace、取得运行锁、初始化诊断
→ Configuration.load()
→ 私有 RunAssembly 按固定注册表装配模块
→ 聚合环境预检
→ 业务阶段
```

`RunAssembly` 是组合根私有实现，不是业务代码可查询的依赖容器。

### 8.5 目标包布局

```text
video_auto_editor/
├── cli.py
├── composition.py
├── application/
│   ├── live.py
│   └── cache_maintenance.py
├── runtime/
│   ├── cancellation.py
│   ├── errors.py
│   └── identity.py
├── configuration/
├── workspace/
├── diagnostics/
├── cache/
├── source_analysis/
├── transcription/
│   ├── interface.py
│   ├── stepaudio.py
│   ├── deterministic.py
│   └── reconciliation.py
├── clip_planning/
├── text_model/
│   ├── interface.py
│   ├── stepfun.py
│   └── deterministic.py
├── topic_review/
├── subtitle_optimization/
└── delivery/
    ├── build.py
    ├── verification.py
    ├── publication.py
    └── schema/
```

`runtime/` 只保存运行机制，不保存业务事实。旧扁平模块在新模块接管后删除，
不保留转发导入。

## 9. 业务模块契约

### 9.1 语音识别模块

最小接口：

```python
check_readiness() -> ReadinessReport
transcribe(request: TranscriptionRequest) -> TranscriptionResult
```

`check_readiness()` 本地、只读、无远程请求、无持久化写入且可重复调用；一次返回
全部阻塞问题，但不承诺随后处理必然成功。

`TranscriptionRequest` 只含已验证素材描述、模块私有 workspace capability 和
取消令牌。供应商配置、缓存仓库、媒体执行与诊断 scope 在组合根构造 Adapter 时
注入。

`transcribe()` 是同步的全有或全无阶段。成功结果只含：

- 按素材全局时间排序的非空转写文本块及合法开始、结束时间。
- 可选、与 Unicode 码点一一对应且位于文本块内的逐字时间。
- `speech_presence: present | absent`；确认无语音时允许空文本块。
- 中性 `ExecutionFacts`：缓存、内部重试和覆盖补救统计。

不得返回供应商名称、原始响应、缓存路径、分片路径或部分结果。缺失逐字时间时，
下游可以按文本块时间回退，但不得伪装为供应商逐字时间。

预期失败抛出 `TranscriptionFailure`，至少携带稳定类别、安全消息、
`retryable_in_new_run`、执行事实和脱敏诊断。编排层只按类别映射退出码，
不得解析异常文本或供应商异常类型。

StepAudio 与确定性 Adapter 共用同一契约测试。确定性 Adapter 只消费预编排脚本，
不访问网络、媒体工具、真实缓存、环境变量或系统时钟。

### 9.2 识别分片、覆盖与归并

算法必须采用：

1. 先把素材时间轴划分为连续、无重叠、无缺口的核心区。
2. 请求区在核心区两侧增加有限上下文重叠；每个时间点的输出只归一个核心区。
3. 从标准化音频独立、确定性地生成“确认语音区间”。
4. 以已接受文本的实测时间区间形成覆盖账本。
5. 对确认语音覆盖缺口或文本冲突执行有界、定向、可证明进展的补转。
6. 所有语音覆盖完成且所有冲突消解后才返回完整结果。

归并规则：

- 先依据时间判断是否为同一次话语，再比较文字。
- 同次话语忽略空白和标点后等价时，只保留一份真实响应；优先逐字时间实测、
  离请求边缘更远的候选。
- 展示文本和时间必须来自同一候选，不能拼出供应商未返回的句子。
- 前后相继的真实重复话语必须保留。
- 有语义差异的重叠结果触发定向补转；至少两个独立观察规范化后达成一致才接受。
- 并发完成顺序不得改变结果。

补转规则：

- 目标两侧带有限上下文；长缺口拆成比常规分片短的核心区。
- 按稳定时间顺序规划，每轮必须增加覆盖或减少冲突；无进展立即停止。
- 每区尝试次数、补转长度、总补转音频时长和请求数均有版本化硬上限。
- 预算耗尽、请求失败、空响应、残余语音缺口或无共识全部抛出外部失败。
- 尾部静音、整段确认无语音、有语音但供应商空响应是三种不同语义。

### 9.3 `ClipPlanning`

两阶段接口：

```python
prepare(
    source_analysis,
    transcript,
    course_context,
    clip_policy,
) -> CandidatePlan

finalize(
    candidate_plan,
    review_result,
) -> DeliveryPlan
```

- `prepare()` 位于 `candidate_planning`，产生候选、初始边界和评审上下文。
- `finalize()` 位于 `topic_review` 末尾，归并评审，执行边界补救、
  发布就绪判断、数量上限、系列关系和最终导出选择。
- 零候选时主题评审是成功空操作，最终形成 `result_kind=empty`。
- `priority_topics` 只影响达到发布就绪标准后的优先保留与主题覆盖。
- `excluded_content` 命中时禁止交付。
- 默认导出全部发布就绪短视频；`max_clips` 只有显式配置时才限制数量。

### 9.4 主题评审与字幕优化

编排层只依赖阶段级业务模块：

```python
TopicReview.check_readiness() -> ReadinessReport
TopicReview.review(...) -> TopicReviewResult

SubtitleOptimization.check_readiness() -> ReadinessReport
SubtitleOptimization.optimize(...) -> SubtitleOptimizationResult
```

- `TopicReview` 一次接收全部候选、课程上下文和评审约束，内部拥有相邻候选批次、
  并发、提示、校验、语义重试与缓存。
- `SubtitleOptimization` 一次接收全部待发布短视频和带时间转写，内部拥有字符预算
  子窗口、并发、抽取式提示、子序列校验、时间对齐、语义重试与缓存。
- 空输入成功且不调用模型。
- 任一工作项失败不返回部分结果；已原子写入的其他有效缓存可保留。
- 字幕优化与烧录是发布就绪短视频的强制路径，不存在关闭或规则字幕降级交付。

字幕优化必须保留以下已验证算法：

- 按字符预算在短视频窗口内对连续文本分组，不重新拆散转写文本块。
- 提示把任务定义为“只删除字符”的抽取式删减，禁止新增、改写、同义替换、
  调整语序或增加空格。
- 输出为原文子序列；校验通过后按逐字时间对齐字幕显示块。
- 缺少逐字时间时使用文本块内确定性时间回退。
- 任一子窗口请求、校验或对齐失败使整个字幕优化阶段失败。

### 9.5 文本模型端口

两个业务模块内部共享：

```python
check_readiness() -> ReadinessReport
generate(request: TextGenerationRequest) -> TextGenerationResponse
```

首期只支持同步、非流式、无会话状态的纯文本生成，不支持流式、工具、多模态、
供应商批处理或会话持久化。

请求只含：

- 提示消息。
- 模型标识、温度、推理强度、可选输出上限。
- `run_id`、阶段、操作标识组成的观测上下文。
- 取消令牌。

API Key、Base URL、HTTP 头、供应商名称、传输超时、传输重试和供应商并发限制
在构造 Adapter 时注入。

端口返回模型原始文本和中性执行事实；不解析业务结果。StepFun Adapter 在一次
`generate()` 内处理瞬时传输重试；业务校验失败后的语义重试由业务模块负责。

类型化端口失败至少区分：配置无效、鉴权失败、请求被拒绝、限流、超时、
服务不可用、协议响应无效、生成被拒绝、输出截断、取消和内部错误。

StepFun 与确定性 Adapter 共用端口契约。业务模块测试使用确定性 Adapter；
StepFun 测试使用伪传输或本机回环，不访问真实供应商。

## 10. 处理缓存

### 10.1 seam 与职责

共享缓存仓库是业务模块的内部 seam：

- 文件系统生产 Adapter 与内存测试 Adapter 满足同一接口。
- 业务模块拥有身份文档、payload schema、算法版本和业务校验。
- 仓库只拥有命名空间、统一 envelope、锁、物理布局、摘要、原子发布、
  损坏隔离和通用诊断。
- 仓库按身份提供可取消独占 claim；取得后重新查询，仍未命中才计算和发布。

### 10.2 摘要与规范化

- 音频确定性转换为单声道、16 kHz、`s16le` 标准化 PCM。
- 音频摘要包含规则版本、格式、采样率、声道数、字节长度和 PCM 字节，
  以 SHA-256 流式计算。
- 视频路径、文件名、mtime 和无关画面或容器元数据不参与转写身份。
- 其他身份使用 UTF-8、键排序、无无效空白的规范化 JSON。
- 时间使用整数毫秒或采样序号；业务字符串不得由缓存模块改写。
- 键为命名空间内身份文档的 SHA-256；payload 另有 SHA-256 和字节长度。

### 10.3 四类缓存身份

| 命名空间 | 身份必须覆盖 |
| --- | --- |
| 整场转写 | 整场 PCM 摘要、Adapter/模型/语言/配置指纹、音频规则、分片规划、核心区、归并、语音证据、覆盖、重复消解、结果校验及 schema 版本 |
| 识别分片 | 实际分片 PCM 摘要与采样长度、Adapter/模型/语言/配置指纹、请求构造、响应解析、结果校验及 schema 版本 |
| 主题评审 | 完整相邻候选批次、前后文、课程上下文、评审约束、批次算法、最终提示摘要与版本、语义重试、模型设置、配置指纹、解析/校验及 schema 版本 |
| 字幕优化 | 实际子窗口原文、显示块约束、最终提示摘要与版本、语义重试、模型设置、配置指纹、解析/子序列校验及 schema 版本 |

分片序号、源路径和全局绝对时间不参与识别分片身份；payload 保存相对时间。
字幕优化 payload 只保存已校验文字显示块，不保存时间；命中后必须按当前转写
重新执行子序列校验和时间对齐。

超时、重试次数、退避、并发、`run_id`、workspace 路径、日志级别、密钥值、
密钥环境变量名和复跑状态不参与身份。

每个命名空间独立版本化，不使用全局算法版本。程序版本只记录来源，不自动导致
失效；供应商同名模型的已知修订必须进入模型标识、配置指纹或相关版本。

### 10.4 envelope、单航班与原子发布

统一 envelope 至少包含：

- `envelope_schema_version`、`namespace`。
- 脱敏身份清单、身份摘要、输入摘要、算法版本。
- `payload_schema_version`、payload 摘要、字节长度和 payload。
- 创建时间、生产程序版本、Adapter、模型和不透明配置指纹。

envelope 不复制绝对路径、课程上下文原文、完整提示、供应商 URL、密钥或请求头。
payload 仍是敏感内容。

固定内部布局：

```text
work/cache/<namespace>/<digest-prefix>/<full-digest>.json
```

相同身份使用跨线程、跨进程 Linux 锁单航班；不同身份可并发。写入必须：

1. 在目标目录创建唯一临时文件。
2. 完整序列化，刷新并 `fsync` 文件。
3. 持锁原子重命名到目标路径。
4. `fsync` 父目录。
5. 所有步骤完成后才报告成功。

有效条目不可修改；同一身份已有有效条目时发布幂等成功。只缓存完整、通过业务
校验的成功结果。

### 10.5 损坏与基础设施失败

- 不存在、版本失配或身份不匹配是普通未命中。
- JSON/UTF-8、摘要、字段、身份、payload schema 或业务校验失败是损坏。
- 损坏条目在持锁状态下原子移入 `work/cache/` 内隔离区，记录脱敏原因后按未命中
  重算；不尝试部分恢复。
- 权限、磁盘、同步、锁或原子发布失败是本地处理失败，不得降级为未命中。
- 条目无 TTL，不迁移旧缓存；由 `cache clear` 统一物理清理。

## 11. 标准交付物

### 11.1 固定目录

```text
delivery/
├── manifest.json
├── transcript.json
├── transcript.srt
├── plan.json
├── metadata.json
├── report.md
└── clips/
    └── short_video_<uuid>.mp4
```

- `manifest.json` 是交付运行清单和根索引。
- `transcript.json` 是忠实转写机器真相；`transcript.srt` 是确定性可读渲染。
- `plan.json` 拥有候选、主题评审和导出选择事实。
- `metadata.json` 拥有已发布短视频目录和同主题系列关系。
- `report.md` 只渲染既有事实，不是机器判断来源。
- `clips/` 只含完成字幕优化并烧录字幕的短视频。
- 有效空结果仍包含六个固定文件和空 `clips/`。

### 11.2 通用 schema

- 文档独立版本化：首版为 `delivery_manifest.v1`、`transcript.v1`、
  `clip_plan.v1`、`short_video_catalog.v1`。
- 必填字段、未知字段、类型、枚举和有限数严格校验。
- UTC 时间使用 RFC 3339；素材时间使用非负整数毫秒。
- 相对路径以 `delivery/` 为根，使用 POSIX 语义；禁止绝对路径、反斜杠、
  空段、`.`、`..`、符号链接和路径越界；路径唯一且 Unicode NFC。
- 所有 JSON 含相同 `run_id`；跨文档关系不得依赖数组位置、标题或路径猜测。

实体 ID 使用类型前缀加规范小写 UUIDv4：

```text
run_<uuid>
transcript_<uuid>
transcript_chunk_<uuid>
plan_<uuid>
candidate_<uuid>
short_video_<uuid>
series_<uuid>
```

标识在单次运行内从创建起稳定，并在已发布交付中永久不变。复跑创建全新标识，
不承诺跨运行识别同一实体。业务标识不由内容摘要派生。

### 11.3 `manifest.json`

至少包含：

- schema、`run_id`、`terminal_state: succeeded`、
  `result_kind: clips | empty`。
- 开始和发布时间、生产程序版本。
- 素材 SHA-256、字节长度和时长，不记录绝对路径。
- 四个业务文档和报告引用。
- 除自身外全部常规文件的相对路径、角色、媒体类型、字节长度和 SHA-256。
- 成功运行的精简 `execution` 审计投影。

`files` 按路径排序并与实际常规文件形成精确集合。交付运行清单不复制候选、
评审、标题或报告事实。

### 11.4 `transcript.json` 与 `transcript.srt`

`transcript.json` 至少包含 schema、`run_id`、`transcript_id`、
`speech_presence`、素材时长和按时间稳定排序的 `chunks`。每块包含
`transcript_chunk_id`、`start_ms`、`end_ms`、非空 `text`，可选真实
`char_spans_ms`。

转写文本保持忠实，不做字幕优化或语气词删除。`speech_presence=absent` 时块为空；
`present` 时至少一个合法块。验证时从 JSON 重新渲染并逐字节比较
`transcript.srt`。

### 11.5 `plan.json`

至少包含 schema、`run_id`、`plan_id`、`result_kind`、候选数、发布数和候选数组。
每个候选包含：

- `candidate_id`、初始和最终素材时间范围、边界补救事实。
- `transcript_chunk_ids` 引用。
- 完整主题评审：主题、完整度、学习价值、传播价值、发布就绪分数、标题、摘要、
  关键词、人工复核标记、淘汰原因和边界建议。
- 判别联合 `selection`：
  `published + short_video_id`，或
  `rejected + reason_code + 人工复核信息`。

不得维护重复的 `selected`、`exports`、`clips` 数组或数组下标引用。

### 11.6 `metadata.json`

至少包含 schema、`run_id`、`result_kind`、`short_videos` 和 `series`。

每条短视频包含 ID、源候选引用、标题、摘要、关键词、最终素材时间范围、时长、
固定媒体路径和 `subtitles.kind: burned_in`。不得包含旁挂字幕路径或降级状态。

同主题系列只由 `series` 拥有；每项含 `series_id`、主题和有序
`short_video_ids`。每条短视频最多属于一个系列；无需创建单元素系列。

### 11.7 结果判别与发布前验证

- `clips`：至少一条短视频；发布候选与短视频一一互引；实际 MP4 集合完全一致。
- `empty`：主题评审完整成功；短视频和系列为空；候选全部未发布；`clips/` 为空。
- 三个业务文档的 `result_kind` 必须一致。

验证顺序：

1. 校验四个 JSON schema、版本、共同 `run_id`、计数和时间不变量。
2. 校验实体 ID 唯一、类型和全部引用。
3. 校验 `clips | empty` 判别联合。
4. 校验安全相对路径、精确文件集合、字节长度和 SHA-256。
5. 用 ffprobe 校验每条 MP4 非空可读、含音视频、时长在认证容差内。
6. 确认无短视频旁挂字幕，并重渲染核对 `transcript.srt`。
7. 全部通过后才进入发布提交点。

交付 capability 链固定为：

```text
DeliveryBuild.build(...)         -> UnverifiedDelivery
DeliveryVerification.verify(...) -> VerifiedDelivery
Publication.publish(...)         -> PublishedDelivery
```

`DeliveryVerification` 不复用构建器的判断代码，不修复交付物。
`Publication` 不理解业务 schema，只接受绑定本次运行、受管目录和验证快照的
`VerifiedDelivery`。

## 12. 运行诊断、错误和终端

### 12.1 三层观测

```text
work/runs/<run_id>/
├── events.jsonl
└── run.json

delivery/
└── manifest.json
```

- `events.jsonl`：只追加的结构化事件日志，是时间线事实来源。
- `run.json`：`run_manifest.v1` 运行诊断清单，正常终止时原子写入。
- `delivery/manifest.json`：只在成功发布时存在的长期审计摘要。
- 强制终止允许只有明确不完整的事件日志；缺少合法终态不得推断为失败。

### 12.2 事件信封

每行固定包含 schema、UTC 时间、严格递增 `sequence`、`run_id`、level、
稳定 `event_code`、stage、module、可选 operation 关系、安全消息和严格
`attributes` schema。

- 持续时间由单调时钟测量，使用整数毫秒。
- 每个进入的阶段恰有一对 `stage.started` / `stage.completed`。
- 空工作阶段是 `succeeded + work_item_count: 0`，不是 `skipped`。
- 有成本、并发或独立失败的工作使用 operation 事件。
- 业务模块只能通过绑定运行、阶段和模块的 `DiagnosticScope` 发事件；
  不得直接写日志、打印或提交任意字典。

重试统计严格区分：

- `transport_retry`：同一语义请求的额外传输尝试。
- `semantic_retry`：业务输出校验失败后的额外生成。
- `coverage_recovery`：语音覆盖缺口补转，不是重试。

### 12.3 `run.json`

顶层固定包含：

- `identity`
- `lifecycle`
- `source`
- `environment`
- `configuration`
- `stages`
- `operations`
- `retries_and_recovery`
- `cache`
- `external_services`
- `delivery`
- `notices`
- `errors`
- `event_log`

未知或未执行事实使用明确状态，不用空字符串或零值冒充。

### 12.4 公共错误

错误码格式为 `<namespace>.<condition>`，供应商无关、稳定且封闭。每个码固定映射：

- 粗粒度类别与退出码。
- 默认安全消息。
- `retryable_in_new_run`。
- 稳定 `operator_action`。
- 允许的脱敏诊断字段。

本地缺少凭据是 `config.credential_missing`、退出 `2`，且不得发远程请求；
已配置凭据被供应商拒绝是业务模块的 `authentication_failed`、退出 `30`。

终态错误对象包含不透明 ID、错误码、类别、阶段、模块、可选 operation、
对应事件序号、安全消息、可重试性、人工动作和受 schema 约束的诊断。

普通并发中第一个非取消确定性失败是主错误；其余为关联错误。聚合预检按
“配置优先于环境、同类按注册表顺序”稳定排序。中断本身不创建主错误；
清理失败只形成关联错误和 `recovery_incomplete`。

### 12.5 诊断耐久性

- 初始化诊断或首事件失败：环境预检失败，退出 `10`。
- 发布提交点前无法追加必需事件或写终态清单：本地处理失败，退出 `40`。
- 发布提交点后诊断收尾失败：不得撤销成功交付；stderr 提示诊断可能不完整。
- 关键边界事件立即 `fdatasync`；普通事件最多每 100 条或 1 秒同步。
- `run.json` 写入前同步事件日志，再经临时文件、文件同步、原子重命名和父目录同步。

### 12.6 终端与 skill 调度器

- stdout 输出正常进度和成功摘要；stderr 输出警告、错误和诊断紧急消息。
- 安静模式仍显示终态、`run_id` 和诊断位置。
- 持久化审计不受终端详细程度影响。
- skill 调度器读取运行诊断清单和交付运行清单，不解析终端文本。

## 13. 数据安全、披露与留存

### 13.1 数据分级

| 等级 | 内容 | 处理要求 |
| --- | --- | --- |
| 机密凭据 | API Key、Authorization、环境变量值 | 只在内存和供应商请求中使用；绝不持久化或记录 |
| 敏感内容 | 视频、音频、识别分片、转写、上下文、候选、模型输入输出、字幕、评审和标准交付物 | 只在完成业务所需的受管位置与供应商操作中出现 |
| 受限标识元数据 | 文件名、绝对路径、摘要、缓存键、远端请求标识 | 只进入明确批准字段 |
| 可披露运行事实 | 供应商/模型、版本、状态、错误码、聚合耗时/重试/缓存/请求 | 可按白名单进入诊断与审计 |

### 13.2 最小供应商外发

- StepAudio 只接收当前识别分片音频和协议参数。
- StepFun 主题评审只接收当前候选所需转写、必要课程上下文字段和业务约束。
- StepFun 字幕优化只接收当前字幕子窗口转写和版本化固定指令。
- 确定性 Adapter 零外部传输。

不得向供应商发送本地路径、文件名、`run_id`、缓存标识或与当前操作无关的数据。

### 13.3 三层披露

1. 预检后、首次远程请求前，终端非交互列出供应商、用途和计划数据类别。
2. `run.json.external_services` 记录选中供应商、模型、endpoint origin、用途、
   允许数据类别、实际请求统计和零请求事实；事件逐次记录脱敏调用事实。
3. 成功交付只投影实际联系过的供应商、模型、用途、数据类别和聚合请求数。

不得记录正文、完整 URL、请求参数、代理 URL、代理凭据或密钥。应用不承诺
供应商侧零留存、删除期限、禁止训练，也不调用供应商删除接口。

### 13.4 路径、HTTPS 和日志

- 受管子树内只使用受约束相对路径；禁止特殊文件、符号链接和路径越界。
- 清理、覆盖和回滚前重新验证 workspace 标记与目标归属，遍历不跟随符号链接。
- 外部文本不参与文件名。
- 供应商端点强制 HTTPS，不得降级为 HTTP。
- 使用标准证书链与主机名校验，信任系统 CA，遵循标准代理环境变量。
- 观测采用安全字段白名单，不采用记录任意内容后正则打码。
- 未知异常仅记录规范化包内位置；媒体 stderr 仅记录稳定原因或长度与 SHA-256。

### 13.5 留存

- 有效缓存无 TTL，保留到显式 `cache clear`。
- 脱敏运行诊断包不自动过期，也不由 `cache clear` 删除。
- 标准交付物和上一版备份由操作员管理。
- 清理只承诺文件系统逻辑删除和目录项持久化，不承诺 SSD、快照、备份或系统日志
  中的安全擦除。
- 本版不设计应用层加密；设备、磁盘、交换空间、快照、备份和供应商账户策略由
  操作员负责。

## 14. 生产验收矩阵

### 14.1 候选身份与通用规则

候选由 commit SHA 与应用 wheel SHA-256 共同标识。wheel 只构建一次，所有自动
和人工门禁使用同一构建物。代码或构建物变化产生新候选，既有证据全部失效。

每个 PR、进入受保护 `main` 的提交和最终候选都执行完整无密钥自动门禁，不按
改动路径裁剪。每个门禁必须有非零用例，且零失败、零错误、零跳过、零 `xfail`、
零 `xpass`；禁止测试级自动重试。

测试 surface 是模块接口和稳定 seam。纯算法与 schema 可细粒度测试；不设置覆盖率
百分比门槛，也不要求测试每个内部辅助函数。

### 14.2 无密钥自动门禁

| 层级 | 必须覆盖 | 通过条件 |
| --- | --- | --- |
| 单元与 schema | 算法、规范化、配置、上下文、时间、标识、缓存、诊断、错误注册表、交付 schema | 正常、边界、非法与确定性性质通过；严格拒绝未知字段、非法枚举、非有限数、越界路径和悬空引用 |
| 模块接口 | 所有深模块 | 使用确定性 Adapter 覆盖成功、空输入、有效空结果、缓存、取消、并发顺序独立、类型化失败和接口不变量 |
| Adapter 契约 | StepAudio/确定性语音识别、StepFun/确定性文本模型、文件系统/内存缓存、持久化/收集诊断 | 同类 Adapter 共用契约；生产 Adapter 另覆盖协议、重试、限流、取消、脱敏和错误翻译 |
| 故障注入 | 错误、缓存、诊断、交付、发布、信号、残留与恢复 | 每个公共稳定故障贯通到终态、退出码、`run.json`、清理/回滚和无新交付 |
| 安装 | 候选 wheel、锁文件、snapshot、系统能力 | 全新认证环境从本地构建物安装，依赖和能力预检全部通过 |
| CLI 黑盒 | 已安装命令、真实媒体工具、合成 MP4、确定性 Adapter | 仓库外运行，覆盖成功、有效空结果、失败、覆盖、回滚、缓存、清理和信号 |
| 独立交付校验 | 已发布 `delivery/` | 不导入构建或内建验证实现；独立验证 schema、文件、摘要、引用、路径与 MP4 |

自动测试执行阶段禁止外部网络，只保留回环。生产 Adapter 使用伪传输或回环假服务；
确定性 Adapter 任何网络访问、生产 Adapter 访问非回环地址都使门禁失败。

### 14.3 故障注入硬要求

- 公共错误码注册表的每种稳定故障至少一个贯通用例。
- 缓存写、同步、重命名、父目录同步、隔离和单航班等待注入权限、磁盘满、
  截断、中断与取消。
- 诊断追加、关键同步、终态清单发布和提交点后诊断收尾逐项注入。
- 交付构建、验证、备份、目录切换、回滚和清理逐点验证全有或全无。
- 对真实 CLI 子进程发送 `SIGINT`/`SIGTERM`，覆盖首次、再次信号和提交点临界区。
- 所有故障同时断言严格脱敏、敏感临时文件清理、有效缓存和既有交付不受损。

### 14.4 发布前真实冷运行

在认证主机上：

1. 从锁定 snapshot、wheel 和哈希锁文件安装候选。
2. 执行严格预检并保存环境事实。
3. 使用版本化、记录 SHA-256 的真实中文直播素材、固定配置和课程上下文。
4. 使用新建、有效标记、空缓存 workspace 运行 `video-auto-editor live`。
5. 必须实际联系 StepAudio 和 StepFun，并至少产出一条发布就绪短视频。
6. 完成语音识别、主题评审、字幕优化、媒体导出、字幕烧录、内建验证、
   原子发布和独立校验。
7. 操作员逐条观看全部短视频并核对素材与忠实转写，确认主题完整、首尾自然、
   音画正常、字幕忠实可读、标题摘要无虚构、排除内容未泄漏。

### 14.5 发布前缓存复跑

冷运行通过后，在同一 workspace、相同素材、配置和上下文下立即以
`--overwrite` 复跑：

- 整场转写、主题评审、字幕优化全部命中，StepAudio 和 StepFun 请求数严格为零。
- 仍执行本地导出、字幕烧录、验证和原子发布。
- 冷运行交付成为 `delivery.previous/`，新交付通过独立验证。
- 除运行 ID、业务 UUID、时间和统计外，两次业务结果语义等价。
- 任一远程请求、损坏、未命中、上一版丢失或结果漂移都使门禁失败。

### 14.6 密钥和证据

- PR、分支、`main` 自动 CI 不接收真实供应商密钥。
- 真实门禁由发布操作员在受控主机上启动，使用独立限额凭据。
- 凭据从主机密钥存储临时注入环境，不进入参数、JSON、历史、日志或附件。
- 真实素材、交付物和运行诊断包不上传 GitHub。
- 公开证据只保存摘要、供应商/模型、请求与缓存统计、脱敏指纹、检查项和结论。

### 14.7 生产 tag 与发布记录

- 全部门禁通过后才创建维护者签名的 annotated tag：
  `vMAJOR.MINOR.PATCH`。
- tag、应用版本和 wheel 元数据版本一致。
- tag message 记录 commit SHA、wheel SHA-256 和 `release-evidence.json` SHA-256。
- GitHub Release 附同一 wheel、哈希锁文件、安装/依赖清单、中文发布说明和
  `release-evidence.json`。
- tag 与证据不可改写或移动；变更或补验使用新版本。

`release-evidence.json` 至少包含候选身份、锁文件、snapshot、实际环境版本与能力、
自动门禁链接与用例统计、网络隔离、真实输入摘要、两次 `run_id`、终态、交付摘要、
供应商请求、缓存统计、独立验证、人工复核、允许复跑的失败记录、已知限制和
完整依赖清单。

任一硬门禁失败阻止生产 tag，不允许备注、允许失败或手工豁免。只有已明确归类的
供应商短暂故障或认证主机基础设施故障可以对同一不可变候选复跑。

## 15. ADR 处置清单

### 15.1 迁移原则

- 既有 ADR 正文保持为不可改写历史，只新增统一元数据。
- 原 ADR 含有会指导错误生产行为的条款时，即使部分原则仍有效，也整体标为
  `Superseded`，由新 ADR 完整重述。
- 只有不改变原契约的补充才标为 `Amended`。
- 旧 ADR 状态、新 ADR、双向链接、本文引用和相关领域词汇必须在同一个文档变更中
  完成；完成前不得开始生产架构迁移。

统一元数据：

```markdown
Status: Accepted | Amended | Superseded
Date: YYYY-MM-DD
Amended by: [ADR 标题](...)
Superseded by: [ADR 标题](...)
```

新 ADR 反向记录 `Amends` 或 `Supersedes`。链接必须可达、对称，不得存在无替代
目标的 `Superseded`。

### 15.2 既有 ADR 状态

| 既有 ADR | 状态 | 处置 |
| --- | --- | --- |
| [默认导出全部发布就绪短视频](adr/0002-export-all-publish-ready-live-clips-by-default.md) | Accepted | 保持 |
| [按相邻候选批次进行主题评审](adr/0006-review-topic-candidates-in-neighboring-batches.md) | Accepted | 保持 |
| [默认使用 StepFun Chat 进行主题评审](adr/0005-use-stepfun-chat-as-default-topic-review-model.md) | Amended | 由共享文本模型端口 ADR 补充 |
| [CLI 负责视频处理，skill 负责上层调度](adr/0008-keep-video-processing-in-cli-and-use-skill-for-orchestration.md) | Amended | 由组合根与深模块 ADR 补充 |
| [处理缓存必须包含影响结果的输入摘要](adr/0009-cache-live-processing-results-with-result-affecting-inputs.md) | Amended | 由版本化内容寻址缓存 ADR 补充 |
| [使用混合策略判定直播课主题片段](adr/0001-use-hybrid-topic-detection-for-live-clips.md) | Superseded | 由共享文本模型端口与分层主题评审 ADR 替代 |
| [默认使用 stepaudio-2.5-asr 进行语音识别](adr/0003-use-stepaudio-as-default-transcription-provider.md) | Superseded | 由语音识别模块与覆盖账本 ADR 替代 |
| [长直播默认分片识别后合并时间戳](adr/0004-transcribe-long-live-videos-in-audio-shards.md) | Superseded | 由语音识别模块与覆盖账本 ADR 替代 |
| [语音识别失败中止，主题评审失败只输出未评审方案](adr/0007-stop-on-transcription-failure-but-continue-planning-without-topic-review.md) | Superseded | 由全有或全无状态机 ADR 替代 |
| [短视频默认烧录字幕并过滤纯语气词](adr/0010-burn-subtitles-and-filter-filler-words-for-clips.md) | Superseded | 由强制字幕优化与烧录 ADR 替代 |
| [用大模型在子序列约束下优化短视频字幕](adr/0011-optimize-clip-subtitles-with-llm-under-subsequence-constraint.md) | Superseded | 由强制字幕优化与烧录 ADR 替代 |
| [分段字幕优化窗口以提升子序列通过率](adr/0012-segment-subtitle-optimization-window-to-improve-subsequence-pass-rate.md) | Superseded | 由强制字幕优化与烧录 ADR 替代 |
| [分片重叠 + 覆盖度兜底补转修复 ASR 尾部丢失](adr/0013-overlap-asr-shards-and-backfill-tail-coverage.md) | Superseded | 由语音识别模块与覆盖账本 ADR 替代 |

结果：2 份 Accepted、3 份 Amended、8 份 Superseded。

### 15.3 新生产级 ADR

按以下标题和范围创建十二份 ADR，不再讨论替代方案：

1. [认证 Linux 原生生产环境与可复现安装](adr/0014-certify-linux-native-production-environment-and-reproducible-installation.md)。
2. [收敛 live-only 公共接口与受管 workspace](adr/0015-converge-live-only-public-interface-and-managed-workspace.md)。
3. [采用全有或全无的直播拆条运行状态机](adr/0016-adopt-all-or-nothing-live-run-state-machine.md)。
4. [采用单一组合根与业务能力深模块](adr/0017-adopt-single-composition-root-and-deep-business-capability-modules.md)。
5. [采用供应商无感知的语音识别模块与覆盖账本](adr/0018-adopt-provider-agnostic-speech-recognition-and-coverage-ledger.md)。
6. [采用共享文本模型端口与分层主题评审](adr/0019-adopt-shared-text-model-port-and-layered-topic-review.md)。
7. [将字幕优化与烧录设为强制生产路径](adr/0020-make-subtitle-optimization-and-burning-mandatory.md)。
8. [采用版本化内容寻址处理缓存](adr/0021-adopt-versioned-content-addressed-processing-cache.md)。
9. [采用版本化标准交付物与验证后原子发布](adr/0022-adopt-versioned-standard-delivery-and-atomic-publication-after-verification.md)。
10. [采用结构化运行诊断与稳定错误分类](adr/0023-adopt-structured-run-diagnostics-and-stable-error-classification.md)。
11. [统一敏感数据、供应商披露与本地留存契约](adr/0024-unify-sensitive-data-provider-disclosure-and-local-retention-contract.md)。
12. [以分层验收证据批准生产版本](adr/0025-approve-production-releases-with-layered-acceptance-evidence.md)。

新 ADR 只解释权衡与理由，当前契约引用本文相应章节，不复制整份规格。

## 16. 可执行实施分解

以下阶段是依赖有向无环图。每阶段只有满足“完成条件”后才能解锁依赖它的阶段。
实现可以拆成多个 PR，但不得跳过依赖或留下两套当前公共契约。

### 阶段 A：文档与测试治理前置

依赖：无。

工作：

1. 按第 15 节在同一变更中创建十二份 ADR、更新十三份旧 ADR 元数据和双向链接。
2. 修正 `CONTEXT.md` 中仍描述未评审导出、字幕降级、旧缓存范围或旧诊断路径的
   过期条款，使其与本文术语和业务语义一致。
3. 修订仓库测试治理规则：
   允许在“已批准决定明确废止旧契约，且替代新契约测试已经建立”的前提下，
   删除或重写冲突的旧测试；继续禁止为掩盖生产缺陷而放宽断言、跳过测试、
   篡改 Mock/Fixture 或增加测试级重试。
4. 增加文档校验：ADR 状态、链接可达、双向关系、无悬空 Superseded、
   本文内部链接和术语扫描。

完成条件：

- 旧 ADR 分类精确为 2/3/8，十二份新 ADR 均为 Accepted。
- 文档校验自动通过。
- 测试治理冲突消除，且没有修改生产代码。

### 阶段 B：不可变契约内核与基础深模块

依赖：阶段 A。

工作：

1. 实现 `runtime/errors.py`：封闭错误注册表、错误对象、退出码和人工动作映射。
2. 实现 `runtime/cancellation.py`：根取消、令牌、首次/再次信号和 10 秒清理窗口。
3. 实现 `runtime/identity.py`：类型化 UUID 和单运行稳定规则。
4. 实现交付 capability 类型。
5. 实现 `Configuration`、`Workspace`、`RunDiagnostics` 和共享缓存仓库。
6. 为文件系统/内存缓存、持久化/测试诊断建立共用契约测试。

完成条件：

- 配置、课程上下文、路径、权限、锁、诊断 schema、错误映射、缓存身份/envelope、
  单航班、损坏隔离和原子写入测试全部通过。
- 所有跨模块值不可变；受管路径只能由 capability 取得。
- 故障注入证明缓存或诊断基础设施失败不会被误当普通未命中。

### 阶段 C：确定性纵向状态机

依赖：阶段 B。

工作：

1. 实现 `LiveApplication.execute()` 和固定状态机。
2. 使用最小测试业务模块和确定性 Adapter 接通从初始化到发布提交点的纵向流程。
3. 实现主错误、关联错误、聚合预检、阶段事件、清理和发布点信号语义。
4. 实现 `cache clear` 管理应用的锁与安全清理，不接入生产 CLI。

完成条件：

- 黑盒覆盖短视频成功、有效空结果、各阶段失败、`SIGINT`、`SIGTERM`、
  诊断失败、清理失败和提交点后诊断收尾失败。
- `LiveApplication` 不知道分片、批次、子窗口或单条导出。
- 失败与中断不产生新标准交付物。

### 阶段 D：本地业务事实

依赖：阶段 B；接入依赖阶段 C。

工作：

1. 实现 `SourceAnalysis` 的 ffprobe 素材验证与不可变结果。
2. 实现 `ClipPlanning.prepare()` 的候选与初始边界。
3. 实现 `ClipPlanning.finalize()` 的评审归并、边界补救、发布就绪判断、
   `max_clips`、排除内容和同主题系列。
4. 建立算法、ID、排序、引用和有效空结果测试。

完成条件：

- 同输入与不同并发完成顺序产生相同业务结果。
- `prepare/finalize` 事实所有权清晰，无重复选择投影。
- 默认导出全部发布就绪短视频，零候选形成有效空结果。

### 阶段 E：语音识别

依赖：阶段 B、阶段 D 的素材描述；接入依赖阶段 C。

工作：

1. 建立语音识别接口及共享 Adapter 契约套件。
2. 实现标准化 PCM、核心区/上下文请求规划、独立语音证据、覆盖账本、
   重叠归并和有界定向补转。
3. 实现整场与识别分片缓存身份和 payload schema。
4. 实现 StepAudio 与确定性 Adapter、媒体子进程取消、类型化错误和执行事实。
5. 接入 `transcription` 阶段。

完成条件：

- 覆盖精确重复、标点差异、真实重复话语、语义冲突、乱序完成、15 秒和约
  165 秒尾部截断、中间缺失、静音、空响应、逐字时间异常、无进展、预算耗尽和中断。
- 残余语音缺口或冲突不返回、不缓存部分转写。
- StepAudio 与确定性 Adapter 共用契约；编排测试不 Mock Adapter 内部函数。

### 阶段 F：文本模型、主题评审与字幕优化

依赖：阶段 B、阶段 D；真实接入还依赖阶段 E。

工作：

1. 建立文本模型端口及 StepFun/确定性 Adapter 共享契约。
2. 实现传输重试、取消、并发限制、类型化失败和脱敏观测。
3. 实现 `TopicReview` 的批次、提示、JSON 校验、语义重试和缓存。
4. 实现 `SubtitleOptimization` 的字符预算分组、抽取式提示、子序列校验、
   时间对齐、语义重试和缓存。
5. 接入 `topic_review`，并把字幕优化作为交付构建前的强依赖。

完成条件：

- 端口、两个业务模块分别通过公开接口黑盒测试。
- 空输入零请求；任一工作项失败不返回部分阶段结果。
- 缓存命中零远程请求；提示或校验版本变化选择性失效。
- 不存在关闭、旁挂字幕或规则字幕降级成功路径。

阶段 E 与阶段 F 的内部实现可并行；只有两者完成后才可解锁真实交付链。

### 阶段 G：交付、验证与发布

依赖：阶段 C、D、E、F。

工作：

1. 实现四份严格 JSON schema、忠实 SRT 确定性渲染和人类报告投影。
2. 实现 `DeliveryBuild` 及真实 FFmpeg 字幕烧录和 MP4 导出。
3. 实现不复用构建判断代码的 `DeliveryVerification`。
4. 实现 `Publication` 的上一版备份、目录提交、父目录同步和回滚。
5. 接入 capability 链和应用状态机。

完成条件：

- 完整交付与有效空结果通过同一验证流程。
- 文件集合、摘要、引用、路径、MP4 和 SRT 全部独立验证。
- 每个发布故障点均证明既有交付可恢复且无半成品可见。
- 提交点前后中断语义符合第 7 节。

### 阶段 H：生产组合根、CLI 与旧架构删除

依赖：阶段 G。

工作：

1. 实现 `composition.py` 固定生产注册表和测试组合根。
2. 接入 StepAudio、StepFun、文件系统缓存、持久化诊断和真实媒体能力。
3. 实现第 5 节的 `live` 与 `cache clear` 命令。
4. 一次性删除 `single`、`batch`、旧参数、旧 schema、旧降级路径、
   Whisper 生产依赖、旧编排和转发导入。
5. 在替代新契约测试已建立后，删除或重写只验证已废止契约的旧测试。
6. 更新打包元数据、控制台入口、运行和构建锁文件。

完成条件：

- 包布局与第 8 节一致，无全局依赖容器、万能上下文或兼容 shim。
- 生产 JSON 不能选择确定性 Adapter。
- 仓库内不再存在旧命令、未评审导出、字幕降级或旧交付格式的可达生产路径。
- 已安装控制台命令从仓库外目录运行，不回退到源码或 `python -m`。

### 阶段 I：自动生产门禁

依赖：阶段 H。

工作：

1. 构建一次候选 wheel 和本地 wheelhouse。
2. 建立单元/schema、模块接口、Adapter 契约和全量故障注入门禁。
3. 建立 Ubuntu snapshot 安装、严格预检、CLI 黑盒和独立交付校验。
4. 测试执行阶段启用外部网络隔离。
5. 生成机器可读门禁证据和用例统计。

完成条件：

- 同一候选 wheel 通过第 14.2 节全部门禁。
- 非零用例、零失败/错误/跳过/xfail/xpass。
- 网络隔离和故障注入证据完整。

### 阶段 J：真实门禁与生产发布

依赖：阶段 I。

工作：

1. 在认证主机执行真实冷运行及逐条人工内容复核。
2. 在同一 workspace 执行零供应商请求缓存复跑。
3. 汇总不可变 `release-evidence.json` 和中文发布说明。
4. 核验版本、commit、wheel、锁文件、安装清单和证据摘要。
5. 创建签名 annotated tag 和绑定 GitHub Release。

完成条件：

- 冷运行与缓存复跑连续通过，独立交付验证和人工复核通过。
- 所有失败尝试和允许复跑原因已记录。
- tag、wheel、版本与证据完全一致，发布附件即实际验收构建物。

## 17. 实施完成定义

全部以下条件同时满足，生产就绪迁移才完成：

- 公共面只有 `live`、`cache clear`、`--help`、`--version`。
- 十二个业务深模块按第 8 节组合，内部 seam 不泄漏给应用。
- 直播拆条运行严格遵守全有或全无状态机与退出码。
- 四类处理缓存满足身份、版本、单航班、损坏隔离和耐久写入契约。
- 标准交付物严格满足固定目录、schema、稳定标识、验证和原子发布契约。
- 运行诊断、公共错误、供应商披露和敏感数据处理通过故障与脱敏验收。
- 旧架构、旧兼容路径、旧 schema、Whisper 生产依赖和已废止契约测试已删除。
- ADR、领域词汇、本文和生产行为一致。
- 同一候选 wheel 通过自动门禁、真实冷运行、零请求缓存复跑和人工内容复核。
- 已创建签名生产 tag 和不可变发布证据。

本文没有待实施者决定的产品或架构问题。实施过程中若出现本文未覆盖且会改变公共
行为、模块事实所有权、数据外发、失败语义、交付 schema 或发布门禁的新问题，
必须暂停对应阶段并以新的决策流程处理；不得把临时选择隐藏在实现中。
