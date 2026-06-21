"""命令行入口与单视频、批处理流程编排。"""

import argparse
import datetime
import glob
import os
import shutil

from video_auto_editor.config import CONFIG
from video_auto_editor.context import load_course_context
from video_auto_editor.dedup import check_duplicate_content, check_duplicate_live_candidates, cross_video_dedup
from video_auto_editor.export import export_live_clips
from video_auto_editor.media import clip_segment, concat_videos, get_video_duration
from video_auto_editor.models import ClipInfo
from video_auto_editor.plan import write_plan
from video_auto_editor.report import generate_batch_report, generate_live_report, generate_single_report
from video_auto_editor.review import build_topic_review_batches, create_topic_reviewer
from video_auto_editor.scoring import analyze_fluency, calculate_adjusted_score, score_segment
from video_auto_editor.selection import select_best_segment, select_live_exports
from video_auto_editor.silence import detect_silence, identify_segments
from video_auto_editor.transcript import export_srt, transcribe_candidates, transcribe_video
from video_auto_editor.topic import enrich_clip_candidates, generate_clip_candidates


def process_single_video(video_path, output_dir, work_dir, batch_mode=False, config=None):
    """处理单条视频，成功时返回 ClipInfo，失败时返回 None。"""
    config = config or CONFIG
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    video_work = os.path.join(work_dir, video_name)
    os.makedirs(video_work, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"  Processing: {video_name}" if batch_mode else f"  Video Auto Editor v4.7 - Scenario A\n  Input: {video_path}")
    print(f"{'=' * 60}\n")

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

    print("\n📝 Step 3: Segment identification...")
    segments = identify_segments(silences, total_duration)
    print(f"   Identified {len(segments)} segments")

    print("\n⭐ Step 4: Scoring...")
    for segment in segments:
        score_segment(segment, silences, total_duration)
        print(
            f"   segment_{segment.index}: {segment.start_time:.1f}s-{segment.end_time:.1f}s "
            f"({segment.duration:.1f}s) score={segment.total_score}"
        )

    print(f"\n🔍 Step 5: Filtering candidates (min_score={config['min_score']}, min_duration={config['min_duration']}s)...")
    candidates = [
        segment for segment in segments
        if segment.total_score >= config["min_score"] and segment.duration >= config["min_duration"]
    ]
    print(f"   {len(candidates)} candidate segments")

    if not candidates:
        print("\n⚠️  No candidates meet criteria, lowering standards...")
        candidates = sorted(segments, key=lambda segment: segment.total_score, reverse=True)[:5]
        print(f"   Selected top {len(candidates)} segments by score")

    candidates = transcribe_candidates(video_path, candidates, video_work, config=config)

    print("\n📊 Step 7: Fluency analysis...")
    for segment in candidates:
        if segment.transcript:
            (
                segment.repeat_count,
                segment.stutter_count,
                segment.is_natural_end,
                segment.is_interrupted,
            ) = analyze_fluency(segment.transcript)
        segment.adjusted_score = calculate_adjusted_score(segment, config)
        status = (" ✅natural end" if segment.is_natural_end else "") + (" ❌interrupted" if segment.is_interrupted else "")
        print(
            f"   segment_{segment.index}: base={segment.total_score} adjusted={segment.adjusted_score:.1f}"
            f" repeat={segment.repeat_count} stutter={segment.stutter_count}{status}"
        )

    print("\n🔄 Step 8: Duplicate content detection...")
    candidates = check_duplicate_content(candidates, config)
    print(f"   Marked {sum(1 for candidate in candidates if candidate.is_duplicate)} duplicate segments")

    print("\n🏆 Step 9: Selecting best segment...")
    best = select_best_segment(candidates)
    if not best:
        print("   ❌ Cannot select best segment")
        return None

    print(
        f"   ✅ Best: segment_{best.index} | "
        f"{best.start_time:.1f}-{best.end_time:.1f}s ({best.duration:.1f}s) | "
        f"adjusted={best.adjusted_score:.1f} | natural_end={'yes' if best.is_natural_end else 'no'}"
    )

    print("\n✂️  Step 10: Clipping output...")
    output_path = os.path.join(output_dir, f"{video_name}_clip.mp4")
    if not clip_segment(video_path, best, output_path, config):
        print("   ❌ Failed to clip")
        return None
    print(f"   ✅ Output: {output_path}")

    if not batch_mode:
        report_path = generate_single_report(
            video_name, output_dir, total_duration, silences, segments, candidates, best
        )
        print(f"   📄 Report: {report_path}")

    return ClipInfo(
        video_name=video_name,
        clip_path=output_path,
        transcript=best.transcript,
        adjusted_score=best.adjusted_score,
        is_natural_end=best.is_natural_end,
        duration=best.duration,
    )


