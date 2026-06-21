import json

import pytest

from video_auto_editor.context import (
    build_course_context,
    load_course_context,
    write_course_context,
)


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_course_context_accepts_known_and_unknown_fields(tmp_path):
    unknown_value = {"enabled": True}
    path = write_json(
        tmp_path / "context.json",
        {
            "course_title": "直播课",
            "priority_topics": ["剪辑", "发布"],
            "unknown_future_field": unknown_value,
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

    unknown_value["enabled"] = False
    assert context.data["unknown_future_field"] == {"enabled": True}


def test_course_context_data_returns_copy(tmp_path):
    path = write_json(tmp_path / "context.json", {"course_title": "直播课"})
    context = load_course_context(path)

    data = context.data
    data["course_title"] = "已篡改"
    data["new_field"] = "绕过校验"

    assert context.data == {"course_title": "直播课"}
    assert context.summary()["known_fields"] == ["course_title"]


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


def test_build_course_context_cleans_strings_and_lists():
    build = build_course_context(
        {
            "course_title": "  直播课  ",
            "instructor": "   ",
            "priority_topics": ["剪辑", "  ", " 发布 "],
        }
    )

    assert build.payload["course_title"] == "直播课"
    assert "instructor" not in build.payload
    assert build.payload["priority_topics"] == ["剪辑", "发布"]
    assert build.unknown_fields == []


def test_build_course_context_collects_unknown_fields():
    build = build_course_context(
        {
            "course_title": "直播课",
            "platform": "抖音",
            "extra": 1,
        }
    )

    assert build.unknown_fields == ["extra", "platform"]
    assert "platform" not in build.payload
    assert "extra" not in build.payload


def test_build_course_context_rejects_invalid_string_type():
    with pytest.raises(ValueError, match="course_title 必须是字符串"):
        build_course_context({"course_title": ["bad"]})


def test_build_course_context_rejects_invalid_list_item_type():
    with pytest.raises(ValueError, match="priority_topics\\[1\\] 必须是字符串"):
        build_course_context({"priority_topics": ["ok", 2]})


def test_build_course_context_rejects_non_list_for_list_field():
    with pytest.raises(ValueError, match="forbidden_terms 必须是字符串数组"):
        build_course_context({"forbidden_terms": "term"})


def test_write_course_context_is_loadable(tmp_path):
    path = tmp_path / "nested" / "course-context.json"
    build = write_course_context(
        {
            "course_title": "直播课",
            "priority_topics": ["剪辑", "发布"],
            "platform": "抖音",
        },
        path,
    )

    context = load_course_context(path)
    assert context.data["course_title"] == "直播课"
    assert context.data["priority_topics"] == ["剪辑", "发布"]
    assert "platform" not in context.data
    assert build.unknown_fields == ["platform"]


def test_generated_payload_matches_summary_classification(tmp_path):
    path = tmp_path / "course-context.json"
    write_course_context(
        {"course_title": "直播课", "priority_topics": ["剪辑", "发布"]},
        path,
    )

    summary = load_course_context(path).summary()
    assert summary["string_fields"] == ["course_title"]
    assert summary["list_counts"] == {"priority_topics": 2}
    assert summary["unknown_fields"] == []
