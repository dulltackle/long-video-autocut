import json

import pytest

from video_auto_editor.context import load_course_context


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_course_context_accepts_known_and_unknown_fields(tmp_path):
    path = write_json(
        tmp_path / "context.json",
        {
            "course_title": "直播课",
            "priority_topics": ["剪辑", "发布"],
            "unknown_future_field": {"enabled": True},
        },
    )

    context = load_course_context(path)

    assert context.data["course_title"] == "直播课"
    assert context.data["unknown_future_field"] == {"enabled": True}
    assert context.summary() == {
        "known_fields": ["course_title", "priority_topics"],
        "string_fields": ["course_title"],
        "list_counts": {"priority_topics": 2},
        "unknown_fields": ["unknown_future_field"],
    }


def test_load_course_context_rejects_non_object_json(tmp_path):
    path = write_json(tmp_path / "context.json", ["not", "object"])

    with pytest.raises(ValueError, match="JSON object"):
        load_course_context(path)


def test_load_course_context_rejects_known_string_field_type(tmp_path):
    path = write_json(tmp_path / "context.json", {"course_title": ["bad"]})

    with pytest.raises(ValueError, match="course_title 必须是字符串"):
        load_course_context(path)


def test_load_course_context_rejects_known_list_field_item_type(tmp_path):
    path = write_json(tmp_path / "context.json", {"forbidden_terms": ["ok", 1]})

    with pytest.raises(ValueError, match="forbidden_terms\\[1\\] 必须是字符串"):
        load_course_context(path)


def test_load_course_context_rejects_invalid_json(tmp_path):
    path = tmp_path / "context.json"
    path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError, match="合法 JSON"):
        load_course_context(path)
