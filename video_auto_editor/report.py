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


def generate_live_report(video_name, output_dir, total_duration, silences, candidates, selected, exports, config=None, warnings=None):
    """生成直播拆条报告。"""
    if len(selected) != len(exports):
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
        if warnings:
            file.write("## Warnings\n\n")
            for warning in warnings:
                file.write(f"- {_escape_markdown_text(warning)}\n")
            file.write("\n")
        file.write("## 视频信息\n\n")
        file.write(f"- Duration: {total_duration:.1f}s ({total_duration / 60:.1f}min)\n")
        file.write(f"- Silence spans: {len(silences)}\n")
        file.write(f"- Candidates: {len(candidates)}\n")
        file.write(f"- Exported clips: {len(exports)}\n\n")

        file.write("## 入选片段\n\n")
        file.write("| # | Title | Time Range | Duration | Score | Output |\n")
        file.write("|---|-------|------------|----------|-------|--------|\n")
        for candidate in selected:
            export = export_by_index.get(candidate.index)
            output_path = export.output_path if export else ""
            file.write(
                f"| {candidate.index} | {_escape_markdown_cell(candidate.title)} | "
                f"{candidate.start_time:.1f}-{candidate.end_time:.1f}s | {candidate.duration:.1f}s | "
                f"{_live_candidate_score(candidate):.1f} | `{_escape_markdown_cell(output_path)}` |\n"
            )

        file.write("\n## 候选决策\n\n")
        file.write("| Candidate | Time Range | Duration | Score | Decision | Reason | Preview |\n")
        file.write("|-----------|------------|----------|-------|----------|--------|---------|\n")
        for candidate in candidates:
            if candidate.index in selected_indexes:
                decision = "Keep"
                reason = "selected"
            elif candidate.is_duplicate:
                decision = "Drop"
                reason = "duplicate"
            else:
                decision = "Drop"
                reason = "lower score or overlap"
            preview = candidate.text[:40] + "..." if len(candidate.text) > 40 else candidate.text
            file.write(
                f"| candidate_{candidate.index} | {candidate.start_time:.1f}-{candidate.end_time:.1f}s | "
                f"{candidate.duration:.1f}s | {_live_candidate_score(candidate):.1f} | "
                f"{decision} | {reason} | {_escape_markdown_cell(preview)} |\n"
            )

        file.write("\n## 输出文件\n\n")
        file.write("- `plan.json`\n")
        file.write("- `metadata.json`\n")
        file.write("- `transcript.srt`\n")
        file.write("- `clips/`\n")
        if (config or {}).get("export_subtitles", True):
            file.write("- `subtitles/`\n")
    return report_path


def _live_candidate_score(candidate):
    return candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score
