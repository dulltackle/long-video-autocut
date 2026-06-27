"""直播拆条结果导出。"""

import json
import os
import re
from datetime import datetime, timezone

from video_auto_editor.config import CONFIG
from video_auto_editor.media import clip_segment
from video_auto_editor.models import LiveClipInfo, TranscriptChunk
from video_auto_editor.subtitle import filter_filler_words, resegment_chunks
from video_auto_editor.transcript import export_srt


OPTIMIZE_OK = "OK"
OPTIMIZE_DISABLED = "DISABLED"
OPTIMIZE_FAILED = "FAILED"


def export_live_clips(
    video_path,
    selected,
    chunks,
    output_dir,
    config=None,
    candidates=None,
    review_status="reviewed",
    review_provider=None,
    subtitle_optimizer=None,
):
    """批量导出直播短视频、字幕和 metadata；任一视频失败时返回 None。

    各 clip 互相独立，按 export_concurrency 并发裁剪（含字幕切片），但产物顺序、
    文件名编号、失败清理与 metadata 内容与串行实现完全一致。
    """
    config = config or CONFIG
    clips_dir = os.path.join(output_dir, "clips")
    subtitles_dir = os.path.join(output_dir, "subtitles")
    export_subtitles = config.get("export_subtitles", True)
    written_paths = []

    try:
        os.makedirs(clips_dir, exist_ok=True)
        if export_subtitles:
            os.makedirs(subtitles_dir, exist_ok=True)

        jobs = _build_clip_jobs(selected, clips_dir, subtitles_dir, export_subtitles)
        results = _run_clip_jobs(jobs, video_path, chunks, config, export_subtitles, subtitle_optimizer)

        # 先汇总所有实际写出的路径（含失败 clip），保证失败时能清理干净。
        for result in results:
            written_paths.extend(result["paths"])
        if any(not result["ok"] for result in results):
            _cleanup_written_paths(written_paths)
            return None

        # executor.map / 串行均保持输入顺序，exports 按 output_index 升序排列。
        exports = [result["info"] for result in results]

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


def _build_clip_jobs(selected, clips_dir, subtitles_dir, export_subtitles):
    """按导出顺序预计算每条 clip 的编号、文件名与输出路径（纯计算，确定顺序）。"""
    jobs = []
    for output_index, candidate in enumerate(selected, 1):
        filename_base = f"{output_index:03d}_{_safe_filename(candidate.title or f'直播片段_{output_index:03d}')}"
        jobs.append(
            {
                "output_index": output_index,
                "candidate": candidate,
                "output_path": os.path.join(clips_dir, f"{filename_base}.mp4"),
                "subtitle_path": os.path.join(subtitles_dir, f"{filename_base}.srt") if export_subtitles else "",
            }
        )
    return jobs


def _run_clip_jobs(jobs, video_path, chunks, config, export_subtitles, subtitle_optimizer=None):
    """并发执行各 clip 裁剪与字幕切片，返回与输入同序的结果列表。"""
    if not jobs:
        return []

    def worker(job):
        return _run_single_clip_job(job, video_path, chunks, config, export_subtitles, subtitle_optimizer)

    workers = max(1, min(int(config.get("export_concurrency", 1)), len(jobs)))
    if workers == 1:
        return [worker(job) for job in jobs]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(worker, jobs))


def _run_single_clip_job(job, video_path, chunks, config, export_subtitles, subtitle_optimizer=None):
    """裁剪单条 clip（subprocess I/O）及其字幕；返回成功标记与已写出路径。"""
    candidate = job["candidate"]
    output_path = job["output_path"]
    subtitle_path = job["subtitle_path"]
    paths = [output_path]
    status = OPTIMIZE_DISABLED
    try:
        # 先生成旁挂 SRT（优化成功用优化块，否则规则兜底），再据状态与 burn_subtitles 决定是否烧录。
        burn_path = None
        if export_subtitles and subtitle_path:
            paths.append(subtitle_path)
            clip_start = max(0.0, candidate.start_time - float(config["buffer_start"]))
            clip_end = candidate.end_time + float(config["buffer_end"])
            window_chunks = _slice_chunks_for_clip(chunks, clip_start, clip_end)
            blocks, status = optimize_clip_subtitle_chunks(window_chunks, config, subtitle_optimizer)
            export_srt(blocks, subtitle_path)
            # 优化失败（FAILED）抑制烧录：仍导出视频 + 旁挂规则 SRT，标人工复核；
            # OK/DISABLED 维持既有烧录行为。
            if config.get("burn_subtitles", True) and status in {OPTIMIZE_OK, OPTIMIZE_DISABLED}:
                burn_path = subtitle_path

        if not clip_segment(video_path, candidate, output_path, config, subtitle_path=burn_path):
            return {"index": job["output_index"], "ok": False, "paths": paths, "info": None}

        info = _build_clip_info(candidate, job["output_index"], output_path, subtitle_path, status)
        return {"index": job["output_index"], "ok": True, "paths": paths, "info": info}
    except (OSError, ValueError):
        return {"index": job["output_index"], "ok": False, "paths": paths, "info": None}


