# Linux 原生生产安装与环境预检基线

## 结论

首个生产认证环境应收敛为 **Ubuntu 24.04 LTS amd64**，而不是笼统承诺所有
Linux x86_64 发行版。生产运行时采用以下基线：

| 项目 | 生产基线 | 启动时判定 |
| --- | --- | --- |
| 操作系统 | Ubuntu 24.04 LTS，`amd64` | 必须为 Linux；机器架构规范化后必须为 `x86_64`/`amd64`；首期只有 Ubuntu 24.04 属于认证环境 |
| Python | CPython `>=3.12.3,<3.13` | 必须由版本化虚拟环境运行，解释器与 CLI 必须来自同一虚拟环境 |
| FFmpeg | `>=6.1,<7`，参考包为 Ubuntu `7:6.1.1-3ubuntu5` | `ffmpeg` 与 `ffprobe` 必须报告相同的上游版本，且通过实际能力烟测 |
| ffprobe | 与 FFmpeg 同包、同上游版本 | 必须成功输出 JSON 格式的媒体时长和流信息 |
| fontconfig | 参考包 `2.15.0-1.1ubuntu2` | `fc-list` 必须找到配置字体，`fc-match` 必须解析到同一字体家族 |
| 中文字体 | `fonts-noto-cjk`，参考包 `1:20230817+repack1-3`；默认家族 `Noto Sans CJK SC` | 必须找到该家族，且 FFmpeg 中文 SRT 烧录烟测成功 |

这里的 FFmpeg 与字体“最低版本”是**首个认证发行版基线**，不是声称更早版本
一定不具备功能。FFmpeg 官方资料明确表明 `subtitles` 滤镜依赖
`--enable-libass`，`libx264` 也依赖构建时启用外部库；因此只比较版本号无法证明
当前二进制具备直播拆条所需能力。生产门禁必须同时检查版本范围和实际能力。

选择 Python 3.12 而不是继续使用仓库当前的 3.10 下限，原因是 Python 官方支持表
显示 3.10 将于 2026 年 10 月停止支持，而 3.12 支持到 2028 年 10 月；Ubuntu
24.04 LTS 原生提供 Python 3.12.3，并获得到 2029 年 5 月的标准安全维护。该选择
不需要 PPA，也能让安装来源保持单一。

## 仓库现状与依赖边界

