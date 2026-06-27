"""Markdown 报告生成。"""

import datetime
import os


def _escape_markdown_cell(value):
    """转义 Markdown 表格单元格中的特殊字符。"""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _escape_markdown_text(value):
    """清理普通 Markdown 文本中的换行，避免破坏报告结构。"""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def generate_single_report(video_name, output_dir, total_duration, silences, segments, candidates, best):
    """生成单视频处理报告。"""
    report_path = os.path.join(output_dir, f"{video_name}_report.md")
    with open(report_path, "w", encoding="utf-8") as file:
        file.write(f"# {video_name} Clip Report\n\n")
        file.write("**Version**: v4.7\n")
        file.write(f"**Processed**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        file.write("## Video Info\n\n")
        file.write(f"- Duration: {total_duration:.1f}s ({total_duration / 60:.1f}min)\n")
        file.write(f"- Silence spans: {len(silences)}\n- Segments: {len(segments)}\n- Candidates: {len(candidates)}\n\n")
        file.write("## Candidate Comparison\n\n")
        file.write("| Segment | Time Range | Duration | Base | Adjusted | Natural End | Duplicate | Selected |\n")
        file.write("|---------|------------|----------|------|----------|-------------|-----------|----------|\n")
        for candidate in candidates:
            file.write(
                f"| seg_{candidate.index} | {candidate.start_time:.1f}-{candidate.end_time:.1f}s | "
                f"{candidate.duration:.1f}s | {candidate.total_score} | {candidate.adjusted_score:.1f} | "
                f"{'yes' if candidate.is_natural_end else 'no'} | "
                f"{'yes' if candidate.is_duplicate else ''} | "
                f"{'✅' if candidate.index == best.index else ''} |\n"
            )
        file.write("\n## Final Selection\n\n")
        file.write(f"- **Segment**: segment_{best.index}\n")
        file.write(f"- **Time**: {best.start_time:.1f}s - {best.end_time:.1f}s\n")
        file.write(f"- **Duration**: {best.duration:.1f}s\n")
        file.write(f"- **Adjusted Score**: {best.adjusted_score:.1f}\n")
        if best.transcript:
            file.write(f"- **Transcript**: {_escape_markdown_text(best.transcript)}\n")
    return report_path


def generate_batch_report(output_dir, clips, kept, removed, final_path):
    """生成批处理汇总报告。"""
    report_path = os.path.join(output_dir, "batch_report.md")
    total_duration = sum(clip.duration for clip in kept)
    with open(report_path, "w", encoding="utf-8") as file:
        file.write("# Batch Processing Report\n\n")
        file.write("**Version**: v4.7\n")
        file.write(f"**Processed**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")

        if removed:
            file.write("## Cross-Video Dedup\n\n")
            file.write("| Video | Adjusted | Natural End | Decision | Reason |\n")
            file.write("|-------|----------|--------------|----------|--------|\n")
            for clip in clips:
                decision = "❌ Remove" if clip.is_cross_duplicate else "✅ Keep"
                reason = f"duplicate of {clip.duplicate_of}" if clip.is_cross_duplicate else ""
                file.write(
                    f"| {_escape_markdown_cell(clip.video_name)} | {clip.adjusted_score:.1f} | "
                    f"{'yes' if clip.is_natural_end else 'no'} | {decision} | {_escape_markdown_cell(reason)} |\n"
                )
            file.write("\n")

        file.write(f"## Final Concatenation ({len(kept)} clips)\n\n")
        file.write("| # | Video | Duration | Adjusted | Natural End | Transcript Summary |\n")
        file.write("|---|-------|----------|----------|-------------|--------------------|\n")
        for index, clip in enumerate(kept, 1):
            summary = (clip.transcript[:40] + "...") if clip.transcript and len(clip.transcript) > 40 else (clip.transcript or "—")
            file.write(
                f"| {index} | {_escape_markdown_cell(clip.video_name)} | {clip.duration:.1f}s | "
                f"{clip.adjusted_score:.1f} | {'yes' if clip.is_natural_end else 'no'} | {_escape_markdown_cell(summary)} |\n"
            )
        file.write(f"\n**Total duration**: {total_duration:.1f}s ({total_duration / 60:.1f}min)\n")
        file.write(f"\n**Output file**: `{final_path}`\n")
    return report_path


def generate_live_report(
    video_name,
    output_dir,
    total_duration,
    silences,
    candidates,
    selected,
    exports,
    config=None,
    dry_run=False,
    warnings=None,
):
    """生成直播拆条报告。"""
    if not dry_run and len(selected) != len(exports):
        raise ValueError(f"selected ({len(selected)}) and exports ({len(exports)}) must have same length")

    report_name = (config or {}).get("live_report_name", "拆条报告.md")
    report_path = os.path.join(output_dir, report_name)
    selected_indexes = {candidate.index for candidate in selected}
    export_by_index = {
        candidate.index: export
        for candidate, export in zip(selected, exports)
    }

    with open(report_path, "w", encoding="utf-8") as file:
        file.write(f"# {video_name} 直播拆条报告\n\n")
        file.write("**Version**: v4.7\n")
        file.write(f"**Processed**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        reviewed = any(
            candidate.review is not None
            or (candidate.export_selection is not None and candidate.export_selection.review_status == "reviewed")
            for candidate in candidates
        )
        report_mode = _live_report_mode(reviewed, dry_run, selected, config)
        file.write(f"> {report_mode}\n")
        if dry_run and reviewed:
            file.write("> Dry-run：本报告包含主题评审结果，但未导出短视频。\n\n")
        elif dry_run:
            file.write("> Dry-run：本报告是未评审拆条方案，不代表发布就绪短视频。\n\n")
        elif reviewed:
            file.write("> Reviewed 非 dry-run 交付包：仅导出发布就绪候选。\n\n")
        elif (config or {}).get("allow_unreviewed_export", False):
            file.write("> 显式允许未评审导出的兼容方案。\n\n")
        else:
            file.write("> 未评审且未允许导出：不会生成短视频文件。\n\n")
        if warnings:
            file.write("## Warnings\n\n")
            for warning in warnings:
                file.write(f"- {_escape_markdown_text(warning)}\n")
            file.write("\n")
        file.write("## 视频信息\n\n")
        file.write(f"- Duration: {total_duration:.1f}s ({total_duration / 60:.1f}min)\n")
        file.write(f"- Silence spans: {len(silences)}\n")
        file.write(f"- Candidates: {len(candidates)}\n")
        if dry_run:
            file.write(f"- Selected clips: {len(selected)}\n")
            file.write("- Exported clips: 0 (dry-run)\n\n")
        else:
            file.write(f"- Exported clips: {len(exports)}\n")
            file.write(f"- 字幕烧录: {_burn_subtitles_status(config)}\n\n")

        file.write("## 导出清单\n\n")
        file.write("| # | Title | Topic | Ready | Final Range | Video | Subtitle |\n")
        file.write("|---|-------|-------|-------|-------------|-------|----------|\n")
        for candidate in selected:
            export = export_by_index.get(candidate.index)
            selection = candidate.export_selection
            output_path = _relative_or_placeholder(export.output_path, output_dir, "(dry-run)") if export else "(dry-run)"
            subtitle_path = _relative_or_placeholder(export.subtitle_path, output_dir, "") if export else "(dry-run)"
            ready_score = selection.publish_ready_score if selection else ""
            topic_name = selection.topic_name if selection else ""
            final_start = selection.final_start if selection else candidate.start_time
            final_end = selection.final_end if selection else candidate.end_time
            file.write(
                f"| {candidate.index} | {_escape_markdown_cell(candidate.title)} | "
                f"{_escape_markdown_cell(topic_name)} | {_escape_markdown_cell(ready_score)} | "
                f"{final_start:.1f}-{final_end:.1f}s | `{_escape_markdown_cell(output_path)}` | "
                f"`{_escape_markdown_cell(subtitle_path)}` |\n"
            )
        if not selected:
            file.write("| - | - | - | - | - | - | - |\n")

        if not dry_run and exports:
            file.write("\n## 字幕优化\n\n")
            file.write("| # | Title | 字幕优化状态 |\n")
            file.write("|---|-------|--------------|\n")
            for candidate in selected:
                export = export_by_index.get(candidate.index)
                if export is None:
                    continue
                file.write(
                    f"| {candidate.index} | {_escape_markdown_cell(candidate.title)} | "
                    f"{_escape_markdown_cell(_subtitle_optimization_status(export))} |\n"
                )

        reviewed_candidates = [candidate for candidate in candidates if candidate.review is not None]
        if reviewed_candidates:
            file.write("\n## 主题评审\n\n")
            file.write("| Candidate | Topic | Complete | Learning | Share | Ready | Decision | Human Review | Reason |\n")
            file.write("|-----------|-------|----------|----------|-------|-------|----------|--------------|--------|\n")
            for candidate in reviewed_candidates:
                review = candidate.review
                reason = review.reject_reason or review.boundary_fix_suggestion or ""
                file.write(
                    f"| candidate_{candidate.index} | {_escape_markdown_cell(review.topic_name)} | "
                    f"{'yes' if review.topic_complete else 'no'} | {review.learning_value} | "
                    f"{review.share_value} | {review.publish_ready_score} | "
                    f"{_escape_markdown_cell(review.export_decision)} | "
                    f"{'yes' if review.needs_human_review else 'no'} | "
                    f"{_escape_markdown_cell(reason)} |\n"
                )

        file.write("\n## 未导出候选\n\n")
        file.write("| Candidate | Time Range | Score | Reason | Human Review | Boundary Suggestion | Preview |\n")
        file.write("|-----------|------------|-------|--------|--------------|---------------------|---------|\n")
        for candidate in candidates:
            selection = candidate.export_selection
            if candidate.index in selected_indexes:
                continue
            reason = selection.reason if selection else ("duplicate" if candidate.is_duplicate else "unselected")
            needs_human_review = _needs_human_review(candidate)
            boundary_suggestion = _boundary_fix_suggestion(candidate)
            preview = candidate.text[:40] + "..." if len(candidate.text) > 40 else candidate.text
            file.write(
                f"| candidate_{candidate.index} | {candidate.start_time:.1f}-{candidate.end_time:.1f}s | "
                f"{_live_candidate_score(candidate):.1f} | {_escape_markdown_cell(reason)} | "
                f"{'yes' if needs_human_review else 'no'} | {_escape_markdown_cell(boundary_suggestion)} | "
                f"{_escape_markdown_cell(preview)} |\n"
            )
        if all(candidate.index in selected_indexes for candidate in candidates):
            file.write("| - | - | - | - | - | - | - |\n")

        human_review_candidates = [candidate for candidate in candidates if _needs_human_review(candidate)]
        file.write("\n## 人工复核\n\n")
        file.write("| Candidate | Topic | Reason | Boundary Suggestion |\n")
        file.write("|-----------|-------|--------|---------------------|\n")
        for candidate in human_review_candidates:
            selection = candidate.export_selection
            topic_name = selection.topic_name if selection else (candidate.review.topic_name if candidate.review else "")
            reason = selection.reason if selection else ""
            file.write(
                f"| candidate_{candidate.index} | {_escape_markdown_cell(topic_name)} | "
                f"{_escape_markdown_cell(reason)} | {_escape_markdown_cell(_boundary_fix_suggestion(candidate))} |\n"
            )
        if not human_review_candidates:
            file.write("| - | - | - | - |\n")

        file.write("\n## 同主题系列\n\n")
        file.write("| Series | Topic | Candidates |\n")
        file.write("|--------|-------|------------|\n")
        series_rows = _series_rows(selected)
        for series_key, topic_name, indexes in series_rows:
            file.write(
                f"| {_escape_markdown_cell(series_key)} | {_escape_markdown_cell(topic_name)} | "
                f"{_escape_markdown_cell(', '.join(f'candidate_{index}' for index in indexes))} |\n"
            )
        if not series_rows:
            file.write("| - | - | - |\n")

        file.write("\n## 标准交付物\n\n")
        file.write("| Deliverable | Generated |\n")
        file.write("|-------------|-----------|\n")
        deliverables = _deliverable_status(output_dir, config, dry_run)
        for name, generated in deliverables:
            file.write(f"| `{name}` | {'yes' if generated else 'no'} |\n")
    return report_path


def _live_candidate_score(candidate):
    return candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score


def _subtitle_optimization_status(export):
    """逐条 clip 字幕优化状态：成功标已优化烧录，失败标旁挂规则字幕待人工复核。"""
    if getattr(export, "subtitle_optimized", True):
        return "已优化烧录"
    note = getattr(export, "subtitle_optimization_note", "")
    return f"未优化·旁挂规则字幕·待人工复核（{note}）" if note else "未优化·旁挂规则字幕·待人工复核"


def _burn_subtitles_status(config):
    config = config or {}
    if not config.get("export_subtitles", True):
        return "关（未导出字幕）"
    if config.get("burn_subtitles", True):
        return "开（白字黑描边·底部居中）"
    return "关（仅旁挂 SRT）"


def _live_report_mode(reviewed, dry_run, selected, config):
    config = config or {}
    if reviewed and dry_run:
        return "Reviewed dry-run 发布方案：列出计划导出的发布就绪项，但不会生成短视频。"
    if reviewed:
        return "Reviewed 非 dry-run 交付包：包含实际导出文件和字幕文件。"
    if config.get("allow_unreviewed_export", False):
        return "未评审兼容导出方案：用户显式允许按旧分数路径导出。"
    return "未评审且未导出方案：缺少可用评审结果，默认不导出短视频。"


def _relative_or_placeholder(path, output_dir, placeholder):
    if not path:
        return placeholder
    try:
        return os.path.relpath(path, output_dir)
    except ValueError:
        return path


def _needs_human_review(candidate):
    selection = candidate.export_selection
    if selection is not None:
        return selection.needs_human_review or selection.reason in {"needs_human_review", "boundary_fix_needs_human_review"}
    return bool(candidate.review and candidate.review.needs_human_review)


def _boundary_fix_suggestion(candidate):
    selection = candidate.export_selection
    if selection is not None:
        return selection.boundary_fix_suggestion or ""
    if candidate.review is not None:
        return candidate.review.boundary_fix_suggestion or ""
    return ""


def _series_rows(selected):
    groups = {}
    for candidate in selected:
        selection = candidate.export_selection
        if selection is None or not selection.series_key:
            continue
        groups.setdefault(selection.series_key, {"topic": selection.topic_name, "indexes": []})
        groups[selection.series_key]["indexes"].append(candidate.index)
    return [
        (series_key, data["topic"], data["indexes"])
        for series_key, data in sorted(groups.items(), key=lambda item: item[0])
    ]


def _deliverable_status(output_dir, config, dry_run):
    config = config or {}
    return [
        ("plan.json", os.path.exists(os.path.join(output_dir, "plan.json"))),
        ("transcript.srt", os.path.exists(os.path.join(output_dir, "transcript.srt"))),
        ("metadata.json", (not dry_run) and os.path.exists(os.path.join(output_dir, "metadata.json"))),
        ("clips/", (not dry_run) and os.path.isdir(os.path.join(output_dir, "clips"))),
        (
            "subtitles/",
            (not dry_run)
            and config.get("export_subtitles", True)
            and os.path.isdir(os.path.join(output_dir, "subtitles")),
        ),
        (config.get("live_report_name", "拆条报告.md"), True),
    ]
