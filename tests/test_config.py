import json

import pytest

from video_auto_editor.config import CONFIG, load_config_file, merge_config_file


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_config_file_accepts_known_fields(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(
        config_path,
        {
            "asr_provider": "whisper",
            "max_clips": 3,
            "allow_unreviewed_export": True,
            "topic_review_retry_attempts": 2,
            "topic_review_retry_backoff_seconds": 0.5,
        },
    )

    payload = load_config_file(str(config_path))

    assert payload == {
        "asr_provider": "whisper",
        "max_clips": 3,
        "allow_unreviewed_export": True,
        "topic_review_retry_attempts": 2,
        "topic_review_retry_backoff_seconds": 0.5,
    }


def test_merge_config_file_overrides_base_copy(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"asr_provider": "whisper"})

    merged = merge_config_file(CONFIG, str(config_path))

    assert merged["asr_provider"] == "whisper"
    assert CONFIG["asr_provider"] == "stepaudio"


def test_merge_config_file_validates_against_passed_base_config(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"custom_threshold": 2})

    merged = merge_config_file({"custom_threshold": 1}, str(config_path))

    assert merged == {"custom_threshold": 2}


def test_load_config_file_rejects_unknown_fields(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"stepfun_api_key": "sk-secret"})

    with pytest.raises(ValueError, match=f"配置文件 {config_path} 包含未知配置项：stepfun_api_key"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_non_object_json(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ValueError, match=f"配置文件 {config_path} 必须是 JSON object"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_invalid_json(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError, match=f"配置文件 {config_path} 必须是合法 JSON"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_missing_file(tmp_path):
    config_path = tmp_path / "missing.json"
    with pytest.raises(ValueError, match=f"无法读取配置文件 {config_path}"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_null(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"asr_provider": None})

    with pytest.raises(ValueError, match=f"配置文件 {config_path}：配置项 asr_provider 不允许为 null"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_wrong_scalar_types(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"max_clips": "5"})

    with pytest.raises(ValueError, match=f"配置文件 {config_path}：配置项 max_clips 必须是 integer"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_bool_from_integer(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"topic_review_enabled": 1})

    with pytest.raises(ValueError, match=f"配置文件 {config_path}：配置项 topic_review_enabled 必须是 boolean"):
        load_config_file(str(config_path))


def test_load_config_file_accepts_integer_for_float_field(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"topic_review_temperature": 0})

    assert load_config_file(str(config_path)) == {"topic_review_temperature": 0}


def test_subtitle_config_defaults():
    assert CONFIG["burn_subtitles"] is True
    assert CONFIG["filler_words"] == ["嗯", "啊", "呃", "哦", "唉", "呐", "嘛", "咯", "呀", "哎", "欸", "噢", "唔"]
    assert CONFIG["subtitle_max_chars_per_line"] == 15
    assert CONFIG["subtitle_max_lines"] == 1
    assert CONFIG["subtitle_font"] == "Noto Sans CJK SC"
    assert CONFIG["subtitle_font_size"] == 18
    assert CONFIG["subtitle_outline"] == 2
    assert CONFIG["subtitle_margin_v"] == 20


def test_load_config_file_accepts_filler_words_override(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"filler_words": ["嗯", "啊"]})

    assert load_config_file(str(config_path)) == {"filler_words": ["嗯", "啊"]}


def test_load_config_file_rejects_filler_words_non_list(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"filler_words": "嗯啊"})

    with pytest.raises(ValueError, match=f"配置文件 {config_path}：配置项 filler_words 类型不匹配"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_burn_subtitles_non_bool(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"burn_subtitles": 1})

    with pytest.raises(ValueError, match=f"配置文件 {config_path}：配置项 burn_subtitles 必须是 boolean"):
        load_config_file(str(config_path))


def test_load_config_file_rejects_subtitle_max_chars_non_int(tmp_path):
    config_path = tmp_path / "config.json"
    write_json(config_path, {"subtitle_max_chars_per_line": "15"})

    with pytest.raises(ValueError, match=f"配置文件 {config_path}：配置项 subtitle_max_chars_per_line 必须是 integer"):
        load_config_file(str(config_path))
