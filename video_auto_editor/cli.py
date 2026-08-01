"""命令行入口与直播拆条流程编排。"""

import argparse
import os
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
from video_auto_editor.config import CONFIG
from video_auto_editor.dedup import check_duplicate_live_candidates
from video_auto_editor.export import export_live_clips
from video_auto_editor.media import get_video_duration
from video_auto_editor.plan import write_plan
from video_auto_editor.report import generate_live_report
from video_auto_editor.review import build_topic_review_batches, create_topic_reviewer
from video_auto_editor.runtime.errors import ERROR_REGISTRY
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.subtitle_optimizer import create_subtitle_optimizer
from video_auto_editor.selection import select_live_exports
from video_auto_editor.silence import detect_silence
from video_auto_editor.transcript import export_srt, transcribe_video
from video_auto_editor.topic import enrich_clip_candidates, generate_clip_candidates
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


def process_live_video(video_path, output_dir, work_dir, config=None, course_context=None, dry_run=False):
    """直播拆条 MVP：输出多条短视频、metadata 和报告。"""
    config = config or CONFIG
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    video_work = os.path.join(work_dir, video_name)
    os.makedirs(video_work, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"  Video Auto Editor v4.7 - Live MVP\n  Input: {video_path}")
    print(f"{'=' * 60}\n")
    if course_context is not None:
        print("📚 Course context: loaded")
    if dry_run:
        print("🧪 Dry-run: will generate transcript, report and plan.json without exporting clips")

    print("📋 Step 1: Getting video info...")
    total_duration = get_video_duration(video_path)
    if total_duration is None:
        print("   ❌ Failed to get video info")
        return None
    print(f"   Duration: {total_duration:.1f}s ({total_duration / 60:.1f}min)")

    print("\n🔇 Step 2: Silence detection...")
    try:
        silences = detect_silence(video_path, config)
    except RuntimeError as exc:
        print(f"   ❌ {exc}")
        return None
    print(f"   Detected {len(silences)} silence spans")

    print("\n🎤 Step 3: Transcribing full video...")
    transcript_result = transcribe_video(video_path, video_work, config=config)
    if not transcript_result.success:
        print(f"   ❌ {transcript_result.error}")
        return None

    source = "cache" if transcript_result.from_cache else config.get("asr_provider", "asr")
    print(f"   ✅ Loaded {len(transcript_result.chunks)} transcript chunks from {source}")
    print(f"   📄 Transcript cache: {transcript_result.cache_path}")

    srt_path = os.path.join(output_dir, "transcript.srt")
    export_srt(transcript_result.chunks, srt_path)
    print(f"   📄 Transcript SRT: {srt_path}")

    print("\n🧩 Step 4: Generating clip candidates...")
    candidates = generate_clip_candidates(transcript_result.chunks, silences, total_duration, config)
    candidates = enrich_clip_candidates(candidates, config)
    print(f"   Generated {len(candidates)} clip candidates")
    for candidate in candidates:
        preview = candidate.text[:50] + "..." if len(candidate.text) > 50 else candidate.text
        print(
            f"   candidate_{candidate.index}: {candidate.start_time:.1f}-{candidate.end_time:.1f}s "
            f"({candidate.duration:.1f}s) score={_live_candidate_score(candidate):.1f} | {preview}"
        )

    print("\n🔄 Step 5: Duplicate content detection...")
    candidates = check_duplicate_live_candidates(candidates, config)
    print(f"   Marked {sum(1 for candidate in candidates if candidate.is_duplicate)} duplicate candidates")

    print("\n🧠 Step 6: Reviewing clip topics...")
    review_status, review_provider, review_warnings = _review_live_candidates(
        candidates,
        course_context,
        config,
        video_work=video_work,
    )
    if review_status == "reviewed":
        print(f"   ✅ Reviewed {len(candidates)} candidates")
    else:
        print("   ⚠️  Topic review unavailable; writing unreviewed plan")
        for warning in review_warnings:
            print(f"   ⚠️  {warning}")
    review_diagnostics = review_provider.get("review_diagnostics", {})
    plan_review_provider = {
        key: value
        for key, value in review_provider.items()
        if key != "review_diagnostics"
    }

    print("\n🏆 Step 7: Selecting live exports...")
    selected, _ = select_live_exports(candidates, None, config, review_status=review_status)
    if not selected:
        print("   ⚠️  No live clips selected for export")
    else:
        for candidate in selected:
            print(
                f"   ✅ candidate_{candidate.index}: {candidate.start_time:.1f}-{candidate.end_time:.1f}s "
                f"score={_live_candidate_score(candidate):.1f} title={candidate.title}"
            )

    warnings = _build_live_warnings(config, review_warnings)
    plan_path = write_plan(
        video_path,
        output_dir,
        course_context,
        candidates,
        selected,
        warnings,
        status=review_status,
        review_provider=plan_review_provider,
        review_diagnostics=review_diagnostics,
        config=config,
        dry_run=dry_run,
    )
    print(f"   📄 Plan: {plan_path}")

    exports = []
    if dry_run:
        print("\n✂️  Step 8: Dry-run skips live clip export")
    elif not selected:
        print("\n✂️  Step 8: No selected live clips; skipping export")
    else:
        print("\n✂️  Step 8: Exporting live clips...")
        config["subtitle_optimization_cache_dir"] = os.path.join(video_work, "subtitle_optimization_cache")
        subtitle_optimizer = _create_subtitle_optimizer(config)
        exports = export_live_clips(
            video_path,
            selected,
            transcript_result.chunks,
            output_dir,
            config,
            candidates=candidates,
            review_status=review_status,
            review_provider=plan_review_provider,
            subtitle_optimizer=subtitle_optimizer,
        )
        if exports is None:
            print("   ❌ Failed to export live clips")
            return None
        print(f"   ✅ Exported {len(exports)} clips")
        print(f"   📄 Metadata: {os.path.join(output_dir, 'metadata.json')}")

    report_path = generate_live_report(
        video_name,
        output_dir,
        total_duration,
        silences,
        candidates,
        selected,
        exports,
        config,
        dry_run=dry_run,
        warnings=warnings,
    )
    print(f"   📄 Report: {report_path}")

    print("\n✅ Live MVP complete.")
    return exports


