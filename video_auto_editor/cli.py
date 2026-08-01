"""命令行入口与直播拆条流程编排。"""

import argparse
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import TextIO

from video_auto_editor.application import (
    LiveRunOutcome,
    LiveRunRequest,
    LiveRunState,
)
from video_auto_editor.application.cache_maintenance import (
    CacheMaintenanceApplication,
)
from video_auto_editor.composition import compose_live_application
from video_auto_editor.runtime.errors import ERROR_REGISTRY
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import WorkspaceFailure


class TerminalRenderer:
    """把类型化直播拆条终态渲染到标准终端流。"""

    __slots__ = ("_quiet", "_stderr", "_stdout")

    def __init__(
        self,
        *,
        quiet: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        if not isinstance(quiet, bool):
            raise TypeError("终端安静模式必须是布尔值")
        self._quiet = quiet
        self._stdout = sys.stdout if stdout is None else stdout
        self._stderr = sys.stderr if stderr is None else stderr

    def disclose_providers(self, disclosures: tuple[object, ...]) -> None:
        """在首次远程请求前输出脱敏供应商外发计划。"""
        if self._quiet:
            return
        self._stdout.write("供应商外发计划:\n")
        for disclosure in disclosures:
            provider_id = getattr(disclosure, "provider_id")
            model_id = getattr(disclosure, "model_id")
            purpose = getattr(disclosure, "purpose").value
            categories = ",".join(
                category.value
                for category in getattr(disclosure, "data_categories")
            )
            self._stdout.write(
                f"- {provider_id}/{model_id}: {purpose} [{categories}]\n"
            )
        self._stdout.flush()

    def render_live_outcome(
        self,
        outcome: LiveRunOutcome,
        diagnostics_directory: Path,
    ) -> None:
        """始终渲染终态、运行标识和诊断位置，包括安静模式。"""
        if not isinstance(outcome, LiveRunOutcome):
            raise TypeError("终端渲染器只接受 LiveRunOutcome")
        stream = (
            self._stdout
            if outcome.state is LiveRunState.SUCCEEDED
            else self._stderr
        )
        stream.write(f"终态: {outcome.state.value}\n")
        stream.write(f"run_id: {outcome.run_id}\n")
        stream.write(f"诊断位置: {diagnostics_directory}\n")
        if outcome.primary_error_code is not None:
            message = (
                outcome.primary_error.safe_message
                if outcome.primary_error is not None
                else outcome.primary_error_code.value
            )
            stream.write(
                f"错误: {outcome.primary_error_code.value} — {message}\n"
            )
        if outcome.diagnostics_incomplete:
            stream.write("警告: 运行诊断可能不完整\n")
        if outcome.recovery_incomplete:
            stream.write("警告: 运行清理可能不完整\n")
        stream.flush()


def _diagnostics_directory(
    source: str,
    workspace_dir: str | None,
    run_id: RunId,
) -> Path:
    try:
        source_path = Path(source).resolve(strict=False)
    except (OSError, RuntimeError):
        source_path = Path(source).absolute()
    if workspace_dir is None:
        workspace = (
            source_path.with_suffix(".autocut")
            if source_path.suffix.casefold() == ".mp4"
            else source_path.with_name(f"{source_path.name}.autocut")
        )
    else:
        try:
            workspace = Path(workspace_dir).resolve(strict=False)
        except (OSError, RuntimeError):
            workspace = Path(workspace_dir).absolute()
    return workspace / "work" / "runs" / str(run_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-auto-editor",
        description="直播拆条 CLI 底座",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('video-auto-editor')}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    live_parser = subparsers.add_parser(
        "live",
        help="执行一次正式直播拆条运行",
    )
    live_parser.add_argument("source", metavar="SOURCE", help="单个 MP4 素材路径")
    live_parser.add_argument(
        "--workspace-dir",
        metavar="DIR",
        help="显式 workspace 根目录",
    )
    live_parser.add_argument("--overwrite", action="store_true", help="覆盖现有标准交付物")

    cache_parser = subparsers.add_parser("cache", help="维护处理缓存")
    cache_subparsers = cache_parser.add_subparsers(
        dest="cache_command",
        required=True,
    )
    clear_parser = cache_subparsers.add_parser(
        "clear",
        help="清空受管 workspace 的处理缓存",
    )
    clear_parser.add_argument(
        "workspace",
        metavar="WORKSPACE",
        help="受管 workspace 根目录",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析公共命令并把正式直播拆条交给生产组合根。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "cache":
        try:
            CacheMaintenanceApplication().clear(args.workspace)
        except WorkspaceFailure as failure:
            definition = ERROR_REGISTRY[failure.error_code]
            print(
                f"缓存维护失败: {failure.error_code.value} — "
                f"{definition.safe_message}",
                file=sys.stderr,
            )
            return int(definition.exit_code)
        print("处理缓存已清空")
        return 0

    renderer = TerminalRenderer()
    outcome = compose_live_application(
        disclosure_sink=renderer.disclose_providers,
    ).execute(
        LiveRunRequest(
            args.source,
            workspace_dir=args.workspace_dir,
            overwrite=args.overwrite,
        )
    )
    renderer.render_live_outcome(
        outcome,
        _diagnostics_directory(
            args.source,
            args.workspace_dir,
            outcome.run_id,
        ),
    )
    return int(outcome.exit_code)
