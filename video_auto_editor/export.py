"""直播拆条结果导出。"""

import json
import os
import re

from video_auto_editor.config import CONFIG
from video_auto_editor.media import clip_segment
from video_auto_editor.models import LiveClipInfo, TranscriptChunk
from video_auto_editor.transcript import export_srt


def export_live_clips(video_path, selected, chunks, output_dir, config=None):
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
        _write_metadata(video_path, exports, output_dir)
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


def _write_metadata(video_path, exports, output_dir):
    metadata_path = os.path.join(output_dir, "metadata.json")
    payload = {
        "source_video": os.path.basename(video_path),
        "clips": [
            {
                "index": item.index,
                "title": item.title,
                "start": item.start_time,
                "end": item.end_time,
                "duration": item.duration,
                "summary": item.summary,
                "keywords": item.keywords,
                "score": item.score,
                "output_path": os.path.relpath(item.output_path, output_dir),
                "subtitle_path": os.path.relpath(item.subtitle_path, output_dir) if item.subtitle_path else "",
            }
            for item in exports
        ],
    }
    with open(metadata_path, "w", encoding="utf-8") as metadata_file:
        json.dump(payload, metadata_file, ensure_ascii=False, indent=2)
    return metadata_path


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
