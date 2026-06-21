"""直播拆条 plan.json 输出。"""

import json
import os


def build_plan(
    source_video,
    course_context,
    candidates,
    selected,
    warnings=None,
    status="unreviewed",
    review_provider=None,
    config=None,
    dry_run=False,
):
    """构造可被调度器读取的拆条方案。"""
    config = config or {}
    export_mode = _export_mode(status, config)
    return {
        "source_video": os.path.basename(source_video),
        "status": status,
        "export_mode": export_mode,
        "dry_run": bool(dry_run),
        "publish_ready_threshold": config.get("topic_review_publish_ready_threshold"),
        "export_count": len(selected),
        "skipped_count": max(0, len(candidates) - len(selected)),
        "context": {
            "loaded": course_context is not None,
            "summary": course_context.summary() if course_context is not None else {},
        },
        "review_provider": review_provider or {},
        "candidates": [_candidate_payload(candidate) for candidate in candidates],
        "selected": [_candidate_payload(candidate) for candidate in selected],
        "exports": [_planned_export_payload(candidate, index, dry_run) for index, candidate in enumerate(selected, 1)],
        "warnings": list(warnings or []),
    }


def write_plan(
    source_video,
    output_dir,
    course_context,
    candidates,
    selected,
    warnings=None,
    status="unreviewed",
    review_provider=None,
    config=None,
    dry_run=False,
):
    """写出 plan.json 并返回路径。"""
    os.makedirs(output_dir, exist_ok=True)
    plan_path = os.path.join(output_dir, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as plan_file:
        json.dump(
            build_plan(
                source_video,
                course_context,
                candidates,
                selected,
                warnings,
                status=status,
                review_provider=review_provider,
                config=config,
                dry_run=dry_run,
            ),
            plan_file,
            ensure_ascii=False,
            indent=2,
        )
        plan_file.write("\n")
    return plan_path


def _candidate_payload(candidate):
    score = candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score
    payload = {
        "index": candidate.index,
        "title": candidate.title,
        "start": candidate.start_time,
        "end": candidate.end_time,
        "duration": candidate.duration,
        "score": score,
        "source": candidate.source,
        "summary": candidate.summary,
        "keywords": list(candidate.keywords),
        "is_duplicate": candidate.is_duplicate,
        "duplicate_with": list(candidate.duplicate_with),
    }
    if candidate.review is not None:
        payload["review"] = _review_payload(candidate.review)
    if candidate.export_selection is not None:
        payload["export_selection"] = _export_selection_payload(candidate.export_selection)
    return payload


def _review_payload(review):
    return {
        "topic_name": review.topic_name,
        "topic_complete": review.topic_complete,
        "learning_value": review.learning_value,
        "share_value": review.share_value,
        "publish_ready_score": review.publish_ready_score,
        "export_decision": review.export_decision,
        "title": review.title,
        "summary": review.summary,
        "keywords": list(review.keywords),
        "needs_human_review": review.needs_human_review,
        "reject_reason": review.reject_reason,
        "boundary_fix_suggestion": review.boundary_fix_suggestion,
        "boundary_fix_start": review.boundary_fix_start,
        "boundary_fix_end": review.boundary_fix_end,
    }


def _export_mode(status, config):
    if status == "reviewed":
        return "reviewed_publish_ready"
    if config.get("allow_unreviewed_export", False):
        return "unreviewed_compatibility"
    return "unreviewed_no_export"


def _planned_export_payload(candidate, export_index, dry_run):
    title = candidate.title or f"直播片段_{export_index:03d}"
    filename_base = f"{export_index:03d}_{_safe_filename(title)}"
    return {
        "export_index": export_index,
        "candidate_index": candidate.index,
        "title": title,
        "video_path": f"clips/{filename_base}.mp4",
        "subtitle_path": f"subtitles/{filename_base}.srt",
        "generated": False,
        "dry_run": bool(dry_run),
    }


def _export_selection_payload(selection):
    return {
        "candidate_index": selection.candidate_index,
        "selected_for_export": selection.selected_for_export,
        "decision": selection.decision,
        "reason": selection.reason,
        "review_status": selection.review_status,
        "publish_ready_score": selection.publish_ready_score,
        "export_rank": selection.export_rank,
        "original_start": selection.original_start,
        "original_end": selection.original_end,
        "final_start": selection.final_start,
        "final_end": selection.final_end,
        "topic_name": selection.topic_name,
        "needs_human_review": selection.needs_human_review,
        "boundary_fix_suggestion": selection.boundary_fix_suggestion,
        "boundary_fix_applied": selection.boundary_fix_applied,
        "series_key": selection.series_key,
    }


def _safe_filename(value):
    safe = "".join("_" if char in '\\/:*?"<>|' else char for char in str(value))
    safe = "_".join(safe.split()).strip("._ ")
    return (safe or "直播片段")[:48]