def _build_clip_info(candidate, output_index, output_path, subtitle_path, status=OPTIMIZE_DISABLED):
    selection = candidate.export_selection
    subtitle_optimized = status != OPTIMIZE_FAILED
    subtitle_optimization_note = (
        "字幕优化失败，已回退规则字幕、未烧录，待人工复核" if status == OPTIMIZE_FAILED else ""
    )
    return LiveClipInfo(
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
        subtitle_optimized=subtitle_optimized,
        subtitle_optimization_note=subtitle_optimization_note,
    )


def optimize_clip_subtitle_chunks(window_chunks, config, optimizer):
    """生成短视频字幕显示块并标注来源状态。

    返回 (blocks, status)：
    - OK：字幕优化模型在子序列约束下成功删词断句并对齐回逐字时间；
    - DISABLED：优化关闭、无 provider 或 provider 不可用，走规则兜底（同既有行为）；
    - FAILED：调用失败/超时/非子序列，走规则兜底，由上层抑制烧录并标人工复核。
    """
    if not config.get("subtitle_optimization_enabled", True) or optimizer is None or not optimizer.is_available():
        return _rule_subtitle_chunks(window_chunks, config), OPTIMIZE_DISABLED
    blocks = optimizer.optimize_window(window_chunks)
    if blocks is None:
        return _rule_subtitle_chunks(window_chunks, config), OPTIMIZE_FAILED
    return blocks, OPTIMIZE_OK


def _prepare_clip_subtitle_chunks(chunks, clip_start, clip_end, config):
    """切片到 clip 窗口后生成规则字幕块（语气词过滤 + 显示块重切）。"""
    return _rule_subtitle_chunks(_slice_chunks_for_clip(chunks, clip_start, clip_end), config)


def _rule_subtitle_chunks(window_chunks, config):
    """规则兜底字幕块：语气词过滤（丢空块）→ 显示块重切 → 二次过滤纯语气词块。

    仅作用于短视频旁挂/烧录 SRT；transcript.srt 仍由全量 chunk 忠实导出，不经此路径。
    """
    filler_words = config.get("filler_words") or []
    filtered = []
    for chunk in window_chunks:
        text = filter_filler_words(chunk.text, filler_words)
        if not text:
            continue
        filtered.append(TranscriptChunk(start=chunk.start, end=chunk.end, text=text))
    max_chars = int(config.get("subtitle_max_chars_per_line", 15))
    max_lines = int(config.get("subtitle_max_lines", 2))
    blocks = resegment_chunks(filtered, max_chars, max_lines)
    # 重切按字符数硬切，可能把黏连在多字 token 里的尾随语气词（如「呢呃？」）
    # 单独切成一个显示块「呃？」。这类纯语气词块只有在切分后才暴露，需再过一次
    # 语气词过滤丢弃清理后为空的块，保证旁挂/烧录 SRT 不出现纯语气词 cue。
    cleaned = []
    for block in blocks:
        text = filter_filler_words(block.text, filler_words)
        if not text:
            continue
        cleaned.append(TranscriptChunk(start=block.start, end=block.end, text=text))
    return cleaned


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
                char_spans=_sliced_char_spans(chunk, clip_start, clip_end, text),
            )
        )
    return sliced


def _sliced_char_spans(chunk, clip_start, clip_end, text):
    """clip 内完整包含的 chunk 平移逐字时间到 clip 相对时间；被裁剪或缺失则置 None。

    边界被裁剪的 chunk 文本未截断而时间被夹紧，逐字时间无法可靠对齐，交给比例兜底。
    """
    spans = chunk.char_spans
    if spans is None or len(spans) != len(text):
        return None
    if float(chunk.start) < clip_start or float(chunk.end) > clip_end:
        return None
    return [(span_start - clip_start, span_end - clip_start) for span_start, span_end in spans]


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
        "subtitle_optimized": item.subtitle_optimized,
        "subtitle_optimization_note": item.subtitle_optimization_note,
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