def _live_candidate_score(candidate):
    return candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score


def _create_subtitle_optimizer(config):
    """按配置创建字幕优化 provider；配置非法则降级为不优化（None），不阻塞导出。"""
    try:
        return create_subtitle_optimizer(config)
    except ValueError as exc:
        print(f"   ⚠️  字幕优化 provider 配置错误，回退规则字幕：{exc}")
        return None


def _review_live_candidates(candidates, course_context, config, video_work=None):
    if not candidates:
        return "unreviewed", {}, []
    if not config.get("topic_review_enabled", True):
        return "unreviewed", {}, ["主题评审已关闭，plan.json status 为 unreviewed。"]

    try:
        review_config = dict(config)
        if video_work:
            review_config["topic_review_cache_dir"] = os.path.join(video_work, "topic_review_cache")
        reviewer = create_topic_reviewer(review_config)
    except ValueError as exc:
        return "unreviewed", {}, [f"主题评审 provider 配置错误：{exc}"]

    provider_info = _topic_reviewer_info(reviewer)
    if not reviewer.is_available():
        return "unreviewed", provider_info, ["主题评审不可用：缺少 API Key，未发起评审请求。"]

    try:
        batches = build_topic_review_batches(candidates, course_context, config)
    except ValueError as exc:
        return "unreviewed", provider_info, [f"主题评审配置错误：{exc}"]

    result = reviewer.review_batches(batches)
    provider_info = result.provider_info or provider_info

    # 只要有任意候选评审成功，就进入 reviewed 选择路径：失败批次内的候选因无 review，
    # 会在 _reviewed_rejection_reason 中以 missing_review 自然跳过导出。仅当成功评审数
    # 为 0 时才整体降级为 unreviewed，保护既有契约。
    if not result.reviews:
        provider_info = dict(provider_info)
        provider_info["review_diagnostics"] = _review_failure_diagnostics(result)
        return "unreviewed", provider_info, [f"主题评审不可用：主题评审失败：{result.error}"]

    for candidate in candidates:
        review = result.reviews.get(candidate.index)
        if review is None:
            continue
        candidate.review = review
        candidate.title = review.title or candidate.title
        candidate.summary = review.summary or candidate.summary
        candidate.keywords = list(review.keywords) or candidate.keywords

    warnings = []
    if result.failed_batches:
        provider_info = dict(provider_info)
        provider_info["review_diagnostics"] = _review_failure_diagnostics(result)
        warnings.append(
            f"部分批次评审失败（{len(result.failed_batches)}/{len(batches)}），相关候选已跳过导出。"
        )
    return "reviewed", provider_info, warnings


def _topic_reviewer_info(reviewer):
    return {
        "provider": getattr(reviewer, "provider_name", ""),
        "model": getattr(reviewer, "model", ""),
        "base_url": getattr(reviewer, "base_url", ""),
    }


def _review_failure_diagnostics(result):
    failed_batches = [dict(batch) for batch in getattr(result, "failed_batches", [])]
    return {
        "reviewed_candidate_count": len(getattr(result, "reviews", {}) or {}),
        "failed_review_batch_count": len(failed_batches),
        "failed_review_batches": failed_batches,
    }


def _build_live_warnings(config, review_warnings=None):
    warnings = list(review_warnings or [])
    return warnings


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
