"""直播拆条结果导出。"""

import json
import os
import re
from datetime import datetime, timezone

from video_auto_editor.config import CONFIG
from video_auto_editor.media import clip_segment
from video_auto_editor.models import LiveClipInfo, TranscriptChunk
from video_auto_editor.transcript import export_srt


def export_live_clips(
    video_path,
    selected,
    chunks,
    output_dir,
    config=None,
    candidates=None,
    review_status="reviewed",
    review_provider=None,
):
    """批量导出直播短视频、字幕和 metadata；任一视频失败时返回 None。"""
    config = config or CONFIG
    clips_dir = os.path.join(output_dir, "clips")
    subtitles_dir = os.path.join(output_dir, "subtitles")
    written_paths = []
    exports = []

    try:
        os.makedirs(clips_dir, exist_ok=True)
        if config.get("export_subtitles", True):
            os.makedirs(subtitles_dir, exist_ok=True)

        for output_index, candidate in enumerate(selected, 1):
            selection = candidate.export_selection
            filename_base = f"{output_index:03d}_{_safe_filename(candidate.title or f'直播片段_{output_index:03d}')}"
            output_path = os.path.join(clips_dir, f"{filename_base}.mp4")
            if not clip_segment(video_path, candidate, output_path, config):
                _cleanup_written_paths(written_paths + [output_path])
                return None
            written_paths.append(output_path)

            subtitle_path = ""
            if config.get("export_subtitles", True):
                subtitle_path = os.path.join(subtitles_dir, f"{filename_base}.srt")
                clip_start = max(0.0, candidate.start_time - float(config["buffer_start"]))
                clip_end = candidate.end_time + float(config["buffer_end"])
                written_paths.append(subtitle_path)
                export_srt(_slice_chunks_for_clip(chunks, clip_start, clip_end), subtitle_path)

            exports.append(
                LiveClipInfo(
                    index=output_index,
                    title=candidate.title or f"直播片段_{output_index:03d}",
                    start_time=candidate.start_time,
                    end_time=candidate.end_time,
                    duration=candidate.duration,
                    score=candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score,
                    text=candidate.text,
                    output_path=output_path,
                    subtitle_path=subtitle_path,
                    summary=candidate.summary,
                    keywords=list(candidate.keywords),
                    topic_name=selection.topic_name if selection else "",
                    publish_ready_score=selection.publish_ready_score if selection else None,
                    export_decision=selection.decision if selection else "",
                    decision_reason=selection.reason if selection else "",
                    original_start=selection.original_start if selection else candidate.start_time,
                    original_end=selection.original_end if selection else candidate.end_time,
                    final_start=selection.final_start if selection else candidate.start_time,
                    final_end=selection.final_end if selection else candidate.end_time,
                    boundary_fix_applied=selection.boundary_fix_applied if selection else False,
                    boundary_fix_suggestion=selection.boundary_fix_suggestion if selection else "",
                    series_key=selection.series_key if selection else "",
                    needs_human_review=selection.needs_human_review if selection else False,
                )
            )

        metadata_path = os.path.join(output_dir, "metadata.json")
        written_paths.append(metadata_path)
        _write_metadata(
            video_path,
            exports,
            output_dir,
            config,
            candidates=candidates,
            review_status=review_status,
            review_provider=review_provider,
        )
        return exports
    except (OSError, ValueError):
        _cleanup_written_paths(written_paths)
        return None


def _slice_chunks_for_clip(chunks, clip_start, clip_end):
    sliced = []
    for chunk in chunks:
        start = max(float(chunk.start), clip_start)
        end = min(float(chunk.end), clip_end)
        if end <= start:
            continue
        text = str(chunk.text).strip()
        if not text:
            continue
        sliced.append(
            TranscriptChunk(
                start=start - clip_start,
                end=end - clip_start,
                text=text,
            )
        )
    return sliced


def _write_metadata(video_path, exports, output_dir, config=None, candidates=None, review_status="reviewed", review_provider=None):
    config = config or CONFIG
    metadata_path = os.path.join(output_dir, "metadata.json")
    clip_payloads = [_clip_metadata(item, output_dir) for item in exports]
    not_exported = _not_exported_summary(candidates or [])
    human_review = [item for item in not_exported if item.get("needs_human_review")]
    payload = {
        "source_video": os.path.basename(video_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": review_status,
        "review_provider": review_provider or {},
        "publish_ready_threshold": config.get("topic_review_publish_ready_threshold"),
        "export_count": len(exports),
        "not_exported_count": len(not_exported),
        "clips": clip_payloads,
        "exports": clip_payloads,
        "not_exported": not_exported,
        "human_review": human_review,
    }
    with open(metadata_path, "w", encoding="utf-8") as metadata_file:
        json.dump(payload, metadata_file, ensure_ascii=False, indent=2)
    return metadata_path


def _clip_metadata(item, output_dir):
    return {
        "index": item.index,
        "title": item.title,
        "start": item.start_time,
        "end": item.end_time,
        "duration": item.duration,
        "summary": item.summary,
        "keywords": item.keywords,
        "score": item.score,
        "topic_name": item.topic_name,
        "publish_ready_score": item.publish_ready_score,
        "export_decision": item.export_decision,
        "decision_reason": item.decision_reason,
        "original_start": item.original_start,
        "original_end": item.original_end,
        "final_start": item.final_start,
        "final_end": item.final_end,
        "boundary_fix_applied": item.boundary_fix_applied,
        "boundary_fix_suggestion": item.boundary_fix_suggestion,
        "series_key": item.series_key,
        "needs_human_review": item.needs_human_review,
        "output_path": os.path.relpath(item.output_path, output_dir),
        "subtitle_path": os.path.relpath(item.subtitle_path, output_dir) if item.subtitle_path else "",
    }


def _not_exported_summary(candidates):
    summary = []
    for candidate in candidates:
        selection = candidate.export_selection
        if selection is None or selection.selected_for_export:
            continue
        summary.append(
            {
                "candidate_index": candidate.index,
                "decision": selection.decision,
                "reason": selection.reason,
                "topic_name": selection.topic_name,
                "publish_ready_score": selection.publish_ready_score,
                "needs_human_review": selection.needs_human_review or selection.reason in {
                    "needs_human_review",
                    "boundary_fix_needs_human_review",
                },
                "boundary_fix_suggestion": selection.boundary_fix_suggestion,
                "original_start": selection.original_start,
                "original_end": selection.original_end,
                "final_start": selection.final_start,
                "final_end": selection.final_end,
                "series_key": selection.series_key,
            }
        )
    return summary


def _cleanup_written_paths(paths):
    for path in reversed(paths):
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _safe_filename(value):
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", str(value))
    safe = re.sub(r"\s+", "_", safe).strip("._ ")
    return (safe or "直播片段")[:48]
