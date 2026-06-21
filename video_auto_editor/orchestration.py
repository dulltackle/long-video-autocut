"""直播拆条产物解释器。

只读 ``plan.json`` 与 ``metadata.json`` 产物，生成结构化解释结果，供调度器
skill 渲染给用户。解释器不重新计算候选时间、不重新判定发布就绪，缺失 metadata
时按计划态解释，不伪造已生成文件。
"""

import json
import os


RUN_MODE_LABELS = {
    "reviewed_export": "已评审且实际导出（reviewed 非 dry-run）",
    "reviewed_dry_run": "已评审的 dry-run 方案（未导出视频）",
    "unreviewed_no_export": "未评审、默认不导出",
    "unreviewed_compatibility": "未评审兼容导出（--allow-unreviewed-export）",
}

REASON_LABELS = {
    "duplicate": "与已选片段内容重复",
    "missing_review": "缺少评审结论",
    "needs_human_review": "评审标记需人工复核",
    "boundary_fix_needs_human_review": "边界修复建议需人工确认",
    "publish_ready_score_below_threshold": "发布就绪评分低于阈值",
    "topic_incomplete": "主题不完整",
    "max_clips_limit": "超出最大导出数量限制",
    "unreviewed_export_not_allowed": "未评审且未允许兼容导出",
    "legacy_score_not_selected": "旧评分未入选",
}

HUMAN_REVIEW_REASONS = {"needs_human_review", "boundary_fix_needs_human_review"}


def load_artifacts(output_dir):
    """读取产物目录中的 plan.json 与 metadata.json（存在时）。"""
    plan = _read_json(os.path.join(output_dir, "plan.json"))
    if plan is None:
        raise ValueError(f"未找到 plan.json：{output_dir}")
    metadata = _read_json(os.path.join(output_dir, "metadata.json"))
    return plan, metadata


def interpret_output_dir(output_dir):
    """读取产物目录并返回结构化解释结果。"""
    plan, metadata = load_artifacts(output_dir)
    return interpret_artifacts(plan, metadata)


def interpret_artifacts(plan, metadata=None):
    """根据 plan.json 与可选 metadata.json 生成结构化解释。"""
    plan = plan or {}
    has_metadata = metadata is not None
    run_mode = _run_mode(plan, has_metadata)

    exports = _interpret_exports(plan, metadata)
    not_exported = _interpret_not_exported(plan, metadata)
    human_review = _interpret_human_review(exports, not_exported, metadata)
    series = _interpret_series(exports, not_exported)
    warnings = _interpret_warnings(plan)

    return {
        "run_mode": run_mode,
        "run_mode_label": RUN_MODE_LABELS.get(run_mode, run_mode),
        "status": plan.get("status", ""),
        "source_video": plan.get("source_video", ""),
        "publish_ready_threshold": plan.get("publish_ready_threshold"),
        "exports_generated": has_metadata,
        "export_count": len(exports),
        "exports": exports,
        "not_exported": not_exported,
        "human_review": human_review,
        "series": series,
        "warnings": warnings,
    }


def _run_mode(plan, has_metadata):
    status = plan.get("status")
    export_mode = plan.get("export_mode")
    dry_run = bool(plan.get("dry_run"))
    if status == "reviewed":
        if dry_run or not has_metadata:
            return "reviewed_dry_run"
        return "reviewed_export"
    if export_mode == "unreviewed_compatibility":
        return "unreviewed_compatibility"
    return "unreviewed_no_export"


def _interpret_exports(plan, metadata):
    if metadata is not None:
        clips = metadata.get("clips") or metadata.get("exports") or []
        return [_export_from_metadata(clip) for clip in clips]
    return [_export_from_plan(item) for item in plan.get("exports", [])]


def _export_from_metadata(clip):
    return {
        "index": clip.get("index", 0),
        "title": clip.get("title") or "",
        "topic_name": clip.get("topic_name") or "",
        "publish_ready_score": clip.get("publish_ready_score"),
        "final_start": clip.get("final_start"),
        "final_end": clip.get("final_end"),
        "video_path": clip.get("output_path") or "",
        "subtitle_path": clip.get("subtitle_path") or "",
        "series_key": clip.get("series_key") or "",
        "needs_human_review": bool(clip.get("needs_human_review")),
        "generated": True,
    }


def _export_from_plan(item):
    selection = item.get("export_selection") or {}
    return {
        "index": item.get("export_index", 0),
        "title": item.get("title") or "",
        "topic_name": selection.get("topic_name") or "",
        "publish_ready_score": selection.get("publish_ready_score"),
        "final_start": selection.get("final_start"),
        "final_end": selection.get("final_end"),
        "video_path": item.get("video_path") or "",
        "subtitle_path": item.get("subtitle_path") or "",
        "series_key": selection.get("series_key") or "",
        "needs_human_review": bool(selection.get("needs_human_review")),
        "generated": bool(item.get("generated")),
    }


def _interpret_not_exported(plan, metadata):
    if metadata is not None and metadata.get("not_exported") is not None:
        return [_not_exported_entry(entry) for entry in metadata.get("not_exported", [])]
    entries = []
    for candidate in plan.get("candidates", []):
        selection = candidate.get("export_selection")
        if selection is None or selection.get("selected_for_export"):
            continue
        merged = dict(selection)
        merged.setdefault("candidate_index", candidate.get("index"))
        entries.append(_not_exported_entry(merged))
    return entries


def _not_exported_entry(entry):
    reason = entry.get("reason") or ""
    needs_human_review = bool(entry.get("needs_human_review")) or reason in HUMAN_REVIEW_REASONS
    return {
        "candidate_index": entry.get("candidate_index"),
        "decision": entry.get("decision") or "",
        "reason": reason,
        "reason_label": REASON_LABELS.get(reason, reason or "未说明原因"),
        "topic_name": entry.get("topic_name") or "",
        "publish_ready_score": entry.get("publish_ready_score"),
        "needs_human_review": needs_human_review,
        "boundary_fix_suggestion": entry.get("boundary_fix_suggestion") or "",
        "series_key": entry.get("series_key") or "",
    }


def _interpret_human_review(exports, not_exported, metadata):
    items = []
    for export in exports:
        if export.get("needs_human_review"):
            items.append(
                {
                    "source": "export",
                    "index": export.get("index"),
                    "title": export.get("title"),
                    "topic_name": export.get("topic_name"),
                    "boundary_fix_suggestion": "",
                }
            )
    for entry in not_exported:
        if entry.get("needs_human_review"):
            items.append(
                {
                    "source": "not_exported",
                    "index": entry.get("candidate_index"),
                    "title": "",
                    "topic_name": entry.get("topic_name"),
                    "boundary_fix_suggestion": entry.get("boundary_fix_suggestion") or "",
                }
            )
    return items


def _interpret_series(exports, not_exported):
    groups = {}
    order = []
    for export in exports:
        _add_to_series(groups, order, export.get("series_key"), {
            "type": "export",
            "title": export.get("title"),
            "index": export.get("index"),
        })
    for entry in not_exported:
        _add_to_series(groups, order, entry.get("series_key"), {
            "type": "not_exported",
            "title": entry.get("topic_name") or "",
            "index": entry.get("candidate_index"),
        })
    return [{"series_key": key, "items": groups[key]} for key in order if len(groups[key]) > 1]


def _add_to_series(groups, order, series_key, item):
    if not series_key:
        return
    if series_key not in groups:
        groups[series_key] = []
        order.append(series_key)
    groups[series_key].append(item)


def _interpret_warnings(plan):
    return [{"message": str(message)} for message in plan.get("warnings", [])]


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