def _find_video_files(input_dir):
    return sorted(
        glob.glob(os.path.join(input_dir, "*.MTS")) +
        glob.glob(os.path.join(input_dir, "*.mp4")) +
        glob.glob(os.path.join(input_dir, "*.mov"))
    )


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
    review_status, review_provider, review_warnings = _review_live_candidates(candidates, course_context, config)
    if review_status == "reviewed":
        print(f"   ✅ Reviewed {len(candidates)} candidates")
    else:
        print("   ⚠️  Topic review unavailable; writing unreviewed plan")

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
        review_provider=review_provider,
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
        exports = export_live_clips(
            video_path,
            selected,
            transcript_result.chunks,
            output_dir,
            config,
            candidates=candidates,
            review_status=review_status,
            review_provider=review_provider,
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


def process_batch(input_dir, output_dir, work_dir, config=None):
    """批处理视频目录，输出拼接视频和批处理报告。"""
    config = config or CONFIG
    video_files = _find_video_files(input_dir)
    if not video_files:
        print("❌ No video files found")
        return

    print(f"\n{'=' * 60}")
    print("  Video Auto Editor v4.7 - Scenario B (Batch)")
    print(f"  Input directory: {input_dir} ({len(video_files)} videos)")
    print(f"{'=' * 60}\n")

    clips = []
    failed = []
    for video_file in video_files:
        clip = process_single_video(video_file, output_dir, work_dir, batch_mode=True, config=config)
        if clip:
            clips.append(clip)
        else:
            failed.append(video_file)
            print(f"   ⚠️  Failed to process {os.path.basename(video_file)}")

    if not clips:
        print("❌ No videos processed successfully")
        return

    print(f"\n{'=' * 60}")
    print(f"  🔄 Cross-video dedup check ({len(clips)} clips)")
    print(f"{'=' * 60}\n")

    clips = cross_video_dedup(clips, config)
    kept = [clip for clip in clips if not clip.is_cross_duplicate]
    removed = [clip for clip in clips if clip.is_cross_duplicate]

    for clip in removed:
        print(f"   ❌ Remove {clip.video_name} (duplicate of {clip.duplicate_of}, adjusted {clip.adjusted_score:.1f})")
    for clip in kept:
        print(f"   ✅ Keep {clip.video_name} (adjusted {clip.adjusted_score:.1f})")
    print(f"\n   Dedup result: {len(clips)} -> {len(kept)} clips")

    print(f"\n{'=' * 60}")
    print(f"  🎬 Concatenating {len(kept)} clips")
    print(f"{'=' * 60}\n")

    final_path = os.path.join(output_dir, f"final_concat_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.mp4")
    if not concat_videos([clip.clip_path for clip in kept], final_path, config):
        print("   ❌ Concatenation failed")
        return
    print(f"   ✅ Final video: {final_path}")

    for clip in clips:
        if os.path.exists(clip.clip_path):
            os.remove(clip.clip_path)
    if os.path.exists(work_dir) and _can_remove_work_dir(work_dir, output_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
    elif os.path.exists(work_dir):
        print("   ⚠️  Skipping work_dir cleanup to avoid deleting output files")

    report_path = generate_batch_report(output_dir, clips, kept, removed, final_path)
    print(f"   📄 Report: {report_path}")
    print(f"\n{'=' * 60}")
    print(f"  Batch processing complete! ({len(kept)}/{len(clips)} clips)")
    print(f"{'=' * 60}\n")


def _can_remove_work_dir(work_dir, output_dir):
    """只有 work_dir 与 output_dir 完全不重叠时才允许目录级清理。"""
    work_abs = os.path.abspath(work_dir)
    output_abs = os.path.abspath(output_dir)
    common = os.path.commonpath([work_abs, output_abs])
    return common not in {work_abs, output_abs}


def _live_candidate_score(candidate):
    return candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score


def _review_live_candidates(candidates, course_context, config):
    if not candidates:
        return "unreviewed", {}, []
    if not config.get("topic_review_enabled", True):
        return "unreviewed", {}, ["主题评审已关闭，plan.json status 为 unreviewed。"]

    try:
        reviewer = create_topic_reviewer(config)
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
    if not result.success:
        return "unreviewed", provider_info, [f"主题评审失败：{result.error}"]

    for candidate in candidates:
        review = result.reviews.get(candidate.index)
        if review is None:
            continue
        candidate.review = review
        candidate.title = review.title or candidate.title
        candidate.summary = review.summary or candidate.summary
        candidate.keywords = list(review.keywords) or candidate.keywords
    return "reviewed", provider_info, []


def _topic_reviewer_info(reviewer):
    return {
        "provider": getattr(reviewer, "provider_name", ""),
        "model": getattr(reviewer, "model", ""),
        "base_url": getattr(reviewer, "base_url", ""),
    }


def _build_live_warnings(config, review_warnings=None):
    warnings = list(review_warnings or [])
    return warnings


def _add_common_output_args(parser):
    parser.add_argument("--output-dir", default="./output", help="输出目录，默认 ./output")
    parser.add_argument("--work-dir", default="./video_work", help="临时工作目录，默认 ./video_work")


def _positive_int(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m video_auto_editor",
        description="Video Auto Editor v4.7",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    single_parser = subparsers.add_parser("single", help="处理单个视频")
    single_parser.add_argument("video_path", help="输入视频文件路径")
    _add_common_output_args(single_parser)

    batch_parser = subparsers.add_parser("batch", help="批处理视频目录")
    batch_parser.add_argument("input_dir", help="输入视频目录")
    _add_common_output_args(batch_parser)

    live_parser = subparsers.add_parser("live", help="直播拆条 MVP：批量导出短视频、metadata 和报告")
    live_parser.add_argument("video_path", help="输入直播视频文件路径")
    _add_common_output_args(live_parser)
    live_parser.add_argument("--max-clips", type=_positive_int, default=None, help="最多导出短视频数量；未传时使用临时保护上限 5")
    live_parser.add_argument("--dry-run", action="store_true", help="只生成转写、报告和 plan.json，不导出短视频")
    live_parser.add_argument("--context-file", help="课程上下文 JSON 文件")
    live_parser.add_argument("--allow-unreviewed-export", action="store_true", help="允许导出未评审拆条结果")

    return parser


def main(argv=None):
    """模块命令入口，要求显式指定 single 或 batch 子命令。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(args.work_dir, exist_ok=True)

    if args.command == "batch":
        process_batch(args.input_dir, args.output_dir, args.work_dir)
        return

    if args.command == "live":
        live_config = CONFIG.copy()
        if args.max_clips is not None:
            live_config["max_clips"] = args.max_clips
            live_config["max_clips_user_provided"] = True
        live_config["allow_unreviewed_export"] = args.allow_unreviewed_export
        course_context = None
        if args.context_file:
            try:
                course_context = load_course_context(args.context_file)
            except ValueError as exc:
                parser.error(str(exc))
        process_live_video(
            args.video_path,
            args.output_dir,
            args.work_dir,
            config=live_config,
            course_context=course_context,
            dry_run=args.dry_run,
        )
        return

    clip = process_single_video(args.video_path, args.output_dir, args.work_dir)
    if clip:
        print(f"  Scenario A complete: {clip.clip_path}")
    else:
        print("  ❌ Processing failed")
