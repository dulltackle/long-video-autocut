"""直播拆条环境预检。

预检只做纯函数式判断：输入为可注入的环境探测结果（命令是否存在、环境变量
是否存在、配置字段），输出为结构化的检查项列表与整体 ready 状态。预检不直接
执行 ffmpeg、ffprobe 或发起网络请求，便于在无外部依赖、无网络的环境中测试。
"""

import shutil
import os
from dataclasses import dataclass, field

from video_auto_editor.config import CONFIG


OK = "ok"
WARN = "warn"
ERROR = "error"


@dataclass(frozen=True)
class EnvironmentProbe:
    """可注入的环境探测接口。

    默认实现使用 ``shutil.which`` 与 ``os.environ``，测试可注入 fake 集合，
    避免依赖真实命令与真实环境变量。
    """

    commands: dict | None = None
    env: dict | None = None

    def command_exists(self, name):
        if self.commands is not None:
            return bool(self.commands.get(name))
        return shutil.which(name) is not None

    def env_value(self, name):
        source = self.env if self.env is not None else os.environ
        value = source.get(name)
        if value is None:
            return None
        text = str(value).strip()
        return text or None


@dataclass(frozen=True)
class PreflightCheck:
    """单个预检项结果。"""

    name: str
    status: str
    detail: str
    hint: str = ""


@dataclass(frozen=True)
class PreflightResult:
    """预检汇总结果。"""

    ready: bool
    checks: list = field(default_factory=list)

    @property
    def errors(self):
        return [check for check in self.checks if check.status == ERROR]

    @property
    def warnings(self):
        return [check for check in self.checks if check.status == WARN]

    def to_dict(self):
        return {
            "ready": self.ready,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "hint": check.hint,
                }
                for check in self.checks
            ],
        }


def run_preflight(probe=None, config=None):
    """执行环境预检并返回结构化结果。

    存在任一 ``error`` 时 ``ready=False``；只有 ``warn`` 时 ``ready=True`` 但
    保留警告，对应未评审降级路径。
    """
    probe = probe or EnvironmentProbe()
    config = config or CONFIG

    checks = [
        _check_cli(probe),
        _check_command(probe, "ffmpeg", "ffmpeg"),
        _check_command(probe, "ffprobe", "ffprobe"),
    ]
    checks.append(_check_stepfun_key(probe, config))
    checks.append(_check_asr_provider(probe, config))
    checks.append(_check_topic_review(probe, config))

    ready = all(check.status != ERROR for check in checks)
    return PreflightResult(ready=ready, checks=checks)


def _check_cli(probe):
    if probe.command_exists("video-auto-editor"):
        return PreflightCheck(
            name="video-auto-editor",
            status=OK,
            detail="video-auto-editor CLI 可调用。",
        )
    return PreflightCheck(
        name="video-auto-editor",
        status=ERROR,
        detail="未找到 video-auto-editor 命令。",
        hint="在仓库根目录执行 `pip install -e .` 安装 CLI，或改用 `python -m video_auto_editor` 调用。",
    )


def _check_command(probe, name, hint_command):
    if probe.command_exists(name):
        return PreflightCheck(
            name=name,
            status=OK,
            detail=f"{name} 可执行。",
        )
    return PreflightCheck(
        name=name,
        status=ERROR,
        detail=f"未找到 {name}，无法处理音视频。",
        hint=f"安装 ffmpeg 套件（含 {hint_command}），例如 `sudo apt install ffmpeg` 或 `brew install ffmpeg`。",
    )


def _stepfun_key_env(config):
    return config.get("stepfun_api_key_env", "STEPFUN_API_KEY")


def _check_stepfun_key(probe, config):
    env_name = _stepfun_key_env(config)
    if probe.env_value(env_name) is not None:
        return PreflightCheck(
            name=env_name,
            status=OK,
            detail=f"已设置 {env_name}。",
        )
    return PreflightCheck(
        name=env_name,
        status=ERROR,
        detail=f"未设置 {env_name}，默认 ASR 与默认主题评审都依赖它。",
        hint=f"导出环境变量：`export {env_name}=sk-...`。",
    )


def _check_asr_provider(probe, config):
    provider = config.get("asr_provider", "stepaudio")
    if provider == "stepaudio":
        env_name = config.get("stepfun_api_key_env", "STEPFUN_API_KEY")
        if probe.env_value(env_name) is None:
            return PreflightCheck(
                name="asr_provider",
                status=ERROR,
                detail=f"ASR provider 为 stepaudio，但未设置 {env_name}。",
                hint=f"导出 `{env_name}` 或将 `asr_provider` 切换为 whisper。",
            )
        return PreflightCheck(
            name="asr_provider",
            status=OK,
            detail="ASR provider 为 stepaudio，凭据已就绪。",
        )
    if provider == "whisper":
        return PreflightCheck(
            name="asr_provider",
            status=WARN,
            detail="ASR provider 为 whisper，依赖本地 whisper 环境而非 StepAudio。",
            hint="确认本地已安装 whisper 及其模型；否则切回 stepaudio。",
        )
    return PreflightCheck(
        name="asr_provider",
        status=ERROR,
        detail=f"未知 ASR provider：{provider}。",
        hint="将 `asr_provider` 设为 stepaudio 或 whisper。",
    )


def _check_topic_review(probe, config):
    if not config.get("topic_review_enabled", True):
        return PreflightCheck(
            name="topic_review",
            status=WARN,
            detail="主题评审已关闭，将走未评审降级路径，默认不导出发布就绪片段。",
            hint="如需发布就绪评审，启用 `topic_review_enabled` 并配置评审模型；或运行时加 `--allow-unreviewed-export` 兼容导出。",
        )

    provider = config.get("topic_review_provider")
    model = config.get("topic_review_model")
    if not provider or not model:
        return PreflightCheck(
            name="topic_review",
            status=ERROR,
            detail="主题评审已启用但 provider 或 model 配置缺失。",
            hint="补全 `topic_review_provider` 与 `topic_review_model` 配置。",
        )

    env_name = config.get("topic_review_api_key_env", "STEPFUN_API_KEY")
    if probe.env_value(env_name) is None:
        return PreflightCheck(
            name="topic_review",
            status=WARN,
            detail=f"主题评审已启用（{provider}/{model}），但未设置 {env_name}，将走未评审降级路径。",
            hint=f"导出 `{env_name}` 以启用评审；或运行时加 `--allow-unreviewed-export` 兼容导出。",
        )
    return PreflightCheck(
        name="topic_review",
        status=OK,
        detail=f"主题评审配置完整（{provider}/{model}）。",
    )
