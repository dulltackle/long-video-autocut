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
):
    """构造可被调度器读取的拆条方案。"""
    return {
        "source_video": os.path.basename(source_video),
        "status": status,
        "context": {
            "loaded": course_context is not None,
            "summary": course_context.summary() if course_context is not None else {},
        },
        "review_provider": review_provider or {},
        "candidates": [_candidate_payload(candidate) for candidate in candidates],
        "selected": [_candidate_payload(candidate) for candidate in selected],
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
    }
