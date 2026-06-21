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


def diagnose_run(
    exit_code=0,
    has_transcript=True,
    plan=None,
    warnings=None,
    video_path="<video>",
    output_dir="out/live",
    work_dir="work/live",
    context_file="out/live/course-context.json",
):
    """基于退出码、warnings 与缺失产物给出结构化失败诊断与二次运行建议。

    诊断只解释既有信号，不臆造未在产物或退出码中体现的失败原因。
    """
    plan = plan or {}
    warnings = list(warnings if warnings is not None else plan.get("warnings", []))
    base_cmd = _base_command(video_path, output_dir, work_dir, context_file)

    diagnoses = []

    # 1. ASR 失败：进程异常退出且没有产出 transcript.srt，属于中止类失败。
    if exit_code != 0 and not has_transcript:
        diagnoses.append(
            {
                "category": "asr_failed",
                "severity": "abort",
                "detail": "未生成 transcript.srt，ASR 不可用或识别失败，处理已中止。",
                "hint": "确认已设置 STEPFUN_API_KEY 且网络可达；或将 ASR provider 切换为本地 whisper 后重跑。",
                "rerun_command": f"export STEPFUN_API_KEY=sk-...\n{base_cmd}",
            }
        )
        return diagnoses

    status = plan.get("status")
    export_count = plan.get("export_count", len(plan.get("exports", [])))

    # 2. 评审降级：评审关闭、不可用或缺少 API Key，输出未评审方案。
    if status == "unreviewed":
        diagnoses.append(_review_degraded_diagnosis(warnings, base_cmd, video_path, output_dir, work_dir, context_file))

    # 3. 评审成功但无发布就绪候选：正常结束、导出为空，而非失败。
    elif status == "reviewed" and export_count == 0:
        diagnoses.append(
            {
                "category": "no_publish_ready",
                "severity": "info",
                "detail": "评审完成但没有发布就绪候选，正常结束、导出为空，并非失败。",
                "hint": "可降低发布就绪阈值或补充更优质的直播素材；如需先看完整方案，加 --dry-run 复跑。",
                "rerun_command": f"{base_cmd} --dry-run",
            }
        )

    # 4. 缺少课程上下文：评审质量下降提示（仅提示，不视为失败）。
    if not _context_loaded(plan):
        diagnoses.append(
            {
                "category": "missing_context",
                "severity": "info",
                "detail": "未提供课程上下文，主题评审缺少课程信息，可能影响标题与主题判定质量。",
                "hint": "整理课程标题、讲师、重点主题等信息生成 --context-file 后重跑，可提升评审质量。",
                "rerun_command": base_cmd,
            }
        )

    return diagnoses


def _review_degraded_diagnosis(warnings, base_cmd, video_path, output_dir, work_dir, context_file):
    joined = " ".join(str(message) for message in warnings)
    if "缺少 API Key" in joined:
        detail = "主题评审因缺少 API Key 不可用，已输出未评审方案，默认不导出。"
        hint = "设置 STEPFUN_API_KEY 后重跑以启用评审；或显式允许未评审兼容导出。"
        rerun = f"export STEPFUN_API_KEY=sk-...\n{base_cmd}"
    elif "已关闭" in joined:
        detail = "主题评审已关闭，已输出未评审方案，默认不导出。"
        hint = "启用 topic_review_enabled 并配置评审模型；或显式允许未评审兼容导出。"
        rerun = _command_with_flag(video_path, output_dir, work_dir, context_file, "--allow-unreviewed-export")
    elif "评审失败" in joined or "配置错误" in joined:
        detail = "主题评审请求失败或配置错误，已降级输出未评审方案。"
        hint = "检查评审模型配置与网络后重跑；或显式允许未评审兼容导出。"
        rerun = f"{base_cmd}"
    else:
        detail = "评审未生效，已输出未评审方案，默认不导出。"
        hint = "确认评审已启用且凭据齐备后重跑；或显式允许未评审兼容导出。"
        rerun = f"{base_cmd}"
    return {
        "category": "review_degraded",
        "severity": "degraded",
        "detail": detail,
        "hint": hint,
        "rerun_command": rerun,
        "compatibility_command": _command_with_flag(
            video_path, output_dir, work_dir, context_file, "--allow-unreviewed-export"
        ),
    }


def _context_loaded(plan):
    context = plan.get("context") or {}
    return bool(context.get("loaded"))


def _base_command(video_path, output_dir, work_dir, context_file):
    return (
        f"video-auto-editor live {video_path} "
        f"--output-dir {output_dir} --work-dir {work_dir} --context-file {context_file}"
    )


def _command_with_flag(video_path, output_dir, work_dir, context_file, flag):
    return f"{_base_command(video_path, output_dir, work_dir, context_file)} {flag}"


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