当前 [`pyproject.toml`](https://github.com/dulltackle/long-video-autocut/blob/b1014182e5233614763e43dbfee3d94f1d640ed2/pyproject.toml)
存在三处不可复现因素：

1. Python 只声明 `>=3.10`，没有认证的 minor 上限；
2. 构建后端写成无上限的 `setuptools>=61`；
3. 唯一第三方运行依赖 `openai-whisper` 没有版本或哈希。

本次 Wayfinder 地图已经决定移除 `single`、`batch` 和 Whisper 生产路径。当前
`live` 的 StepAudio、主题评审和字幕优化请求均由 Python 标准库实现；完成该移除
后，生产运行时应当没有 PyPI 第三方依赖。FFmpeg/ffprobe 和字体属于系统依赖，
不能写进 Python 依赖锁。

生产依赖应分为三层：

- **系统依赖**：由固定 Ubuntu archive snapshot 解析
  `python3.12`、`python3.12-venv`、`ffmpeg`、`fontconfig`、
  `fonts-noto-cjk` 和 `ca-certificates`，安装后把实际 deb 版本写入发布记录。
- **Python 运行依赖**：维护 `requirements-runtime.lock`。当前目标状态为空；
  未来一旦增加依赖，所有直接与传递依赖都必须使用 `==` 精确固定并附 SHA-256，
  安装时强制 `--require-hashes --only-binary=:all: --no-deps`。
- **应用本体**：发布预构建 wheel 和独立 SHA-256 校验文件；生产机只安装已验证
  wheel，不从 Git checkout 构建，也不执行 editable install。

pip 官方将精确固定、哈希校验和 wheelhouse 都列为可重复安装手段，并说明
`--require-hashes` 要求每个依赖都固定且带哈希；安装本地项目时应使用 pip
`--no-deps`，避免遗漏的依赖被隐式下载。生产机安装预构建 wheel 后，当前
`setuptools>=61` 的开放构建依赖就不会进入生产解析过程。构建机仍需单独维护
带版本和哈希的 `requirements-build.lock`。

## 系统包锁定策略

安装脚本必须接受发布清单中的 `apt_snapshot_id`，使用 Ubuntu Snapshot Service
解析系统包，而不是对实时 archive 执行裸 `apt install`。Ubuntu 官方说明该服务
用于可复现部署，Ubuntu 24.04 起默认支持 `--snapshot`；快照至少保留两年。

发布流程应当：

1. 选择一个已经完成真实端到端验收的 UTC snapshot ID；
2. 用该 snapshot 安装系统包并记录 `dpkg-query` 返回的完整版本；
3. 把 snapshot ID、系统包版本、应用 wheel 哈希和 Python 锁文件哈希写入同一个
   发布清单；
4. 每次生产 tag 使用自己的发布清单，不能使用“latest”或安装时重新生成锁；
5. 安全更新通过新 snapshot、新验收和新 tag 发布，不能让生产机器静默漂移。

快照服务只承诺至少两年留存，因此它不是永久归档。若要求两年以上重建同一环境，
只有两个可验证选项：维护组织自己的 deb 镜像，或随发布归档完整 deb 安装包及
仓库签名材料。这一长期归档选择超出了当前票据能够从官方承诺中锁定的范围。

## 安装脚本契约

后续实施应提供非交互、幂等的 `scripts/install-production.sh`。脚本输入必须包含：

- 应用 wheel 的路径；
- wheel 的预期 SHA-256；
- `apt_snapshot_id`；
- 安装前缀（默认可为 `/opt/long-video-autocut/<version>`）。

脚本按以下顺序执行，任一步失败都必须非零退出：

1. 验证 `/etc/os-release` 为 Ubuntu 24.04，`dpkg --print-architecture` 为
   `amd64`，并拒绝 root 之外的系统包安装阶段。
2. 使用同一 snapshot ID 更新索引并安装
   `python3.12 python3.12-venv ffmpeg fontconfig fonts-noto-cjk ca-certificates`；
   禁止 PPA 和未记录的第三方 apt 源。
3. 用 `python3.12 -m venv` 创建版本化虚拟环境。Python 官方说明 `venv` 使用
   调用它的 Python 版本，因此脚本不能使用未限定的 `python3 -m venv`。
4. 校验 wheel SHA-256；若运行锁文件非空，先用
   `python -m pip install --require-hashes --only-binary=:all: --no-deps
   -r requirements-runtime.lock` 安装；再用
   `python -m pip install --no-deps <wheel>` 安装应用。
5. 通过原子更新的符号链接切换当前版本，避免覆盖正在使用的虚拟环境。
6. 运行严格预检；只有预检通过才把此版本标记为可运行。
7. 输出安装清单，至少包含 OS、架构、snapshot ID、deb 版本、Python 完整版本、
   wheel 名称与哈希、FFmpeg/ffprobe 版本和命中字体文件。

脚本不得执行 `pip install -e .`、`pip install openai-whisper`、无哈希网络安装，
也不得在生产机上从 sdist 构建 wheel。

## 严格启动预检

当前
[`preflight.py`](https://github.com/dulltackle/long-video-autocut/blob/b1014182e5233614763e43dbfee3d94f1d640ed2/video_auto_editor/preflight.py)
只用 `shutil.which` 判断命令是否存在，无法证明版本、编码器、滤镜、字体或真实
可执行性。生产预检必须执行下列探测并输出结构化结果；任一硬检查失败时，CLI
不得进入处理阶段。

### 平台与 Python

- `sys.platform == "linux"`，`platform.machine()` 规范化后为 `x86_64`；
- `/etc/os-release` 是 Ubuntu 24.04；其他 Linux 可以显示“未认证”，但生产模式
  必须拒绝；
- `platform.python_implementation() == "CPython"`；
- Python 版本满足 `>=3.12.3,<3.13`；
- `sys.prefix != sys.base_prefix`，确认位于虚拟环境；
- CLI 可执行文件、`sys.executable` 与已安装 distribution 都位于相同版本前缀；
- 应用版本与发布清单、Git tag 对应版本一致。

### FFmpeg、ffprobe 与字幕能力

仓库媒体代码实际使用 `ffprobe` JSON、`subtitles` 滤镜、`libx264`、原生 AAC 和
MP4 输出，见
[`media.py`](https://github.com/dulltackle/long-video-autocut/blob/b1014182e5233614763e43dbfee3d94f1d640ed2/video_auto_editor/media.py#L11-L56)。
预检必须：

- 解析 `ffmpeg -version` 与 `ffprobe -version`，两者满足 `>=6.1,<7` 且上游版本
  完全一致；
- 验证 `ffmpeg -filters` 包含 `subtitles`；
- 验证 `ffmpeg -encoders` 包含 `libx264` 和 `aac`；
- 验证 FFmpeg 能写 MP4，ffprobe 能以 JSON 读回容器、视频流、音频流和时长；
- 在临时目录生成一秒的测试音视频和包含“中文预检”的 SRT，使用与生产导出相同
  的 `subtitles`、`FontName=Noto Sans CJK SC`、`libx264`、AAC 参数完成一次烧录，
  再用 ffprobe 验证输出；无论成功失败都清理该临时目录。

FFmpeg 官方文档确认 `subtitles` 滤镜使用 libass，并要求构建时启用 libass；
libx264 是单独的编码器包装。因此上述烟测是版本检查的必要补充，不是可选的
集成测试。

### 字体

- `fc-list -q "Noto Sans CJK SC"` 必须返回成功；
- `fc-match` 返回的 family 必须包含 `Noto Sans CJK SC`，不能只接受任意 fallback；
- 命中的字体文件必须存在、可读，并记录到预检结果；
- 字体家族名称来自有效配置副本中的 `subtitle_font`，不能在预检中另写死一份；
- 最终结论以 FFmpeg 中文烧录烟测为准。

Ubuntu 24.04 的 `fonts-noto-cjk` 提供 CJK regular/bold 字体；`fontconfig` 包提供
`fc-list`、`fc-match` 等探测命令。fontconfig 官方手册说明 `fc-list -q` 在没有
匹配字体时返回 1，适合作为机器可读硬检查。

### 文件系统、配置与供应商前置条件

- 输入视频必须为可读普通文件，并可被 ffprobe 解析；
- 工作目录和交付目录父目录必须存在或可创建、可写；
- 采用临时目录后 `os.replace` 的原子发布要求临时目录与最终目录位于同一文件
  系统；预检应比较 `st_dev`，并执行创建、`fsync`、重命名、删除烟测；
- 默认拒绝非空交付目录；`--overwrite` 的备份目标也必须可写且位于预期父目录；
- 配置文件必须完成已有类型、范围和跨字段校验；远程 Adapter 的 base URL 必须
  为 HTTPS；
- 所需密钥环境变量必须存在且非空，但预检结果只能记录变量名和“已设置”，不得
  记录值；
- Python 默认 TLS 信任库必须可加载且包含 CA。供应商鉴权与服务可用性应由
  Adapter 契约检查决定，不应通过预检消耗一次正式识别或评审请求。

由于资源预算已明确排除在本地图范围外，预检只验证路径与原子操作能力，不设置
武断的剩余磁盘、内存或 CPU 阈值。

## 预检输出与发布门禁

预检至少支持 JSON 输出，每项包含稳定检查码、`ok/error`、观测值、期望值和不含
敏感信息的修复提示。成功结果应写入运行清单，不能只打印到终端。建议稳定检查码
包括：

`platform.os`、`platform.arch`、`python.version`、`python.venv`、
`app.version`、`ffmpeg.version`、`ffmpeg.subtitles`、
`ffmpeg.libx264`、`ffmpeg.aac`、`ffprobe.json`、`font.family`、
`media.smoke_test`、`filesystem.atomic_publish`、`tls.ca_store`、
`config.valid` 和 `credentials.present`。

安装与发布验收必须至少覆盖：

1. 从已固定 Ubuntu snapshot 的全新 Ubuntu 24.04 amd64 环境安装成功；
2. 相同发布清单重复安装后，deb 版本、Python 版本、wheel 哈希和预检清单一致；
3. 分别移除 `subtitles` 能力、`libx264`、中文字体、ffprobe、虚拟环境或写权限
   时，预检在发起任何远程请求前失败并返回非零；
4. FFmpeg 与 ffprobe 版本不一致时失败；
5. 修改 wheel 或锁文件后哈希校验失败，且旧版本仍保持可运行。

## 事实来源

- [Python 官方版本状态](https://devguide.python.org/versions/)
- [Python 3.12 `venv` 文档](https://docs.python.org/3.12/tutorial/venv.html)
- [Ubuntu 发布与维护周期](https://ubuntu.com/about/release-cycle)
- [Ubuntu 24.04 `python3.12` 包](https://packages.ubuntu.com/noble/python3.12)
- [Ubuntu 24.04 `ffmpeg` 包](https://packages.ubuntu.com/noble/ffmpeg)
- [Ubuntu 24.04 `libavfilter9` 包及其 libass 依赖](https://packages.ubuntu.com/noble/libavfilter9)
- [Ubuntu 24.04 `libavcodec60` 包及其 libx264 依赖](https://packages.ubuntu.com/noble/libavcodec60)
- [Ubuntu 24.04 `fonts-noto-cjk` 包](https://packages.ubuntu.com/noble/fonts-noto-cjk)
- [Ubuntu 24.04 `fontconfig` 包](https://packages.ubuntu.com/noble/fontconfig)
- [Ubuntu Snapshot Service](https://snapshot.ubuntu.com/)
- [Ubuntu Server 的 Snapshot Service 使用说明](https://documentation.ubuntu.com/server/how-to/software/snapshot-service/)
- [FFmpeg `subtitles` 滤镜文档](https://ffmpeg.org/ffmpeg-filters.html#subtitles-1)
- [FFmpeg 编码器文档](https://ffmpeg.org/ffmpeg-codecs.html)
- [pip 可重复安装文档](https://pip.pypa.io/en/stable/topics/repeatable-installs/)
- [pip 安全安装与哈希检查文档](https://pip.pypa.io/en/stable/topics/secure-installs/)
- [Ubuntu 24.04 `fc-list` 手册](https://manpages.ubuntu.com/manpages/noble/man1/fc-list.1.html)
- [Ubuntu 24.04 `fc-match` 手册](https://manpages.ubuntu.com/manpages/noble/man1/fc-match.1.html)
